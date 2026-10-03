"""Часть дня — чистые функции (`techspec/21-part-of-day.md`).

«Утром», «днём», «вечером» без часа бот запоминает частью дня, а не
придуманным часом. Модель называет часть и день; час ставит бот: в
`due_at` ложится начало части того же дня по поясу владельца (§21.2).

Часы частей в боте живут только здесь — константы, а не настройка
(§21.1). Модуль работает на примитивах и ничего своего не импортирует:
его зовёт разбор (`services/understanding.py`), а не наоборот.
"""

from __future__ import annotations

from datetime import date, datetime, time, tzinfo

MORNING = "morning"
AFTERNOON = "afternoon"
EVENING = "evening"
# Части в порядке суток.
PARTS = (MORNING, AFTERNOON, EVENING)
# Начало каждой части по местным часам владельца: утро — до 12:00, день —
# до 18:00, вечер — до полуночи.
PART_STARTS = {MORNING: time(8, 0), AFTERNOON: time(12, 0), EVENING: time(18, 0)}
# Срок со временем — точность, которой становится часть вместе с повтором.
TIME_PRECISION = "time"


def is_part(precision: str | None) -> bool:
    """Часть ли дня эта точность срока."""
    return precision in PART_STARTS


def part_start(day: date, part: str, timezone: tzinfo) -> datetime:
    """Начало части этого дня — момент с поясом владельца."""
    return datetime.combine(day, PART_STARTS[part], tzinfo=timezone)


def settle(
    due_at: datetime | None,
    precision: str | None,
    *,
    repeating: bool,
    timezone: tzinfo,
) -> tuple[datetime | None, str | None]:
    """Срок модели, каким его запишет бот (§21.2): `(due_at, due_precision)`.

    У части — начало этой части того же дня: час модели не в счёт. День
    берётся по поясу владельца; момент без пояса — день как назван. Часть
    вместе с повтором — срок со временем с моментом модели: правило без
    часа съехало бы на 18:00 (§21.6). Остальное и часть без срока — как
    есть.
    """
    if due_at is None or precision is None or not is_part(precision):
        return due_at, precision
    if repeating:
        return due_at, TIME_PRECISION
    day = due_at.date() if due_at.tzinfo is None else due_at.astimezone(timezone).date()
    return part_start(day, precision, timezone), precision
