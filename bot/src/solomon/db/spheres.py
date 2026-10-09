"""Сферы жизни владельца: чтение списка со знаниями (`techspec/30-spheres.md`).

Сферы заводит и убирает `record_understanding` (`db/tasks.py`, одной
транзакцией с разбором), сферу дела меняет `edit_from_chat`, сферу чата —
разбор чата (`db/chats.py`). Боту здесь нужно одно чтение: живые сферы со
знаниями — записями памяти со сферой и статусом `fact`. Список уходит в
промпт разбора, разбора чатов и поиска (§30.2), по нему же бот узнаёт
сферу, названную моделью.

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

SPHERES_TABLE = "spheres"
FACTS_TABLE = "facts"
FACT_STATUS = "fact"
# Сколько знаний читается: в промпт уходит до 2000 знаков на все сферы
# (§30.2) — обрезает бот, чтение берёт с запасом.
KNOWLEDGE_LIMIT = 100


@dataclass(frozen=True, slots=True)
class Sphere:
    """Живая сфера владельца и что о ней известно — тексты записей памяти,
    старые первыми."""

    id: str
    name: str
    facts: tuple[str, ...] = ()


def _field(row: Any, name: str) -> str:
    """Поле строки текстом; строки или поля нет — отказ."""
    if not isinstance(row, Mapping) or row.get(name) is None:
        raise DatabaseError(f"В ответе базы нет поля сферы: {name}.")
    return str(row[name])


async def list_spheres(
    db: Client, *, owner_telegram_id: int, limit: int = KNOWLEDGE_LIMIT
) -> list[Sphere]:
    """Живые сферы владельца по времени заведения, у каждой — знания.

    Знания — записи памяти со сферой и статусом `fact`, свежие из
    `limit` последних, внутри сферы — от старых к новым. Предположения
    (`guess`) в промпт не попадают (§8.1), как и в блоке «что известно».
    """
    spheres = await ask(
        lambda: (
            db.table(SPHERES_TABLE)
            .select("id, name")
            .eq("owner_telegram_id", owner_telegram_id)
            .is_("removed_at", "null")
            .order("created_at", desc=False)
            .execute()
            .data
        )
    )
    if spheres is None:
        return []
    if not isinstance(spheres, list):
        raise DatabaseError("База вернула не список сфер.")
    found = [(_field(row, "id"), _field(row, "name")) for row in spheres]
    if not found:
        return []
    facts = await ask(
        lambda: (
            db.table(FACTS_TABLE)
            .select("sphere_id, text")
            .eq("owner_telegram_id", owner_telegram_id)
            .eq("status", FACT_STATUS)
            .not_.is_("sphere_id", "null")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
            .data
        )
    )
    if not isinstance(facts, list):
        raise DatabaseError("База вернула не список знаний сфер.")
    known: dict[str, list[str]] = {sphere_id: [] for sphere_id, _ in found}
    for row in reversed(facts):
        sphere_id = _field(row, "sphere_id")
        if sphere_id in known:
            known[sphere_id].append(_field(row, "text"))
    return [
        Sphere(id=sphere_id, name=name, facts=tuple(known[sphere_id])) for sphere_id, name in found
    ]
