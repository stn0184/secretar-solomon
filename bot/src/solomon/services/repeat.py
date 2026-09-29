"""Правило повтора от модели: форма, запись и раз в кнопке (`techspec/13-repeat.md`).

Форму правила (§13.2) держит база — `repeat_valid` в проверке таблицы. Бот
проверяет её сам до записи: правило не по форме не должно ронять запись
целиком (инвариант 5). Задача тогда пишется разовой, с пометкой
«Перепроверьте» и причиной (§13.5). Час серии (`time`) модель не отдаёт —
его ставит база из срока, поэтому здесь его нет.

Следующий раз считает база (`repeat_next`): правило живёт в одном месте, и
его проверяют тесты PGlite, а не этот модуль.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from solomon import texts
from solomon.services.understanding import Repeat

EVERY = ("day", "week", "month", "year")
MAX_INTERVAL = 99
# Последний день месяца — только у месячного правила (§13.2).
LAST_DAY = -1
# Ключи правила без часа серии: по ним правило модели сравнивается с тем,
# что лежит в базе (§13.5).
RULE_KEYS = ("every", "interval", "month_day", "month")
# Длина месяцев в високосный год: «каждый год 29 февраля» — законное правило,
# в обычный год его раз — 28-е.
MONTH_DAYS = (31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


def clean_rule(rule: Repeat) -> dict[str, Any] | None:
    """Правило в каноническом виде §13.2 без `time`; не по форме — `None`.

    Дни недели — по порядку и без повторов, неположенные поля — пусты.
    Пустой список дней у дней, месяцев и лет — это «нет дней», а не ошибка:
    модель отдаёт каждое поле схемы.
    """
    every = rule.every
    if every not in EVERY or not 1 <= rule.interval <= MAX_INTERVAL:
        return None

    weekdays = sorted(set(rule.weekdays))
    if every == "week":
        if not weekdays or any(not 1 <= day <= 7 for day in weekdays):
            return None
    elif weekdays:
        return None

    month_day = rule.month_day
    if every in ("month", "year"):
        if month_day is None:
            return None
        if not (1 <= month_day <= 31 or (every == "month" and month_day == LAST_DAY)):
            return None
    elif month_day is not None:
        return None

    month = rule.month
    if every == "year":
        if month is None or not 1 <= month <= 12:
            return None
        if month_day is not None and month_day > MONTH_DAYS[month - 1]:
            return None
    elif month is not None:
        return None

    return {
        "every": every,
        "interval": rule.interval,
        "weekdays": weekdays if every == "week" else None,
        "month_day": month_day,
        "month": month,
    }


def same_rule(stored: Mapping[str, Any] | None, rule: Mapping[str, Any]) -> bool:
    """То же ли правило, без часа серии: он в базе, а у модели его нет (§13.5).

    Пустые дни недели — `null` или пустой список — одно и то же.
    """
    if stored is None:
        return False
    if any(stored.get(key) != rule.get(key) for key in RULE_KEYS):
        return False
    return sorted(stored.get("weekdays") or []) == sorted(rule.get("weekdays") or [])


def series_precision(rule: Mapping[str, Any]) -> str:
    """Точность раза серии: есть час серии — `time`, нет — `day` (§13.3)."""
    return "time" if rule.get("time") else "day"


@dataclass(frozen=True, slots=True)
class RuleOutcome:
    """Что делать с правилом модели при записи задачи (§13.5).

    `rule` — правило для базы или пусто (разовая задача); `malformed` —
    модель отдала правило не по форме, и человеку нужна пометка с причиной.
    """

    rule: dict[str, Any] | None
    malformed: bool = False


# Правила нет и не было: разовая задача без пометки.
NO_RULE = RuleOutcome(rule=None)


def record_rule(kind: str, due_at: datetime | None, rule: Repeat | None) -> RuleOutcome:
    """Правило для записи: у задачи со сроком — по форме или с пометкой.

    У идеи, желания и задачи без срока повторять нечего: правило
    отбрасывается молча — первого раза нет, и вопрос о нём задаёт сама
    модель (§13.1).
    """
    if rule is None or kind != "task" or due_at is None:
        return RuleOutcome(rule=None)
    cleaned = clean_rule(rule)
    if cleaned is None:
        return RuleOutcome(rule=None, malformed=True)
    return RuleOutcome(rule=cleaned)


def malformed_reason(reason: str | None) -> str:
    """Причина пометки: своя причина модели — первой, потом «не разобрал повтор»."""
    own = (reason or "").strip().rstrip(".").strip()
    if not own:
        return texts.REPEAT_DROPPED
    return f"{own}. {texts.REPEAT_DROPPED}"


def occurrence_seconds(moment: datetime) -> int:
    """Раз в секундах Unix — как его сравнивает база (§13.3): доли секунды отброшены."""
    return math.floor(moment.timestamp())


def moment_of(seconds: int) -> datetime:
    """Раз из секунд Unix — обратно в момент (UTC)."""
    return datetime.fromtimestamp(seconds, tz=UTC)
