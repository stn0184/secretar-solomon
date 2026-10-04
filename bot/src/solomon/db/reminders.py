"""Напоминания: расписание, что созрело, что отправлено и что закрыто кнопкой.

Тот же закон, что у `tasks.py`: `owner_telegram_id` именованный и без
значения по умолчанию в каждой функции, и он же уходит в SQL-функцию —
ключ service-role правила доступа обходит, поэтому разделение по владельцу
держит код (`techspec/04-access.md` §4.3). Исключение одно — `reminder_plan`:
она данных не читает и владельца не знает, это правило §6.1, а не запрос.

Расписание считает база (`techspec/11-edit.md` §11.3): одно правило на бота
и на правку из приложения. Здесь же зеркало пояса владельца — его читает
`edit_task`, которой окружение бота не видно, — и отметка «срок перенесён»,
по которой минутный цикл пишет строку «Перенёс» (§11.4). И кнопка «Вернуть»
под «Закрыл» и «Убрал из списка» (`techspec/12-chat-edit.md` §12.6) — она
возвращает задачу в работу вместе с новым планом напоминаний.

Повторяющиеся задачи (`techspec/13-repeat.md`): «Сделано» переводит задачу
на следующий раз, следующий раз считает база (`repeat_next`), «Вернуть» под
«Отметил» и «Пропускаю» — `return_occurrence`, пропущенный раз двигает
`roll_repeats` в минутном цикле.

Вопрос о деле без срока (`techspec/19-undated.md` §19.4): о каком деле
спросить — `undated_to_ask`, ушедший вопрос — `record_ask`. Память о
вопросе — строка `reminders` со ступенью `ask`; планом напоминаний она не
бывает, поэтому в `Stage` её нет.

Вопрос о прошедшем деле (`techspec/22-overdue.md` §22.4): о каком деле
спросить — `overdue_to_ask`, ушедший вопрос — `record_overdue_ask`. Память —
строка `reminders` со ступенью `overdue`, тоже всегда ушедшая и тоже не
из `Stage`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, get_args

from supabase import Client

from solomon.db.rpc import DatabaseError, ask, moment, single_row
from solomon.db.tasks import TaskDetails, optional_moment, repeat_of, task_details_from_row

DUE_REMINDERS_FUNCTION = "due_reminders"
MARK_REMINDERS_SENT_FUNCTION = "mark_reminders_sent"
MARK_TASK_DONE_FUNCTION = "mark_task_done"
REOPEN_TASK_FUNCTION = "reopen_task"
REMINDER_PLAN_FUNCTION = "reminder_plan"
SAVE_OWNER_TIMEZONE_FUNCTION = "save_owner_timezone"
MOVED_TASKS_FUNCTION = "moved_tasks"
CLEAR_DUE_MOVED_FUNCTION = "clear_due_moved"
REPEAT_NEXT_FUNCTION = "repeat_next"
RETURN_OCCURRENCE_FUNCTION = "return_occurrence"
ROLL_REPEATS_FUNCTION = "roll_repeats"
UNDATED_TO_ASK_FUNCTION = "undated_to_ask"
RECORD_ASK_FUNCTION = "record_ask"
OVERDUE_TO_ASK_FUNCTION = "overdue_to_ask"
RECORD_OVERDUE_ASK_FUNCTION = "record_overdue_ask"

Stage = Literal["before", "due"]


@dataclass(frozen=True, slots=True)
class Planned:
    """Одно запланированное напоминание: ступень и момент."""

    stage: Stage
    fire_at: datetime

    def as_row(self) -> dict[str, str]:
        """Строка для `record_understanding` — по именам колонок §3.5."""
        return {"stage": self.stage, "fire_at": self.fire_at.isoformat()}


def _planned_from_row(row: Any) -> Planned:
    """Строка плана из базы. Незнакомая ступень — отказ, а не молчаливый пропуск."""
    if not isinstance(row, Mapping):
        raise DatabaseError("База вернула не строку плана.")
    try:
        stage = row["stage"]
        fire_at = moment(row["fire_at"], "fire_at")
    except KeyError as error:
        raise DatabaseError(f"В ответе базы нет поля плана: {error}.") from error
    if stage not in get_args(Stage):
        raise DatabaseError(f"База вернула незнакомую ступень: {stage!r}.")
    return Planned(stage=stage, fire_at=fire_at)


async def reminder_plan(
    db: Client,
    *,
    due_at: datetime | None,
    due_precision: str | None,
    kind: str,
    timezone: str,
    now: datetime,
) -> list[Planned]:
    """Расписание напоминаний одной задачи по правилу §6.1 — у базы.

    «Сейчас» передаётся снаружи, как у `due_reminders`: ответ бота называет
    ближайшее из того же плана, что уходит в базу (§6.4). Пусто — стучаться
    не о чем или уже некогда.
    """
    params = {
        "due_at": None if due_at is None else due_at.isoformat(),
        "due_precision": due_precision,
        "kind": kind,
        "timezone": timezone,
        "now": now.isoformat(),
    }
    rows = await ask(lambda: db.rpc(REMINDER_PLAN_FUNCTION, params).execute().data)
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise DatabaseError("База вернула не список плана.")
    return sorted((_planned_from_row(row) for row in rows), key=lambda item: item.fire_at)


async def save_owner_timezone(db: Client, *, owner_telegram_id: int, timezone: str) -> None:
    """Записать пояс владельца в базу — зеркало `OWNER_TIMEZONE` (§11.3).

    Имя пояса проверяет сама база: незнакомое — отказ здесь, при запуске.
    """
    params = {"owner_telegram_id": owner_telegram_id, "timezone": timezone}
    await ask(lambda: db.rpc(SAVE_OWNER_TIMEZONE_FUNCTION, params).execute().data)


@dataclass(frozen=True, slots=True)
class MovedTask:
    """Задача, которой правка из приложения перенесла срок (§11.4).

    Всё, из чего строка «Перенёс» собирается в момент отправки: суть, срок,
    каким он лежит в базе сейчас, и ближайшее неотправленное напоминание.
    `due_moved_at` — прочитанная отметка: снимается ровно она. У
    повторяющейся задачи — правило: строка называет и повтор (§13.6).
    """

    id: str
    title: str
    due_at: datetime | None
    due_precision: str | None
    due_moved_at: datetime
    next_fire_at: datetime | None
    repeat: Mapping[str, Any] | None = None
    occurrence_at: datetime | None = None


def _moved_from_row(row: Any) -> MovedTask:
    """Разобрать строку. Без отметки — отказ: такую строку не погасить."""
    if not isinstance(row, Mapping):
        raise DatabaseError("База вернула не строку переноса.")
    try:
        due_at = row["due_at"]
        next_fire_at = row["next_fire_at"]
        return MovedTask(
            id=str(row["id"]),
            title=str(row["title"]),
            due_at=None if due_at is None else moment(due_at, "due_at"),
            due_precision=None if row["due_precision"] is None else str(row["due_precision"]),
            due_moved_at=moment(row["due_moved_at"], "due_moved_at"),
            next_fire_at=None if next_fire_at is None else moment(next_fire_at, "next_fire_at"),
            repeat=repeat_of(row.get("repeat")),
            occurrence_at=optional_moment(row.get("occurrence_at"), "occurrence_at"),
        )
    except KeyError as error:
        raise DatabaseError(f"В ответе базы нет поля переноса: {error}.") from error


async def moved_tasks(db: Client, *, owner_telegram_id: int) -> list[MovedTask]:
    """Активные задачи владельца с отметкой «срок перенесён» (§11.4).

    Закрытые и удалённые база не отдаёт: о них строки нет.
    """
    params = {"owner_telegram_id": owner_telegram_id}
    rows = await ask(lambda: db.rpc(MOVED_TASKS_FUNCTION, params).execute().data)
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise DatabaseError("База вернула не список переносов.")
    return [_moved_from_row(row) for row in rows]


async def clear_due_moved(
    db: Client, *, owner_telegram_id: int, task_id: str, seen: datetime
) -> bool:
    """Снять отметку после отправки строки — только если она всё ещё `seen`.

    `False` — правка пришла между чтением и снятием: отметка остаётся, и
    следующий тик скажет о последнем сроке. Порядок тот же, что у
    напоминаний: сначала отправка, потом отметка (§6.2).
    """
    params = {"owner_telegram_id": owner_telegram_id, "task_id": task_id, "seen": seen.isoformat()}
    data = await ask(lambda: db.rpc(CLEAR_DUE_MOVED_FUNCTION, params).execute().data)
    if not isinstance(data, bool):
        raise DatabaseError(f"База не ответила, снята ли отметка: {data!r}.")
    return data


@dataclass(frozen=True, slots=True)
class DueReminder:
    """Созревшее напоминание вместе с полями задачи, о которой оно (§3.5).

    Поля задачи приходят тем же запросом: текст напоминания собирается из
    них, и второй поход в базу на каждое напоминание был бы лишним. Раз
    `occurrence_at` уходит в кнопку «Сделано» повторяющейся задачи (§13.3).
    """

    id: str
    task_id: str
    stage: str
    fire_at: datetime
    title: str
    due_at: datetime | None
    due_precision: str | None
    repeat: Mapping[str, Any] | None = None
    occurrence_at: datetime | None = None


def _reminder_from_row(row: Any) -> DueReminder:
    """Разобрать строку. Неполная — отказ, а не напоминание без срока."""
    if not isinstance(row, Mapping):
        raise DatabaseError("База вернула не строку напоминания.")
    try:
        due_at = row["due_at"]
        return DueReminder(
            id=str(row["id"]),
            task_id=str(row["task_id"]),
            stage=str(row["stage"]),
            fire_at=moment(row["fire_at"], "fire_at"),
            title=str(row["title"]),
            due_at=None if due_at is None else moment(due_at, "due_at"),
            due_precision=None if row["due_precision"] is None else str(row["due_precision"]),
            repeat=repeat_of(row.get("repeat")),
            occurrence_at=optional_moment(row.get("occurrence_at"), "occurrence_at"),
        )
    except KeyError as error:
        raise DatabaseError(f"В ответе базы нет поля напоминания: {error}.") from error


async def due_reminders(db: Client, *, owner_telegram_id: int, now: datetime) -> list[DueReminder]:
    """Что у владельца созрело к этому моменту и ещё не ушло (§6.2).

    «Сейчас» передаётся боту снаружи, а не берётся базой: время тика решает
    один и тот же час на всех шагах — и в отборе, и в тексте напоминания.
    """
    params = {"owner_telegram_id": owner_telegram_id, "now": now.isoformat()}
    rows = await ask(lambda: db.rpc(DUE_REMINDERS_FUNCTION, params).execute().data)
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise DatabaseError("База вернула не список напоминаний.")
    return [_reminder_from_row(row) for row in rows]


async def mark_sent(
    db: Client,
    *,
    owner_telegram_id: int,
    reminder_ids: Sequence[str],
    telegram_message_id: int,
) -> None:
    """Пометить отправленным — после того, как Telegram сообщение принял.

    Порядок «отправить → пометить» (§6.2): упало между — следующий тик
    постучится второй раз, и это лучше потерянного напоминания (инвариант 5).
    """
    params = {
        "owner_telegram_id": owner_telegram_id,
        "ids": list(reminder_ids),
        "telegram_message_id": telegram_message_id,
    }
    await ask(lambda: db.rpc(MARK_REMINDERS_SENT_FUNCTION, params).execute().data)


def _task_or_none(data: Any) -> TaskDetails | None:
    """Строка задачи из функции базы; пустая составная строка — задачи нет."""
    row = single_row(data)
    if row is None or (isinstance(row, Mapping) and row.get("id") is None):
        return None
    return task_details_from_row(row)


async def mark_task_done(
    db: Client, *, owner_telegram_id: int, task_id: str, occurrence: int | None = None
) -> TaskDetails | None:
    """«Сделано» под напоминанием: разовую закрыть, повторяющуюся перевести (§6.3, §13.3).

    Разовая закрывается, её неотправленные напоминания снимаются — одной
    транзакцией. Повторяющаяся переходит на следующий раз с планом нового
    раза; `occurrence` — раз из кнопки в секундах Unix: задача стоит на
    другом — база отдаёт её как есть, и второе нажатие через раз не
    перескакивает. Без раза (кнопка до повторов) переходит текущий раз.

    Возвращает задачу, какой её оставила база; `None` — задачи нет или она
    чужая: база сверяет владельца сама, поэтому подставленный в callback
    чужой `task_id` не трогает ничего.
    """
    params: dict[str, Any] = {"owner_telegram_id": owner_telegram_id, "task_id": task_id}
    if occurrence is not None:
        params["occurrence"] = occurrence
    return _task_or_none(await ask(lambda: db.rpc(MARK_TASK_DONE_FUNCTION, params).execute().data))


async def repeat_next(
    db: Client,
    *,
    repeat: Mapping[str, Any],
    occurrence_at: datetime,
    after: datetime,
    timezone: str,
) -> datetime:
    """Следующий раз серии строго позже `after` — у базы (§13.2).

    Правило одно на бота и базу: бот зовёт его, чтобы назвать следующий раз
    в ответе «Отметил» и «Пропускаю» до записи (§13.3). Данных функция не
    читает, владельца не знает. Пусто — правило не посчиталось: отказ.
    """
    params = {
        "repeat": dict(repeat),
        "occurrence_at": occurrence_at.isoformat(),
        "after": after.isoformat(),
        "timezone": timezone,
    }
    data = await ask(lambda: db.rpc(REPEAT_NEXT_FUNCTION, params).execute().data)
    if data is None:
        raise DatabaseError("База не посчитала следующий раз.")
    return moment(data, "repeat_next")


async def return_occurrence(
    db: Client,
    *,
    owner_telegram_id: int,
    task_id: str,
    back_to: int,
    moved_from: int,
    schedule: Sequence[Planned],
) -> TaskDetails | None:
    """«Вернуть» под «Отметил» и «Пропускаю»: задачу — на раз `back_to` (§13.3).

    Разы — секунды Unix из кнопки. База возвращает задачу, только если она
    активна, повторяется и стоит на разе `moved_from`; иначе отдаёт её как
    есть — по разу в ответе видно, вернула она или задача ушла дальше.
    `schedule` — план раза `back_to` на момент нажатия. `None` — задачи нет
    или она чужая.
    """
    params = {
        "owner_telegram_id": owner_telegram_id,
        "task_id": task_id,
        "back_to": back_to,
        "moved_from": moved_from,
        "schedule": [item.as_row() for item in schedule],
    }
    return _task_or_none(
        await ask(lambda: db.rpc(RETURN_OCCURRENCE_FUNCTION, params).execute().data)
    )


async def roll_repeats(db: Client, *, owner_telegram_id: int, now: datetime) -> int:
    """Перевести пропущенные разы повторяющихся задач владельца (§13.4).

    Задача, чей раз прошёл и чей следующий раз уже начался, переходит на
    последний наступивший раз с обеими ступенями напоминаний. «Сейчас» —
    время тика, как у `due_reminders`. Возвращает, сколько задач перешло.
    """
    params = {"owner_telegram_id": owner_telegram_id, "now": now.isoformat()}
    rows = await ask(lambda: db.rpc(ROLL_REPEATS_FUNCTION, params).execute().data)
    if rows is None:
        return 0
    if not isinstance(rows, list):
        raise DatabaseError("База вернула не список перекатанных задач.")
    return len(rows)


async def reopen_task(
    db: Client,
    *,
    owner_telegram_id: int,
    task_id: str,
    schedule: Sequence[Planned],
) -> TaskDetails | None:
    """Вернуть закрытую или убранную задачу в работу (§12.6).

    `schedule` — план на момент нажатия от `reminder_plan`: база заменяет им
    неотправленные напоминания и взводит заново ушедшую ступень, так что
    строка «Напомню» называет ровно записанное. Уже активная задача
    возвращается как есть, без записи; чужая или удалённая — `None`.
    """
    params = {
        "owner_telegram_id": owner_telegram_id,
        "task_id": task_id,
        "schedule": [item.as_row() for item in schedule],
    }
    return _task_or_none(await ask(lambda: db.rpc(REOPEN_TASK_FUNCTION, params).execute().data))


@dataclass(frozen=True, slots=True)
class UndatedTask:
    """Дело без срока, о котором пора спросить (§19.1).

    `created_at` — когда записано: первый вопрос называет этот день.
    `asked_at` — когда бот спрашивал о нём в последний раз; `None` — не
    спрашивал, и вопрос будет первым.
    """

    task_id: str
    title: str
    created_at: datetime
    asked_at: datetime | None


def _undated_from_row(row: Any) -> UndatedTask:
    """Разобрать строку. Неполная — отказ, а не вопрос без сути."""
    if not isinstance(row, Mapping):
        raise DatabaseError("База вернула не строку дела без срока.")
    try:
        return UndatedTask(
            task_id=str(row["task_id"]),
            title=str(row["title"]),
            created_at=moment(row["created_at"], "created_at"),
            asked_at=optional_moment(row["asked_at"], "asked_at"),
        )
    except KeyError as error:
        raise DatabaseError(f"В ответе базы нет поля дела без срока: {error}.") from error


async def undated_to_ask(
    db: Client,
    *,
    owner_telegram_id: int,
    day_start: datetime,
    asked_before: datetime,
    question_since: datetime,
    quiet_since: datetime,
) -> UndatedTask | None:
    """О каком деле без срока спросить сейчас (§19.1, §19.2).

    Границы считает бот (`services/asks.py`), база по ним отбирает: сегодня
    уже спрашивал, живой открытый вопрос или нет 15 минут тишины — `None`;
    иначе первое по порядку §19.1 дело. Больше одной строки — отказ: вопрос
    в день один.
    """
    params = {
        "owner_telegram_id": owner_telegram_id,
        "day_start": day_start.isoformat(),
        "asked_before": asked_before.isoformat(),
        "question_since": question_since.isoformat(),
        "quiet_since": quiet_since.isoformat(),
    }
    rows = await ask(lambda: db.rpc(UNDATED_TO_ASK_FUNCTION, params).execute().data)
    if rows is None:
        return None
    if not isinstance(rows, list):
        raise DatabaseError("База вернула не список дел без срока.")
    if len(rows) > 1:
        raise DatabaseError(f"База вернула {len(rows)} дел без срока вместо одного.")
    return _undated_from_row(rows[0]) if rows else None


async def record_ask(
    db: Client,
    *,
    owner_telegram_id: int,
    task_id: str,
    question: str,
    telegram_message_id: int,
) -> TaskDetails | None:
    """Записать ушедший вопрос о деле без срока (§19.4) — одной транзакцией.

    Порядок «отправить → записать», как у напоминания: зовётся, когда
    Telegram сообщение принял. Открытый вопрос задачи — `question`, у
    остальных задач владельца снят; строка `ask` помнит время и сообщение.
    `None` — задача чужая, закрыта, убрана, получила срок или это не задача:
    ничего не записано.
    """
    params = {
        "owner_telegram_id": owner_telegram_id,
        "task_id": task_id,
        "question": question,
        "telegram_message_id": telegram_message_id,
    }
    return _task_or_none(await ask(lambda: db.rpc(RECORD_ASK_FUNCTION, params).execute().data))


@dataclass(frozen=True, slots=True)
class OverdueTask:
    """Задача, срок которой прошёл, а о ней пора спросить (§22.1).

    `due_at` и `due_precision` — прошедший срок: вопрос называет «вчера» или
    дату. `asked_at` — когда бот спрашивал о нынешнем сроке в последний раз;
    `None` — не спрашивал или спрашивал до переноса, и вопрос будет первым.
    """

    task_id: str
    title: str
    due_at: datetime
    due_precision: str | None
    asked_at: datetime | None


def _overdue_from_row(row: Any) -> OverdueTask:
    """Разобрать строку. Неполная — отказ, а не вопрос без сути или срока."""
    if not isinstance(row, Mapping):
        raise DatabaseError("База вернула не строку прошедшего дела.")
    try:
        precision = row["due_precision"]
        return OverdueTask(
            task_id=str(row["task_id"]),
            title=str(row["title"]),
            due_at=moment(row["due_at"], "due_at"),
            due_precision=None if precision is None else str(precision),
            asked_at=optional_moment(row["asked_at"], "asked_at"),
        )
    except KeyError as error:
        raise DatabaseError(f"В ответе базы нет поля прошедшего дела: {error}.") from error


async def overdue_to_ask(
    db: Client,
    *,
    owner_telegram_id: int,
    day_start: datetime,
    asked_before: datetime,
    question_since: datetime,
    quiet_since: datetime | None,
) -> OverdueTask | None:
    """О каком прошедшем деле спросить сейчас (§22.1, §22.4).

    Границы считает бот (`services/overdue.py`), база по ним отбирает: живой
    открытый вопрос или нет 15 минут тишины — `None`; иначе первое по
    порядку §22.1 дело. `quiet_since = None` — тишина не проверяется: так
    спрашивает утренний план. Больше одной строки — отказ: вопрос один.
    """
    params = {
        "owner_telegram_id": owner_telegram_id,
        "day_start": day_start.isoformat(),
        "asked_before": asked_before.isoformat(),
        "question_since": question_since.isoformat(),
        "quiet_since": None if quiet_since is None else quiet_since.isoformat(),
    }
    rows = await ask(lambda: db.rpc(OVERDUE_TO_ASK_FUNCTION, params).execute().data)
    if rows is None:
        return None
    if not isinstance(rows, list):
        raise DatabaseError("База вернула не список прошедших дел.")
    if len(rows) > 1:
        raise DatabaseError(f"База вернула {len(rows)} прошедших дел вместо одного.")
    return _overdue_from_row(rows[0]) if rows else None


async def record_overdue_ask(
    db: Client,
    *,
    owner_telegram_id: int,
    task_id: str,
    question: str,
    telegram_message_id: int | None,
) -> TaskDetails | None:
    """Записать ушедший вопрос о прошедшем деле (§22.4) — одной транзакцией.

    Зовётся, когда Telegram сообщение принял. Открытый вопрос задачи —
    `question`, у остальных задач владельца снят; строка `overdue` помнит
    время и сообщение — у вопроса в плане сообщения нет (`None`): свайп на
    план остаётся обычным сообщением. `None` — задача чужая, закрыта,
    убрана, повторяется, срок её впереди или это не задача: ничего не
    записано.
    """
    params = {
        "owner_telegram_id": owner_telegram_id,
        "task_id": task_id,
        "question": question,
        "telegram_message_id": telegram_message_id,
    }
    return _task_or_none(
        await ask(lambda: db.rpc(RECORD_OVERDUE_ASK_FUNCTION, params).execute().data)
    )
