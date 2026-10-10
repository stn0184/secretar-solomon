"""Часы по сферам из базы (`techspec/31-hours.md`, `techspec/03-schema.md` §3.18).

Считает функция базы `sphere_hours` — одна на бота и приложение
(`techspec/32-redesign.md` §32.3): встречи по длительности, сессии переписки,
пересечение внутри сферы — один раз. Бот передаёт окно и владельца, база
отдаёт минуты сфер. Окна и слова — `services/hours.py`.

Тот же закон, что у `tasks.py`: `owner_telegram_id` именованный и без
значения по умолчанию — ключ service-role правила доступа обходит, поэтому
владельца называет код (`techspec/04-access.md` §4.3); функция без него
отказывает.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from supabase import Client

from solomon.db.rpc import DatabaseError, ask

SPHERE_HOURS_FUNCTION = "sphere_hours"


@dataclass(frozen=True, slots=True)
class SphereMinutes:
    """Минуты сферы за окно: встречи и переписка. `sphere_id` и `name` пусты —
    «без сферы»."""

    sphere_id: str | None
    name: str | None
    meetings: int
    chat: int


def _minutes(row: Mapping[str, Any], field: str) -> int:
    value = row.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DatabaseError(f"В ответе базы не разобрать минуты {field}: {value!r}.")
    return value


def _row(row: Any) -> SphereMinutes:
    """Строка ответа; не строка или без минут — отказ."""
    if not isinstance(row, Mapping):
        raise DatabaseError("База вернула не строку часов сферы.")
    sphere_id = row.get("sphere_id")
    name = row.get("name")
    return SphereMinutes(
        sphere_id=None if sphere_id is None else str(sphere_id),
        name=None if name is None else str(name),
        meetings=_minutes(row, "meetings"),
        chat=_minutes(row, "chat"),
    )


async def sphere_hours(
    db: Client, *, owner_telegram_id: int, start: datetime, end: datetime
) -> list[SphereMinutes]:
    """Минуты сфер владельца за окно `[start, end)` — только сферы с минутами,
    в порядке заведения, «без сферы» последней. Правый край база сама держит
    не позже «сейчас»."""
    params = {
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "owner_telegram_id": owner_telegram_id,
    }
    data = await ask(lambda: db.rpc(SPHERE_HOURS_FUNCTION, params).execute().data)
    if data is None:
        return []
    if not isinstance(data, list):
        raise DatabaseError("База вернула не список часов сфер.")
    return [_row(row) for row in data]
