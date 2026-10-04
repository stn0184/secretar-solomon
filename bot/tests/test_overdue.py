"""Вопрос о прошедшем деле — чистые функции (`techspec/22-overdue.md`).

Окно, границы отбора для плана и для шага тика и слова вопроса. Сети и базы
здесь нет: о каком деле спросить, решает `overdue_to_ask` по этим границам.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from solomon import texts
from solomon.db.reminders import OverdueTask
from solomon.services import asks, overdue
from tests.conftest import OWNER_TIMEZONE

TZ = ZoneInfo(OWNER_TIMEZONE)
# Понедельник, 5 октября 2026, 08:00 в поясе владельца (+05:00): утро плана.
NOW = datetime(2026, 10, 5, 8, 0, tzinfo=TZ)
TASK_ID = "7c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f"
TITLE = "позвонить в сервис"
AGAIN = " Если уже не нужно, скажите — уберу из списка."


def task(due_at: datetime, asked_at: datetime | None = None) -> OverdueTask:
    return OverdueTask(
        task_id=TASK_ID, title=TITLE, due_at=due_at, due_precision="time", asked_at=asked_at
    )


def test_window_is_a_constant_from_eight_to_eight() -> None:
    """Окно 08:00–20:00 — константы, а не настройка (§22.2)."""
    assert overdue.WINDOW_START == time(8, 0)
    assert overdue.WINDOW_END == time(20, 0)


def test_questions_are_constants() -> None:
    """Свой вопрос бот узнаёт по тексту — константам (§22.5)."""
    assert texts.OVERDUE_QUESTION == "Получилось?"
    assert texts.OVERDUE_MOVE_QUESTION == "На когда перенести?"


@pytest.mark.parametrize(
    ("moment", "open_"),
    [
        (datetime(2026, 10, 5, 7, 59, tzinfo=TZ), False),
        (datetime(2026, 10, 5, 8, 0, tzinfo=TZ), True),
        (datetime(2026, 10, 5, 12, 0, tzinfo=TZ), True),
        (datetime(2026, 10, 5, 19, 59, tzinfo=TZ), True),
        (datetime(2026, 10, 5, 20, 0, tzinfo=TZ), False),
        (datetime(2026, 10, 5, 23, 30, tzinfo=TZ), False),
        (datetime(2026, 10, 5, 3, 0, tzinfo=TZ), False),
    ],
)
def test_window_is_eight_to_eight_by_owner_time(moment: datetime, open_: bool) -> None:
    """С 08:00 до 20:00 по поясу владельца; вечером вопрос не догоняет."""
    assert overdue.in_window(moment, TZ) is open_


def test_window_reads_the_owner_time_not_utc() -> None:
    """03:30 UTC — уже 08:30 у владельца (+05:00): окно открыто."""
    assert overdue.in_window(datetime(2026, 10, 5, 3, 30, tzinfo=UTC), TZ) is True


def test_plan_bounds_skip_the_quiet_and_keep_today_question() -> None:
    """План (§22.4): живой вопрос — заданный сегодня, тишину план не ждёт."""
    now = NOW.replace(minute=7)
    bounds = overdue.plan_bounds(now, TZ)

    assert bounds == overdue.OverdueBounds(
        day_start=datetime(2026, 10, 5, 0, 0, tzinfo=TZ),
        asked_before=datetime(2026, 9, 29, 0, 0, tzinfo=TZ),
        question_since=datetime(2026, 10, 5, 0, 0, tzinfo=TZ),
        quiet_since=None,
    )


def test_step_bounds_wait_for_the_quiet_and_a_day_old_question() -> None:
    """Шаг тика (§22.4): живой вопрос — младше суток, тишина — 15 минут."""
    now = datetime(2026, 10, 5, 14, 20, tzinfo=TZ)
    bounds = overdue.step_bounds(now, TZ)

    assert bounds == overdue.OverdueBounds(
        day_start=datetime(2026, 10, 5, 0, 0, tzinfo=TZ),
        asked_before=datetime(2026, 9, 29, 0, 0, tzinfo=TZ),
        question_since=now - asks.QUESTION_LIFE,
        quiet_since=now - asks.QUIET,
    )
    assert bounds.quiet_since == datetime(2026, 10, 5, 14, 5, tzinfo=TZ)


def test_bounds_take_the_day_from_the_owner_time() -> None:
    """21:00 UTC 4 октября — у владельца уже 02:00 5 октября: день — 5-е."""
    now = datetime(2026, 10, 4, 21, 0, tzinfo=UTC)

    assert overdue.plan_bounds(now, TZ).day_start == datetime(2026, 10, 5, 0, 0, tzinfo=TZ)
    assert overdue.step_bounds(now, TZ).day_start == datetime(2026, 10, 5, 0, 0, tzinfo=TZ)


def test_week_is_calendar_days_like_the_undated_question() -> None:
    """Повтор через 7 дней по календарю (§22.1) — та же неделя, что в §19.1."""
    bounds = overdue.step_bounds(datetime(2026, 10, 5, 19, 59, tzinfo=TZ), TZ)

    assert bounds.day_start - bounds.asked_before == timedelta(days=asks.REPEAT_DAYS - 1)


def test_yesterday_question() -> None:
    """Срок был вчера — «Вчера осталось: …» (§22.3)."""
    due = datetime(2026, 10, 4, 18, 0, tzinfo=TZ)

    assert overdue.question_text(task(due), NOW, TZ, more=False) == (
        "Вчера осталось: позвонить в сервис. Получилось?"
    )


def test_yesterday_is_counted_in_the_owner_time() -> None:
    """20:30 UTC 3 октября — у владельца уже 01:30 4 октября, то есть вчера."""
    due = datetime(2026, 10, 3, 20, 30, tzinfo=UTC)

    assert overdue.question_text(task(due), NOW, TZ, more=False).startswith("Вчера осталось:")


def test_earlier_question_names_the_date() -> None:
    """Срок раньше вчерашнего — датой: «Срок был 2 октября: …» (§22.3)."""
    due = datetime(2026, 10, 2, 10, 0, tzinfo=TZ)

    assert overdue.question_text(task(due), NOW, TZ, more=False) == (
        "Срок был 2 октября: позвонить в сервис. Получилось?"
    )


def test_not_the_first_question_of_the_day_says_more() -> None:
    """Сегодня уже спрашивал — «Ещё вчера осталось» и «Ещё одно, срок был»."""
    yesterday = datetime(2026, 10, 4, 18, 0, tzinfo=TZ)
    earlier = datetime(2026, 10, 2, 10, 0, tzinfo=TZ)

    assert overdue.question_text(task(yesterday), NOW, TZ, more=True) == (
        "Ещё вчера осталось: позвонить в сервис. Получилось?"
    )
    assert overdue.question_text(task(earlier), NOW, TZ, more=True) == (
        "Ещё одно, срок был 2 октября: позвонить в сервис. Получилось?"
    )


@pytest.mark.parametrize("more", [False, True])
def test_repeated_question_offers_to_remove(more: bool) -> None:
    """Спрашивал неделю назад — в конце «Если уже не нужно, скажите — уберу из списка.»."""
    due = datetime(2026, 9, 25, 10, 0, tzinfo=TZ)
    asked = datetime(2026, 9, 28, 8, 0, tzinfo=TZ)

    text = overdue.question_text(task(due, asked_at=asked), NOW, TZ, more=more)

    start = "Ещё одно, срок был" if more else "Срок был"
    assert text == f"{start} 25 сентября: позвонить в сервис. Получилось?{AGAIN}"


def test_title_goes_word_for_word() -> None:
    """Суть — `tasks.title` дословно, модель не зовётся (§22.3)."""
    due = datetime(2026, 10, 4, 9, 0, tzinfo=TZ)
    odd = OverdueTask(
        task_id=TASK_ID,
        title="отправить расчёт Ольге (v2)",
        due_at=due,
        due_precision=None,
        asked_at=None,
    )

    assert overdue.question_text(odd, NOW, TZ, more=False) == (
        "Вчера осталось: отправить расчёт Ольге (v2). Получилось?"
    )


def test_help_tells_about_the_overdue_question() -> None:
    """`/help` — строка о вопросе наутро и о следующем после ответа (§22.3)."""
    assert "О деле, срок которого прошёл, наутро спрошу, получилось ли" in texts.HELP
    assert "о следующем — когда вы ответите" in texts.HELP
