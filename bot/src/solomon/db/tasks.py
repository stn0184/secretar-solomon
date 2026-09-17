"""Входящие сообщения и задачи: запись и чтение.

Бот ходит в базу ключом service-role — правила доступа его не ограничивают,
поэтому разделение по владельцу держит этот слой: `owner_telegram_id` есть в
каждой сигнатуре, он именованный и без значения по умолчанию, так что вызов
«забыл владельца» не собирается (`techspec/04-access.md` §4.3).

Клиент Supabase синхронный, а бот асинхронный: запрос уходит в отдельный
поток — иначе на время ответа базы встал бы весь long polling.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from supabase import Client

RECORD_TASK_FUNCTION = "record_task"
TASKS_TABLE = "tasks"
ACTIVE_STATUS = "active"
TASK_COLUMNS = "id, title, status"


class DatabaseError(Exception):
    """База не ответила или ответила не тем.

    Клиент Supabase поднимает свои исключения на любой мелочи — от разрыва
    сети до отказа PostgREST. Наружу они не текут: слой выше получает один
    понятный тип и переводит его в слова для человека.
    """


@dataclass(frozen=True, slots=True)
class Task:
    """Строка `tasks` в том виде, в каком её читает бот."""

    id: str
    title: str
    status: str


async def _ask(call: Callable[[], Any]) -> Any:
    """Сходить в базу из отдельного потока; любой отказ — свой тип."""
    try:
        return await asyncio.to_thread(call)
    except Exception as error:  # отказ клиента превращается в DatabaseError, а не в трассировку
        raise DatabaseError(f"{type(error).__name__}: {error}") from error


def _task_from_row(row: Any) -> Task:
    """Разобрать строку ответа. Неполная строка — отказ, а не полупустая задача."""
    if not isinstance(row, Mapping):
        raise DatabaseError("База вернула не строку задачи.")
    try:
        return Task(id=str(row["id"]), title=str(row["title"]), status=str(row["status"]))
    except KeyError as error:
        raise DatabaseError(f"В ответе базы нет поля задачи: {error}.") from error


async def record_task(
    db: Client,
    *,
    owner_telegram_id: int,
    chat_id: int,
    telegram_message_id: int,
    text: str,
) -> Task:
    """Записать сообщение и задачу одной транзакцией.

    Зовётся SQL-функция `record_task` (`techspec/03-schema.md` §3.4): две
    вставки подряд оставили бы сообщение без задачи, если между ними откажет
    база. Повтор того же обновления новых строк не пишет и возвращает уже
    заведённую задачу.
    """
    params = {
        "owner_telegram_id": owner_telegram_id,
        "chat_id": chat_id,
        "telegram_message_id": telegram_message_id,
        "text": text,
    }
    data = await _ask(lambda: db.rpc(RECORD_TASK_FUNCTION, params).execute().data)
    # PostgREST отдаёт составную строку объектом, но на всякий случай
    # принимается и список из одной строки.
    if isinstance(data, list):
        data = data[0] if data else None
    if data is None:
        raise DatabaseError("База не вернула задачу.")
    return _task_from_row(data)


async def list_active_tasks(db: Client, *, owner_telegram_id: int, limit: int) -> list[Task]:
    """Активные задачи владельца, новые сверху."""
    rows = await _ask(
        lambda: (
            db.table(TASKS_TABLE)
            .select(TASK_COLUMNS)
            .eq("owner_telegram_id", owner_telegram_id)
            .eq("status", ACTIVE_STATUS)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
            .data
        )
    )
    if not isinstance(rows, list):
        raise DatabaseError("База вернула не список задач.")
    return [_task_from_row(row) for row in rows]
