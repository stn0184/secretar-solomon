"""Память о пользователе: чтение известных фактов для промпта разбора.

Записи пишет `record_understanding` (`db/tasks.py`, одной транзакцией с
задачей), подтверждает и удаляет — Mini App под правилами доступа. Боту
здесь нужно одно: список фактов владельца, который уходит в промпт, чтобы
помощник не переспрашивал (`techspec/08-memory.md` §8.2).

Тот же закон, что у `tasks.py`: `owner_telegram_id` именованный и без
значения по умолчанию — ключ service-role правила доступа обходит, поэтому
разделение по владельцу держит код (`techspec/04-access.md` §4.3).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from supabase import Client

from solomon.db.rpc import DatabaseError, ask

FACTS_TABLE = "facts"
FACT_COLUMNS = "id, category, text, status"
FACT_STATUS = "fact"
# Сколько известных фактов уходит в промпт (§5.2, §8.2).
KNOWN_LIMIT = 50


@dataclass(frozen=True, slots=True)
class Fact:
    """Строка `facts` в том виде, в каком её читает бот (§3.7)."""

    id: str
    category: str
    text: str
    status: str


def fact_from_row(row: Any) -> Fact:
    """Разобрать строку. Неполная — отказ, а не запись без текста."""
    if not isinstance(row, Mapping):
        raise DatabaseError("База вернула не строку памяти.")
    try:
        return Fact(
            id=str(row["id"]),
            category=str(row["category"]),
            text=str(row["text"]),
            status=str(row["status"]),
        )
    except KeyError as error:
        raise DatabaseError(f"В ответе базы нет поля записи памяти: {error}.") from error


async def list_facts(
    db: Client,
    *,
    owner_telegram_id: int,
    status: str = FACT_STATUS,
    limit: int = KNOWN_LIMIT,
) -> list[Fact]:
    """Записи владельца с этим статусом, старые первыми, не больше `limit`.

    По умолчанию — только факты: предположения в промпт не попадают
    (§8.1), пока человек их не подтвердил в приложении. Порядок по
    `created_at` — чтобы список в промпте был одним и тем же от разбора
    к разбору.
    """
    rows = await ask(
        lambda: (
            db.table(FACTS_TABLE)
            .select(FACT_COLUMNS)
            .eq("owner_telegram_id", owner_telegram_id)
            .eq("status", status)
            .order("created_at", desc=False)
            .limit(limit)
            .execute()
            .data
        )
    )
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise DatabaseError("База вернула не список записей памяти.")
    return [fact_from_row(row) for row in rows]
