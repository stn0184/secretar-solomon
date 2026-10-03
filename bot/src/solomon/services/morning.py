"""Утренний план — чистые функции (`techspec/20-morning-plan.md`).

Здесь решается всё, что не требует ни базы, ни сети: открыто ли окно
плана, какой сегодня день у владельца и где его границы, какими строками
назвать дела дня. Сам шаг тика — в `services/reminders.py`, рядом с
напоминаниями: план уходит тем же минутным циклом (§20.2).

Окно и предел строк — константы, а не настройка (§20.5). Модуль
импортирует только `texts.py`, `services/asks.py` (день и полночь — одни
с вопросом о деле без срока), `services/parts.py` (часть дня, §21.4) и
строку дела из `db/morning.py`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo

from solomon import texts
from solomon.db.morning import DayTask
from solomon.services import asks, parts

# Окно по поясу владельца (§20.2): с 08:00 и до 12:00, не включая 12:00.
# Бот не работал в 8:00 — план догоняет первым тиком, но к обеду это уже не
# план на день, и с 12:00 сегодняшний не уходит.
WINDOW_START = time(8, 0)
WINDOW_END = time(12, 0)
# Строк дел в плане не больше этого; остальные — одной строкой «И ещё N».
LINE_LIMIT = 20


@dataclass(frozen=True, slots=True)
class DayBounds:
    """Сегодняшний день владельца и его границы для `day_tasks` (§20.4).

    `day` — дата по поясу владельца: по ней процесс и база помнят план.
    `day_start` и `day_end` — сегодняшняя и завтрашняя полночь, моменты с
    поясом; дело входит в план, если `day_start <= due_at < day_end`.
    """

    day: date
    day_start: datetime
    day_end: datetime


def in_window(now: datetime, timezone: tzinfo) -> bool:
    """Открыто ли окно плана: с 08:00 до 12:00 по поясу владельца."""
    clock = now.astimezone(timezone).time()
    return WINDOW_START <= clock < WINDOW_END


def day_bounds(now: datetime, timezone: tzinfo) -> DayBounds:
    """День владельца и его полуночи — те же, что у вопроса (`services/asks.py`)."""
    today = asks.local_day(now, timezone)
    return DayBounds(
        day=today,
        day_start=asks.midnight(today, timezone),
        day_end=asks.midnight(today + timedelta(days=1), timezone),
    )


def _when(task: DayTask, timezone: tzinfo) -> str | None:
    """Метка строки: час у срока со временем, слово у части дня; у дела на день — нет."""
    if task.has_time:
        return texts.format_time(task.due_at.astimezone(timezone))
    if task.due_precision is not None and parts.is_part(task.due_precision):
        return texts.part_label(task.due_precision)
    return None


def plan_lines(tasks: Sequence[DayTask], timezone: tzinfo) -> list[str]:
    """Строки плана (§20.3): со временем и частью дня — по времени, затем дела на день.

    Часть дня стоит среди дел со временем по своему началу — «Утром — …»
    между 07:30 и 09:00 (§21.4); в одну минуту с часом — в порядке базы.
    Дела на день идут в порядке, в каком их отдала база, — порядке записи
    (§20.1). Время — в поясе владельца. Строк больше `LINE_LIMIT` — первые
    `LINE_LIMIT` и последней строкой, сколько не вошло.
    """
    marked = [(task, _when(task, timezone)) for task in tasks]
    timed = sorted(
        ((task, when) for task, when in marked if when is not None),
        key=lambda pair: pair[0].due_at,
    )
    lines = [texts.morning_line(when, task.title) for task, when in timed]
    lines += [
        texts.morning_line(texts.MORNING_ALL_DAY, task.title)
        for task, when in marked
        if when is None
    ]
    if len(lines) > LINE_LIMIT:
        return [*lines[:LINE_LIMIT], texts.morning_more(len(lines) - LINE_LIMIT)]
    return lines


def plan_text(tasks: Sequence[DayTask], timezone: tzinfo) -> str:
    """Текст плана целиком; дел нет — «Доброе утро! На сегодня дел нет.»."""
    return texts.morning_plan(plan_lines(tasks, timezone))
