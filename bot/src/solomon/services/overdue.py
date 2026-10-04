"""Вопрос о прошедшем деле — чистые функции (`techspec/22-overdue.md`).

Здесь решается всё, что не требует ни базы, ни сети: открыто ли окно,
границы отбора `overdue_to_ask` для утреннего плана и для шага тика и
какими словами спросить. Сами вопросы задают `_send_plan` и шаг тика в
`services/reminders.py` (§22.2).

День, полночь, тишина и неделя — те же, что у вопроса о деле без срока
(`services/asks.py`); окно своё. Всё это константы, а не настройка (§22.6).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, tzinfo

from solomon import texts
from solomon.db.reminders import OverdueTask
from solomon.services import asks

# Окно по поясу владельца (§22.2): с 08:00, когда приходит план, и до 20:00,
# не включая 20:00. Вечером вопрос о делах некстати — остальное завтра.
WINDOW_START = time(8, 0)
WINDOW_END = time(20, 0)


@dataclass(frozen=True, slots=True)
class OverdueBounds:
    """Границы отбора `overdue_to_ask` (§22.4), все — моменты с поясом.

    `day_start` — сегодняшняя полночь владельца: срок раньше неё прошёл.
    `asked_before` — полночь шесть дней назад: спрошенное раньше неё
    спрашивается снова. `question_since` — с какого момента открытый вопрос
    считается живым; `quiet_since` — начало 15 минут тишины, `None` — тишина
    не проверяется (так спрашивает план).
    """

    day_start: datetime
    asked_before: datetime
    question_since: datetime
    quiet_since: datetime | None


def in_window(now: datetime, timezone: tzinfo) -> bool:
    """Открыто ли окно вопроса: с 08:00 до 20:00 по поясу владельца."""
    clock = now.astimezone(timezone).time()
    return WINDOW_START <= clock < WINDOW_END


def _week(now: datetime, timezone: tzinfo) -> tuple[datetime, datetime]:
    """Сегодняшняя полночь и полночь шесть дней назад — неделя по календарю."""
    today = asks.local_day(now, timezone)
    return (
        asks.midnight(today, timezone),
        asks.midnight(today - timedelta(days=asks.REPEAT_DAYS - 1), timezone),
    )


def plan_bounds(now: datetime, timezone: tzinfo) -> OverdueBounds:
    """Границы для абзаца утреннего плана (§22.4).

    Живой вопрос — заданный сегодня: «К какому сроку?» в 07:50 план не
    перебивает, а вчерашний вопрос новый снимает. Тишины план не ждёт.
    """
    day_start, asked_before = _week(now, timezone)
    return OverdueBounds(
        day_start=day_start,
        asked_before=asked_before,
        question_since=day_start,
        quiet_since=None,
    )


def step_bounds(now: datetime, timezone: tzinfo) -> OverdueBounds:
    """Границы для отдельного вопроса шагом тика (§22.4).

    Живой вопрос — младше суток (§10.3), тишина — 15 минут, как у вопроса о
    деле без срока.
    """
    day_start, asked_before = _week(now, timezone)
    return OverdueBounds(
        day_start=day_start,
        asked_before=asked_before,
        question_since=now - asks.QUESTION_LIFE,
        quiet_since=now - asks.QUIET,
    )


def question_text(task: OverdueTask, now: datetime, timezone: tzinfo, *, more: bool) -> str:
    """Текст вопроса (§22.3): «Вчера осталось» или дата срока.

    День срока — по `due_at` в поясе владельца. `more` — сегодня процесс уже
    спрашивал о прошедшем деле: вопрос начинается с «Ещё». Спрашивал о нём
    раньше (`asked_at`) — в конце предложение убрать дело из списка.
    """
    due = task.due_at.astimezone(timezone)
    if due.date() == asks.local_day(now, timezone) - timedelta(days=1):
        question = texts.overdue_yesterday(task.title, more=more)
    else:
        question = texts.overdue_dated(task.title, texts.format_date(due), more=more)
    return question if task.asked_at is None else texts.overdue_again(question)
