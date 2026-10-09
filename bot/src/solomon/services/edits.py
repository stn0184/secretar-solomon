"""Правка задачи словом — чистые функции (`techspec/12-chat-edit.md`).

Здесь решается всё, что не требует ни базы, ни модели: порядок и нумерация
списка открытых задач в промпте, перевод номера модели в задачу, строка
свайпа и последняя задача в разговоре, правка для базы из разбора — с
прежним часом при переносе (§12.8) — и кнопки «какую задачу» и «Вернуть».
Чтение и запись — в `services/tasks.py`, сам блок промпта — в
`services/understanding.py`, рядом с другими блоками.

Номер задачи — индекс в списке плюс один: список, по которому модель
назвала номер, и список, по которому бот его переводит, — один и тот же
объект, иначе номер указал бы не на ту задачу.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from solomon import texts
from solomon.db.tasks import TaskDetails, TaskEvent
from solomon.services import parts
from solomon.services.repeat import clean_rule, same_rule
from solomon.services.understanding import SPHERE_ACTION, TaskEdit

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
# повторяющейся задачи, разы в секундах Unix (`techspec/13-repeat.md` §13.3);
# `apart:<сообщение владельца>` — «Записать отдельно» под дублем
# (`techspec/15-duplicates.md` §15.4). Под ответом о нескольких делах
# (`techspec/23-several-tasks.md` §23.5) — `apart:<сообщение>:<номер дела>`,
# а «Вернуть» несёт сообщение владельца вместо задачи: `reopen:<сообщение>`,
# `back:<сообщение>:<раз откуда>:<раз куда>`.
PICK_PREFIX = "pick:"
REOPEN_PREFIX = "reopen:"
BACK_PREFIX = "back:"
APART_PREFIX = "apart:"
# Дел на одно сообщение (§23.3): номер дела в callback — от 1 до этого.
ITEM_LIMIT = 10

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


def last_task_numbers(
    events: Iterable[TaskEvent | None], tasks: Sequence[TaskDetails], now: datetime
) -> list[int]:
    """«Последние задачи в разговоре» (§12.2, `techspec/23-several-tasks.md`
    §23.2): номера задач более позднего события — у сообщения о нескольких
    делах их несколько, по порядку дел.

    Событию больше часа — строки нет. Задачи, которой нет в списке
    (закрыта), нет и среди номеров; нет ни одной — строки нет: к более
    раннему событию бот не откатывается, о закрытой задаче говорили
    последней, и подставить вместо неё другую значило бы угадывать.
    """
    known = [event for event in events if event is not None]
    if not known:
        return []
    latest = max(known, key=lambda event: event.at)
    if now - latest.at > LAST_TASK_WINDOW:
        return []
    numbers = (number_of(tasks, task_id) for task_id in (latest.task_id, *latest.more))
    return [number for number in numbers if number is not None]


def _quoted(text: str | None) -> str | None:
    """Текст сообщения в кавычках для строки свайпа; пусто — ничего."""
    if text is None or not text.strip():
        return None
    body = text.strip()
    if len(body) > SWIPE_TEXT_LIMIT:
        body = body[:SWIPE_TEXT_LIMIT] + "…"
    return f"«{body}»"


def swipe_line(target: SwipeTarget, numbers: Sequence[int], text: str | None) -> str | None:
    """Строка перед текстом сообщения — на что ответили свайпом (§5.2, §12.2).

    Номера есть — строка с номером; у своего сообщения о нескольких делах —
    «о задачах №A, №B» (§23.2). Нет — с текстом того сообщения (до 200
    знаков); нет и текста — строки нет. У сообщения бота, которое не
    напоминание, номера не бывает.
    """
    if target == "reminder" and numbers:
        return f"Ответ на напоминание о задаче №{numbers[0]}"
    if target == "own" and len(numbers) == 1:
        return f"Ответ на своё сообщение о задаче №{numbers[0]}"
    if target == "own" and numbers:
        listed = ", ".join(f"№{number}" for number in numbers)
        return f"Ответ на своё сообщение о задачах {listed}"
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

    `named` — правка назвала хоть одно значение, которое бот принял: пустые
    `changes` при нём — «Так и записано», без него — «Не понял» (§12.8).
    `lost_at` и `lost_precision` — прежний час или часть дня, которые не
    удержались при переносе на сегодня: час уже наступил, часть кончилась;
    по ним ответ спрашивает «во сколько?» (§12.8).
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
    named: bool = False
    lost_at: datetime | None = None
    lost_precision: str | None = None


@dataclass(frozen=True, slots=True)
class _Due:
    """Срок после правки: ключи для базы, каким он станет и что не удержалось."""

    changes: dict[str, Any]
    at: datetime | None
    precision: str | None
    lost_at: datetime | None = None
    lost_precision: str | None = None


def _local(moment: datetime, timezone: ZoneInfo) -> datetime:
    """Момент в поясе владельца; без пояса — считается сказанным в нём."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone)
    return moment.astimezone(timezone)


def _on_day(day: date, precision: str, timezone: ZoneInfo) -> datetime:
    """Момент срока на день: у части — её начало (§21.2), у дня — 18:00 (§3.3)."""
    if parts.is_part(precision):
        return parts.part_start(day, precision, timezone)
    return datetime.combine(day, DAY_DUE_TIME, tzinfo=timezone)


def _prior_on(task: TaskDetails, day: date, timezone: ZoneInfo) -> tuple[datetime, str] | None:
    """Прежний час или часть задачи на новый день (§12.8); у дня и без срока — `None`.

    Час — по часам владельца и с минутами: 17:30 остаётся 17:30 нового дня,
    даже если между днями переводили часы.
    """
    if task.due_at is None or task.due_precision is None:
        return None
    if task.due_precision == "time":
        clock = task.due_at.astimezone(timezone).time()
        return datetime.combine(day, clock, tzinfo=timezone), "time"
    if parts.is_part(task.due_precision):
        return parts.part_start(day, task.due_precision, timezone), task.due_precision
    return None


def _passed(moment: datetime, precision: str, now: datetime, timezone: ZoneInfo) -> bool:
    """Час уже наступил или часть дня кончилась (§12.8)."""
    if precision == "time":
        return moment <= now
    return now >= parts.part_end(moment.astimezone(timezone).date(), precision, timezone)


def _moved(
    task: TaskDetails,
    due_at: datetime,
    precision: str,
    timezone: ZoneInfo,
    lost: tuple[datetime, str] | None = None,
) -> _Due:
    """Новый срок задачи и ключи для базы; тот же срок — пустые ключи.

    День уходит датой (`due_date`), и 18:00 ставит база по своему поясу
    (§3.6); час — моментом со смещением; часть дня — моментом её начала и
    ключом `due_precision` (§21.2).
    """
    lost_at, lost_precision = lost if lost is not None else (None, None)
    if precision == "day":
        same = (
            task.due_precision == "day"
            and task.due_at is not None
            and task.due_at.astimezone(timezone).date() == due_at.date()
        )
        if same:
            return _Due({}, task.due_at, task.due_precision, lost_at, lost_precision)
        return _Due({"due_date": due_at.date().isoformat()}, due_at, "day", lost_at, lost_precision)
    if task.due_precision == precision and task.due_at == due_at:
        return _Due({}, task.due_at, task.due_precision, lost_at, lost_precision)
    changes: dict[str, Any] = {"due_at": due_at.isoformat()}
    if parts.is_part(precision):
        changes["due_precision"] = precision
    return _Due(changes, due_at, precision, lost_at, lost_precision)


def _new_due(task: TaskDetails, edit: TaskEdit, timezone: ZoneInfo, now: datetime) -> _Due:
    """Срок из правки по §12.8. Не меняется — пустые ключи и срок задачи.

    Назван час — он и есть срок. Назван только день — прежний час или часть
    задачи на этот день; названа часть — прежний час, если он в ней лежит,
    иначе начало части. `time_removed` прежнего не держит: срок — день или
    названная часть, а без нового дня — тот же день. Перенос на сегодня, а
    прежний час уже наступил или часть кончилась, — срок как назван, и
    `lost_*` говорит, что не удержалось. Новый срок главнее снятия, снять
    час — главнее снять срок: из противоречивых значений бот выбирает то,
    что теряет меньше.
    """
    if edit.due_at is not None:
        local = _local(edit.due_at, timezone)
        precision = edit.due_precision or "time"
        if precision == "time":
            return _moved(task, local, "time", timezone)
        day = local.date()
        named = _on_day(day, precision, timezone)
        prior = None if edit.time_removed else _prior_on(task, day, timezone)
        if prior is not None and parts.is_part(precision):
            # Названа часть: держится только прежний час, лежащий в ней.
            moment, kind = prior
            if kind != "time" or parts.part_of(moment.astimezone(timezone).time()) != precision:
                prior = None
        if prior is None:
            return _moved(task, named, precision, timezone)
        today = now.astimezone(timezone).date()
        if day == today and _passed(*prior, now, timezone):
            return _moved(task, named, precision, timezone, lost=prior)
        return _moved(task, *prior, timezone)
    if task.due_at is None:
        return _Due({}, None, None)
    if edit.time_removed:
        day = task.due_at.astimezone(timezone).date()
        return _moved(task, _on_day(day, "day", timezone), "day", timezone)
    if edit.due_removed:
        return _Due({"due_at": None}, None, None)
    return _Due({}, task.due_at, task.due_precision)


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


def edit_changes(task: TaskDetails, edit: TaskEdit, timezone: ZoneInfo, now: datetime) -> Change:
    """Слить правку модели с задачей: только отличия, пустое — «не менял» (§12.1).

    Прежний час при переносе ставит бот, а не модель (§12.8): модель называет
    только сказанное, а у кнопки кандидата (§12.6) она задачу и не знала.
    `now` — момент правки или нажатия: по нему «час уже прошёл».

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
    due = _new_due(task, edit, timezone, now)
    changes, due_at, due_precision = due.changes, due.at, due.precision
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
    # Названное — то, что бот принял: отброшенное правило и пустая суть не в счёт.
    named = any(
        (
            edit.due_at is not None,
            edit.due_removed,
            edit.time_removed,
            bool(title),
            edit.priority is not None,
            edit.promise is not None,
            edit.people is not None,
            rule is not None,
            edit.repeat_removed,
        )
    )
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
        named=named,
        lost_at=due.lost_at,
        lost_precision=due.lost_precision,
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


def apart_data(telegram_message_id: int, item: int | None = None) -> str:
    """Callback «Записать отдельно»: сообщение владельца, признанное дублем (§15.4).

    `item` — номер дела в сообщении о нескольких делах
    (`techspec/23-several-tasks.md` §23.5); без него — дело номер 1, как у
    кнопок до этапа 023.
    """
    if item is None:
        return f"{APART_PREFIX}{telegram_message_id}"
    return f"{APART_PREFIX}{telegram_message_id}:{item}"


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


def _digits(value: str) -> int | None:
    """Целое из цифр ASCII без знака — или `None`."""
    if value.isascii() and value.isdigit():
        return int(value)
    return None


def parse_apart(data: str) -> tuple[int, int] | None:
    """Разобрать callback «Записать отдельно»: сообщение владельца и номер дела.

    Номера нет — дело номер 1, как у кнопок до этапа 023
    (`techspec/23-several-tasks.md` §23.5). Кривой — `None`: номера —
    только цифры ASCII без знака, дело — от 1 до 10.
    """
    if not data.startswith(APART_PREFIX):
        return None
    message, joined, item = data.removeprefix(APART_PREFIX).partition(":")
    telegram_message_id = _digits(message)
    if telegram_message_id is None:
        return None
    if not joined:
        return telegram_message_id, 1
    number = _digits(item)
    if number is None or not 1 <= number <= ITEM_LIMIT:
        return None
    return telegram_message_id, number


def bound_to_message(data: str, telegram_message_id: int) -> str:
    """Callback кнопки под ответом о нескольких делах (§23.5).

    «Вернуть» несёт сообщение владельца вместо задачи: итог нажатия
    дописывается к ответу этого сообщения, а задача — его `task_id`. Рядом
    с uuid и двумя разами номер сообщения в 64 байта не влезает. Остальные
    кнопки сообщение уже несут — как есть.
    """
    if parse_reopen(data) is not None:
        return f"{REOPEN_PREFIX}{telegram_message_id}"
    back = parse_back(data)
    if back is not None:
        _, moved_from, moved_to = back
        return f"{BACK_PREFIX}{telegram_message_id}:{moved_from}:{moved_to}"
    return data


def parse_reopen_message(data: str) -> int | None:
    """Разобрать «Вернуть» под ответом о нескольких делах: сообщение владельца."""
    if not data.startswith(REOPEN_PREFIX):
        return None
    return _digits(data.removeprefix(REOPEN_PREFIX))


def parse_back_message(data: str) -> tuple[int, int, int] | None:
    """Разобрать «Вернуть» повторяющейся под ответом о нескольких делах:
    сообщение владельца, раз откуда, раз куда."""
    if not data.startswith(BACK_PREFIX):
        return None
    parts = data.removeprefix(BACK_PREFIX).split(":")
    if len(parts) != 3:
        return None
    numbers = [_digits(part) for part in parts]
    message, moved_from, moved_to = numbers
    if message is None or moved_from is None or moved_to is None:
        return None
    return message, moved_from, moved_to


def keeps_button(data: str, pressed: str) -> bool:
    """Остаётся ли кнопка под ответом о нескольких делах после нажатия (§23.5).

    Исчезают кнопки нажатого вопроса: выбор задачи — все кнопки выбора,
    «Записать отдельно» и «Вернуть» — только сама нажатая.
    """
    if data == pressed:
        return False
    return not (pressed.startswith(PICK_PREFIX) and data.startswith(PICK_PREFIX))


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


def _short_title(title: str) -> str:
    """Суть на кнопке — до 40 знаков, дальше «…» (§12.6)."""
    if len(title) > BUTTON_TITLE_LIMIT:
        return title[:BUTTON_TITLE_LIMIT] + "…"
    return title


def candidate_label(task: TaskDetails, timezone: ZoneInfo) -> str:
    """Надпись кнопки кандидата: суть до 40 знаков и короткий срок (§12.6)."""
    title = _short_title(task.title)
    if task.due_at is None:
        return title
    return (
        f"{title} — {texts.format_short_due(task.due_at.astimezone(timezone), task.due_precision)}"
    )


def apart_label(title: str) -> str:
    """«Записать отдельно: <суть>» — когда дублей в ответе несколько
    (`techspec/23-several-tasks.md` §23.5); суть обрезается, как у кандидата."""
    return f"{texts.APART_BUTTON}: {_short_title(title)}"


def pick_question(
    edit: TaskEdit, now: datetime, timezone: ZoneInfo, sphere: str | None = None
) -> str:
    """Вопрос над кнопками кандидатов — с действием (§12.6); у правки сферы —
    с её названием (`techspec/30-spheres.md` §30.2)."""
    if edit.action == SPHERE_ACTION:
        return texts.PICK_SPHERE.format(sphere=sphere) if sphere else texts.PICK_UNSPHERE
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
