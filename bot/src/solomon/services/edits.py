"""Правка задачи словом — чистые функции (`techspec/12-chat-edit.md`).

Здесь решается всё, что не требует ни базы, ни модели: порядок и нумерация
списка открытых задач в промпте, перевод номера модели в задачу, строка
свайпа и последняя задача в разговоре, правка для базы из разбора и кнопки
«какую задачу» и «Вернуть». Чтение и запись — в `services/tasks.py`, сам
блок промпта — в `services/understanding.py`, рядом с другими блоками.

Номер задачи — индекс в списке плюс один: список, по которому модель
назвала номер, и список, по которому бот его переводит, — один и тот же
объект, иначе номер указал бы не на ту задачу.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from solomon import texts
from solomon.db.tasks import TaskDetails, TaskEvent
from solomon.services.repeat import clean_rule, same_rule
from solomon.services.understanding import TaskEdit

logger = logging.getLogger(__name__)

# Сколько задач видит модель (§12.2): дальше 50-й правки словом нет.
TASK_LIMIT = 50
# Кнопок под вопросом «какую задачу» (§12.6).
CANDIDATE_LIMIT = 5
# Последняя задача в разговоре — событие не старше часа (§12.2).
LAST_TASK_WINDOW = timedelta(hours=1)
# Текст сообщения, на которое ответили свайпом, в строке для модели (§5.2).
SWIPE_TEXT_LIMIT = 200
# Суть на кнопке кандидата (§12.6): длиннее — обрезается.
BUTTON_TITLE_LIMIT = 40
# Час срока «в пятницу» (§3.3): база ставит его сама по `due_date`.
DAY_DUE_TIME = time(18, 0)

# Callback кнопок (§12.6). Telegram ограничивает его 64 байтами, поэтому в
# нём только вид действия и id: `pick:<сообщение владельца>:<задача>`,
# `reopen:<задача>`, `back:<задача>:<раз откуда>:<раз куда>` — «Вернуть»
# повторяющейся задачи, разы в секундах Unix (`techspec/13-repeat.md` §13.3).
PICK_PREFIX = "pick:"
REOPEN_PREFIX = "reopen:"
BACK_PREFIX = "back:"

# На что ответили свайпом (§12.2): напоминание, другое сообщение бота, своё.
SwipeTarget = Literal["reminder", "bot", "own"]


def _newest_first(task: TaskDetails) -> float:
    return -task.created_at.timestamp()


def number_tasks(tasks: Iterable[TaskDetails], limit: int = TASK_LIMIT) -> list[TaskDetails]:
    """Список для промпта (§12.2): сначала со сроком по возрастанию, потом без
    срока; при равном сроке и без срока — новые выше. Не больше `limit`.

    Номер задачи — её место в этом списке, начиная с единицы.
    """
    ordered = sorted(tasks, key=_newest_first)
    ordered.sort(
        key=lambda task: (task.due_at is None, task.due_at.timestamp() if task.due_at else 0)
    )
    return ordered[:limit]


def task_by_number(tasks: Sequence[TaskDetails], number: int | None) -> TaskDetails | None:
    """Задача по номеру модели. Номер вне списка — задача не найдена (§12.2)."""
    if number is None or number < 1 or number > len(tasks):
        return None
    return tasks[number - 1]


def number_of(tasks: Sequence[TaskDetails], task_id: str | None) -> int | None:
    """Номер задачи в списке; её там нет — `None`."""
    if task_id is None:
        return None
    for index, task in enumerate(tasks, start=1):
        if task.id == task_id:
            return index
    return None


def candidates_of(tasks: Sequence[TaskDetails], numbers: Iterable[int]) -> list[TaskDetails]:
    """Похожие задачи для кнопок (§12.6): по порядку модели, без повторов и
    номеров вне списка, не больше пяти."""
    picked: list[TaskDetails] = []
    for number in numbers:
        task = task_by_number(tasks, number)
        if task is None or task in picked:
            continue
        picked.append(task)
        if len(picked) == CANDIDATE_LIMIT:
            break
    return picked


def last_task_number(
    events: Iterable[TaskEvent | None], tasks: Sequence[TaskDetails], now: datetime
) -> int | None:
    """«Последняя задача в разговоре» (§12.2): задача более позднего события.

    Событию больше часа или его задачи нет в списке (закрыта) — строки нет;
    к более раннему событию бот не откатывается: о закрытой задаче говорили
    последней, и подставить вместо неё другую значило бы угадывать.
    """
    known = [event for event in events if event is not None]
    if not known:
        return None
    latest = max(known, key=lambda event: event.at)
    if now - latest.at > LAST_TASK_WINDOW:
        return None
    return number_of(tasks, latest.task_id)


def _quoted(text: str | None) -> str | None:
    """Текст сообщения в кавычках для строки свайпа; пусто — ничего."""
    if text is None or not text.strip():
        return None
    body = text.strip()
    if len(body) > SWIPE_TEXT_LIMIT:
        body = body[:SWIPE_TEXT_LIMIT] + "…"
    return f"«{body}»"


def swipe_line(target: SwipeTarget, number: int | None, text: str | None) -> str | None:
    """Строка перед текстом сообщения — на что ответили свайпом (§5.2, §12.2).

    Номер есть — строка с номером; нет — с текстом того сообщения (до 200
    знаков); нет и текста — строки нет. У сообщения бота, которое не
    напоминание, номера не бывает.
    """
    if target == "reminder" and number is not None:
        return f"Ответ на напоминание о задаче №{number}"
    if target == "own" and number is not None:
        return f"Ответ на своё сообщение о задаче №{number}"
    quoted = _quoted(text)
    if quoted is None:
        return None
    if target == "reminder":
        return f"Ответ на напоминание: {quoted}"
    if target == "own":
        return f"Ответ на своё сообщение: {quoted}"
    return f"Ответ на сообщение бота: {quoted}"


@dataclass(frozen=True, slots=True)
class Change:
    """Правка для базы и задача, какой она станет (§12.4).

    `changes` уходит в `edit` как есть: ключи ядра `change_task`, только
    то, что отличается от задачи. Остальное — по нему бот спрашивает план
    и собирает ответ «Перенёс» или «Поправил».

    `repeat` — правило после правки (§13.5): снятый срок снимает и его.
    `repeat_changed` — правило поставлено или сменилось, `repeat_removed` —
    снято словом. `needs_start` — правило назвали, а первого раза нет:
    у задачи нет срока, и он не назван; бот спрашивает, ничего не меняя.
    """

    changes: dict[str, Any]
    title: str
    due_at: datetime | None
    due_precision: str | None
    priority: str
    people: tuple[str, ...]
    due_changed: bool
    repeat: Mapping[str, Any] | None = None
    repeat_changed: bool = False
    repeat_removed: bool = False
    needs_start: bool = False


def _local(moment: datetime, timezone: ZoneInfo) -> datetime:
    """Момент в поясе владельца; без пояса — считается сказанным в нём."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone)
    return moment.astimezone(timezone)


def _new_due(
    task: TaskDetails, edit: TaskEdit, timezone: ZoneInfo
) -> tuple[dict[str, Any], datetime | None, str | None]:
    """Срок из правки: ключ для базы и срок, какой станет. Не меняется — `{}`.

    День уходит датой (`due_date`), и 18:00 ставит база по своему поясу
    (§3.6); час — моментом со смещением. Новый срок главнее снятия: из двух
    противоречивых значений бот выбирает то, что ничего не теряет.
    """
    if edit.due_at is not None:
        local = _local(edit.due_at, timezone)
        precision = edit.due_precision or "time"
        if precision == "day":
            day = local.date()
            same = (
                task.due_precision == "day"
                and task.due_at is not None
                and task.due_at.astimezone(timezone).date() == day
            )
            if same:
                return {}, task.due_at, task.due_precision
            due = datetime.combine(day, DAY_DUE_TIME, tzinfo=timezone)
            return {"due_date": day.isoformat()}, due, "day"
        if task.due_precision == "time" and task.due_at == local:
            return {}, task.due_at, task.due_precision
        return {"due_at": local.isoformat()}, local, "time"
    if edit.due_removed and task.due_at is not None:
        return {"due_at": None}, None, None
    return {}, task.due_at, task.due_precision


def _new_rule(task: TaskDetails, edit: TaskEdit) -> dict[str, Any] | None:
    """Правило из правки — по форме и у задачи; иначе `None` и строка в журнал.

    Вид словом не меняется, поэтому у идеи и желания правило не ставится
    вовсе. Правило не по форме отбрасывается, остальная правка идёт (§13.5).
    """
    if edit.repeat is None:
        return None
    if task.kind != "task":
        logger.warning("Правило повтора у %s не ставится: повторяется только задача", task.kind)
        return None
    rule = clean_rule(edit.repeat)
    if rule is None:
        logger.warning("Правило повтора в правке не по форме, отброшено: %s", edit.repeat)
    return rule


def edit_changes(task: TaskDetails, edit: TaskEdit, timezone: ZoneInfo) -> Change:
    """Слить правку модели с задачей: только отличия, пустое — «не менял» (§12.1).

    Люди — список целиком: «не Кузнецову, а Петрову» заменяет, а не
    дописывает (в отличие от ответа на вопрос, §10.2). Обещание словом не
    снимается — пустое значит «не менял». Вид не меняется вовсе.

    Правило (§13.5): перенос срока без правила меняет только этот раз.
    Новое правило уходит вместе со сроком, какой станет, — он первый раз,
    и база возьмёт из него час серии; то же правило уходит снова, только
    когда сменился срок («теперь в 11»). Новое правило главнее и снятия
    правила, и снятия срока: из противоречивых значений — то, что ничего
    не теряет.
    """
    rule = _new_rule(task, edit)
    if rule is not None and edit.due_removed:
        edit = edit.model_copy(update={"due_removed": False})
    changes, due_at, due_precision = _new_due(task, edit, timezone)
    due_changed = bool(changes)
    repeat: Mapping[str, Any] | None = task.repeat if due_at is not None else None
    repeat_changed = repeat_removed = needs_start = False
    if rule is not None:
        if due_at is None:
            needs_start = True
        elif due_changed or not same_rule(task.repeat, rule):
            changes["repeat"] = rule
            repeat, repeat_changed = rule, True
    elif edit.repeat_removed and task.repeat is not None and due_at is not None:
        changes["repeat"] = None
        repeat, repeat_removed = None, True
    title = (edit.title or "").strip()
    if title and title != task.title:
        changes["title"] = title
    if edit.priority is not None and edit.priority != task.priority:
        changes["priority"] = edit.priority
    if edit.promise is not None and edit.promise != task.promise:
        changes["promise"] = edit.promise
    if edit.people is not None and list(edit.people) != list(task.people):
        changes["people"] = list(edit.people)
    return Change(
        changes=changes,
        title=changes.get("title", task.title),
        due_at=due_at,
        due_precision=due_precision,
        priority=changes.get("priority", task.priority),
        people=tuple(changes.get("people", task.people)),
        due_changed=due_changed,
        repeat=repeat,
        repeat_changed=repeat_changed,
        repeat_removed=repeat_removed,
        needs_start=needs_start,
    )


def pick_data(telegram_message_id: int, task_id: str) -> str:
    """Callback кнопки кандидата: сообщение владельца и задача (§12.6)."""
    return f"{PICK_PREFIX}{telegram_message_id}:{task_id}"


def reopen_data(task_id: str) -> str:
    """Callback кнопки «Вернуть» (§12.6)."""
    return f"{REOPEN_PREFIX}{task_id}"


def back_data(task_id: str, moved_from: int, moved_to: int) -> str:
    """Callback «Вернуть» повторяющейся задачи: с какого раза ушла и на какой (§13.3)."""
    return f"{BACK_PREFIX}{task_id}:{moved_from}:{moved_to}"


def _task_id(value: str) -> str | None:
    """id задачи из callback — только настоящий uuid: остальное в базу не идёт."""
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


def parse_pick(data: str) -> tuple[int, str] | None:
    """Разобрать callback кандидата. Кривой — `None`, а не исключение."""
    if not data.startswith(PICK_PREFIX):
        return None
    message, _, task = data.removeprefix(PICK_PREFIX).partition(":")
    if not message.isdigit():
        return None
    task_id = _task_id(task)
    if task_id is None:
        return None
    return int(message), task_id


def parse_reopen(data: str) -> str | None:
    """Разобрать callback «Вернуть». Кривой — `None`."""
    if not data.startswith(REOPEN_PREFIX):
        return None
    return _task_id(data.removeprefix(REOPEN_PREFIX))


def parse_back(data: str) -> tuple[str, int, int] | None:
    """Разобрать callback «Вернуть» повторяющейся: задача, раз откуда, раз куда.

    Кривой — `None`: разы — только целые секунды без знака.
    """
    if not data.startswith(BACK_PREFIX):
        return None
    task, _, moments = data.removeprefix(BACK_PREFIX).partition(":")
    moved_from, _, moved_to = moments.partition(":")
    if not all(part.isascii() and part.isdigit() for part in (moved_from, moved_to)):
        return None
    task_id = _task_id(task)
    if task_id is None:
        return None
    return task_id, int(moved_from), int(moved_to)


def candidate_label(task: TaskDetails, timezone: ZoneInfo) -> str:
    """Надпись кнопки кандидата: суть до 40 знаков и короткий срок (§12.6)."""
    title = task.title
    if len(title) > BUTTON_TITLE_LIMIT:
        title = title[:BUTTON_TITLE_LIMIT] + "…"
    if task.due_at is None:
        return title
    return (
        f"{title} — {texts.format_short_due(task.due_at.astimezone(timezone), task.due_precision)}"
    )


def pick_question(edit: TaskEdit, now: datetime, timezone: ZoneInfo) -> str:
    """Вопрос над кнопками кандидатов — с действием (§12.6)."""
    if edit.action == "done":
        return texts.PICK_DONE
    if edit.action == "cancel":
        return texts.PICK_CANCEL
    if edit.action == "skip":
        return texts.PICK_SKIP
    if edit.due_at is not None:
        target = texts.format_move_target(
            _local(edit.due_at, timezone), edit.due_precision or "time", now.astimezone(timezone)
        )
        return texts.PICK_MOVE.format(target=target)
    if edit.due_removed:
        return texts.PICK_REMOVE_DUE
    return texts.PICK_CHANGE
