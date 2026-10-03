"""Утренний план: был ли сегодня, дела дня и запись ушедшего плана.

Закон тот же, что у `reminders.py`: `owner_telegram_id` именованный и без
значения по умолчанию в каждой функции, и он же уходит в SQL-функцию —
ключ service-role правила доступа обходит, поэтому разделение по владельцу
держит код (`techspec/04-access.md` §4.3).

Границы дня и день владельца считает бот (`services/morning.py`), база по
ним отбирает и помнит (`techspec/20-morning-plan.md` §20.4).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from supabase import Client

from solomon.db.rpc import DatabaseError, ask, moment
from solomon.db.tasks import TIME_PRECISION

MORNING_PLAN_SENT_FUNCTION = "morning_plan_sent"
DAY_TASKS_FUNCTION = "day_tasks"
RECORD_MORNING_PLAN_FUNCTION = "record_morning_plan"


@dataclass(frozen=True, slots=True)
class DayTask:
    """Дело со сроком на сегодня — строка плана (§20.1).

    `due_precision` — `time` у срока со временем; у срока на день — `day`
    (в `due_at` тогда лежит 18:00, §3.3).
    """

    task_id: str
    title: str
    due_at: datetime
    due_precision: str | None

    @property
    def has_time(self) -> bool:
        """Срок со временем — строка «09:00 — …»; иначе «В течение дня — …»."""
        return self.due_precision == TIME_PRECISION


def _answer(data: Any, what: str) -> bool:
    """Ответ «да или нет»: не `bool` — отказ, а не догадка."""
    if not isinstance(data, bool):
        raise DatabaseError(f"База не ответила, {what}: {data!r}.")
    return data


def _day_task_from_row(row: Any) -> DayTask:
    """Разобрать строку. Неполная — отказ, а не строка плана без сути."""
    if not isinstance(row, Mapping):
        raise DatabaseError("База вернула не строку дела дня.")
    try:
        title = row["title"]
        due_at = row["due_at"]
        precision = row["due_precision"]
        task_id = row["task_id"]
    except KeyError as error:
        raise DatabaseError(f"В ответе базы нет поля дела дня: {error}.") from error
    if title is None or due_at is None:
        raise DatabaseError("База вернула дело дня без сути или без срока.")
    return DayTask(
        task_id=str(task_id),
        title=str(title),
        due_at=moment(due_at, "due_at"),
        due_precision=None if precision is None else str(precision),
    )


async def morning_plan_sent(db: Client, *, owner_telegram_id: int, day: date) -> bool:
    """Был ли у владельца план за этот день (§20.4, шаг 1)."""
    params = {"owner_telegram_id": owner_telegram_id, "day": day.isoformat()}
    data = await ask(lambda: db.rpc(MORNING_PLAN_SENT_FUNCTION, params).execute().data)
    return _answer(data, "был ли утренний план")


async def day_tasks(
    db: Client, *, owner_telegram_id: int, day_start: datetime, day_end: datetime
) -> list[DayTask]:
    """Активные задачи владельца со сроком от `day_start` до `day_end` (§20.1).

    Порядок — как отдала база: `due_at`, `created_at`, `id`. Сначала дела
    со временем, затем на день, бот раскладывает сам (`services/morning.py`).
    """
    params = {
        "owner_telegram_id": owner_telegram_id,
        "day_start": day_start.isoformat(),
        "day_end": day_end.isoformat(),
    }
    rows = await ask(lambda: db.rpc(DAY_TASKS_FUNCTION, params).execute().data)
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise DatabaseError("База вернула не список дел дня.")
    return [_day_task_from_row(row) for row in rows]


async def record_morning_plan(
    db: Client, *, owner_telegram_id: int, day: date, telegram_message_id: int
) -> bool:
    """Записать ушедший план (§20.4, шаг 4).

    Порядок «отправить → записать»: зовётся, когда Telegram сообщение принял.
    `False` — план за этот день уже записан, ничего не изменилось.
    """
    params = {
        "owner_telegram_id": owner_telegram_id,
        "day": day.isoformat(),
        "telegram_message_id": telegram_message_id,
    }
    data = await ask(lambda: db.rpc(RECORD_MORNING_PLAN_FUNCTION, params).execute().data)
    return _answer(data, "записан ли утренний план")
