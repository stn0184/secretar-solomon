"""Напоминания: что созрело, что уже отправлено и что закрыто кнопкой.

Тот же закон, что у `tasks.py`: `owner_telegram_id` именованный и без
значения по умолчанию в каждой функции, и он же уходит в SQL-функцию —
ключ service-role правила доступа обходит, поэтому разделение по владельцу
держит код (`techspec/04-access.md` §4.3).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from supabase import Client

from solomon.db.rpc import DatabaseError, ask, moment, single_row
from solomon.db.tasks import Task, task_from_row

DUE_REMINDERS_FUNCTION = "due_reminders"
MARK_REMINDERS_SENT_FUNCTION = "mark_reminders_sent"
MARK_TASK_DONE_FUNCTION = "mark_task_done"


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
