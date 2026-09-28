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
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, get_args

from supabase import Client

from solomon.db.rpc import DatabaseError, ask, moment, single_row
from solomon.db.tasks import Task, TaskDetails, task_details_from_row, task_from_row

DUE_REMINDERS_FUNCTION = "due_reminders"
MARK_REMINDERS_SENT_FUNCTION = "mark_reminders_sent"
MARK_TASK_DONE_FUNCTION = "mark_task_done"
REOPEN_TASK_FUNCTION = "reopen_task"
REMINDER_PLAN_FUNCTION = "reminder_plan"
SAVE_OWNER_TIMEZONE_FUNCTION = "save_owner_timezone"
MOVED_TASKS_FUNCTION = "moved_tasks"
CLEAR_DUE_MOVED_FUNCTION = "clear_due_moved"

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
    `due_moved_at` — прочитанная отметка: снимается ровно она.
    """

    id: str
    title: str
    due_at: datetime | None
    due_precision: str | None
    due_moved_at: datetime
    next_fire_at: datetime | None


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
    них, и второй поход в базу на каждое напоминание был бы лишним.
    """

    id: str
    task_id: str
    stage: str
    fire_at: datetime
    title: str
    due_at: datetime | None
    due_precision: str | None


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


async def mark_task_done(db: Client, *, owner_telegram_id: int, task_id: str) -> Task | None:
    """Закрыть задачу и снять её неотправленные напоминания одной транзакцией.

    `None` — задачи нет или она чужая: база сверяет владельца сама, поэтому
    подставленный в callback чужой `task_id` не закрывает ничего (§6.3).
    """
    params = {"owner_telegram_id": owner_telegram_id, "task_id": task_id}
    data = single_row(await ask(lambda: db.rpc(MARK_TASK_DONE_FUNCTION, params).execute().data))
    if data is None or (isinstance(data, Mapping) and data.get("id") is None):
        return None
    return task_from_row(data)


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
    data = single_row(await ask(lambda: db.rpc(REOPEN_TASK_FUNCTION, params).execute().data))
    if data is None or (isinstance(data, Mapping) and data.get("id") is None):
        return None
    return task_details_from_row(data)
