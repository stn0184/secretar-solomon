"""Часы по сферам (`techspec/31-hours.md`): окна и слова блока «Часы по сферам».

Считает база — функция `sphere_hours` (`techspec/03-schema.md` §3.18), одна на
бота и приложение (`techspec/32-redesign.md` §32.3): встречи по длительности,
сессии переписки, пересечение внутри сферы — один раз и встречей, не прошедшее
не считается. Здесь — только обвязка: какие окна спросить и как сказать.

Окна (§31.2) — сегодня, вчера и неделя с понедельника по поясу владельца, как
дни напоминаний (`techspec/06-reminders.md` §6.1); правый край — «сейчас».
Спрашивает базу `services/tasks.py`, в промпт строки кладёт
`services/understanding.py`. Сети и базы здесь нет.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from solomon import texts
from solomon.db.hours import SphereMinutes


@dataclass(frozen=True, slots=True)
class SphereHours:
    """Часы сферы за окно в минутах: `name` — название, `None` — без сферы."""

    name: str | None
    meetings: int
    chat: int

    @property
    def total(self) -> int:
        return self.meetings + self.chat


@dataclass(frozen=True, slots=True)
class Period:
    """Окно и часы сфер в нём — только сферы, у которых что-то набралось."""

    label: str
    spheres: tuple[SphereHours, ...]


@dataclass(frozen=True, slots=True)
class Window:
    """Окно блока: подпись и `[start, end)` по поясу владельца."""

    label: str
    start: datetime
    end: datetime


def _midnight(day: date, timezone: ZoneInfo) -> datetime:
    return datetime.combine(day, time(0, 0), tzinfo=timezone)


def windows(now: datetime, timezone: ZoneInfo) -> list[Window]:
    """Сегодня, вчера и неделя с понедельника — по поясу владельца (§31.2).
    В понедельник «вчера» — прошлое воскресенье, а неделя — сегодня."""
    local = now.astimezone(timezone)
    day = local.date()
    yesterday = day - timedelta(days=1)
    monday = day - timedelta(days=day.weekday())
    today_start = _midnight(day, timezone)
    return [
        Window(f"Сегодня, {texts.format_day(local)}", today_start, local),
        Window(
            f"Вчера, {texts.format_day(_midnight(yesterday, timezone))}",
            _midnight(yesterday, timezone),
            today_start,
        ),
        Window(
            f"Неделя с понедельника, {texts.format_date(_midnight(monday, timezone))}",
            _midnight(monday, timezone),
            local,
        ),
    ]


def period(window: Window, rows: Iterable[SphereMinutes]) -> Period:
    """Окно с часами сфер из ответа базы — в её порядке: сферы по заведению,
    «без сферы» последней; сфера без минут в окно не попадает."""
    found = tuple(SphereHours(row.name, row.meetings, row.chat) for row in rows)
    return Period(window.label, tuple(item for item in found if item.total > 0))


def hours_words(minutes: int) -> str:
    """«2 ч 10 мин», «45 мин», «3 ч»."""
    whole, rest = divmod(minutes, 60)
    if whole and rest:
        return f"{whole} ч {rest} мин"
    if whole:
        return f"{whole} ч"
    return f"{rest} мин"


def _sphere_words(item: SphereHours) -> str:
    """«VoiceFin — 2 ч 10 мин (встречи 2 ч, переписка 10 мин)»: части — только
    те, что есть."""
    parts = []
    if item.meetings:
        parts.append(f"встречи {hours_words(item.meetings)}")
    if item.chat:
        parts.append(f"переписка {hours_words(item.chat)}")
    name = item.name if item.name is not None else texts.NO_SPHERE
    return f"{name} — {hours_words(item.total)} ({', '.join(parts)})"


def lines(periods: Sequence[Period]) -> list[str]:
    """Строки блока «Часы по сферам» (§31.2): окно, сферы с разбивкой и итог.
    Пустое окно — «ни встреч, ни переписки»."""
    found = []
    for item in periods:
        if not item.spheres:
            found.append(f"{item.label}: ни встреч, ни переписки.")
            continue
        said = "; ".join(_sphere_words(sphere) for sphere in item.spheres)
        total = sum(sphere.total for sphere in item.spheres)
        found.append(f"{item.label}: {said}. Всего {hours_words(total)}.")
    return found
