"""Вопрос о деле без срока — чистые функции (`techspec/19-undated.md`).

Окно, тишина, неделя, границы отбора для базы и выбор текста. Сети и базы
здесь нет: что спросить и когда, решают эти функции и `undated_to_ask`.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from solomon import texts
from solomon.db.reminders import UndatedTask
from solomon.services import asks
from solomon.services.tasks import QUESTION_TTL
from tests.conftest import OWNER_TIMEZONE

TZ = ZoneInfo(OWNER_TIMEZONE)
# Суббота, 3 октября 2026, 10:20 в поясе владельца (+05:00).
NOW = datetime(2026, 10, 3, 10, 20, tzinfo=TZ)
TASK_ID = "5b0c7a52-8f3e-4c1d-9a6b-2e4f1d3c8b90"


def undated(
    created_at: datetime,
    asked_at: datetime | None = None,
    title: str = "купить фильтр для воды",
) -> UndatedTask:
    return UndatedTask(task_id=TASK_ID, title=title, created_at=created_at, asked_at=asked_at)


def test_window_silence_week_and_question_life_are_constants() -> None:
    """Окно, тишина и неделя — константы, а не настройка (§19.2)."""
    assert asks.WINDOW_START == time(10, 0)
    assert asks.WINDOW_END == time(20, 0)
    assert asks.QUIET == timedelta(minutes=15)
    assert asks.REPEAT_DAYS == 7
    # Живой вопрос — тот же срок, что у открытого вопроса в разговоре (§10.3).
    assert asks.QUESTION_LIFE == QUESTION_TTL


@pytest.mark.parametrize(
    ("moment", "open_"),
    [
        (datetime(2026, 10, 3, 9, 59, tzinfo=TZ), False),
        (datetime(2026, 10, 3, 10, 0, tzinfo=TZ), True),
        (datetime(2026, 10, 3, 14, 0, tzinfo=TZ), True),
        (datetime(2026, 10, 3, 19, 59, tzinfo=TZ), True),
        (datetime(2026, 10, 3, 20, 0, tzinfo=TZ), False),
        (datetime(2026, 10, 3, 23, 30, tzinfo=TZ), False),
        (datetime(2026, 10, 3, 3, 0, tzinfo=TZ), False),
    ],
)
def test_window_is_ten_to_eight_by_owner_time(moment: datetime, open_: bool) -> None:
    """С 10:00 до 20:00: вечером и ночью вопрос не догоняет (§19.2)."""
    assert asks.in_window(moment, TZ) is open_


def test_window_reads_owner_time_not_utc() -> None:
    """05:00 UTC — это 10:00 у владельца: окно открыто."""
    assert asks.in_window(datetime(2026, 10, 3, 5, 0, tzinfo=UTC), TZ) is True
    assert asks.in_window(datetime(2026, 10, 3, 15, 0, tzinfo=UTC), TZ) is False


def test_local_day_is_owner_day() -> None:
    """День процесса — по поясу владельца: 20:30 UTC второго — уже третье."""
    assert asks.local_day(datetime(2026, 10, 2, 20, 30, tzinfo=UTC), TZ) == date(2026, 10, 3)


def test_bounds_for_the_database() -> None:
    """Полночь сегодня и шесть полночей назад, сутки и 15 минут назад (§19.4)."""
    bounds = asks.bounds(NOW, TZ)

    assert bounds.day_start == datetime(2026, 10, 3, 0, 0, tzinfo=TZ)
    assert bounds.asked_before == datetime(2026, 9, 27, 0, 0, tzinfo=TZ)
    assert bounds.question_since == datetime(2026, 10, 2, 10, 20, tzinfo=TZ)
    assert bounds.quiet_since == datetime(2026, 10, 3, 10, 5, tzinfo=TZ)


def test_bounds_from_utc_moment_are_owner_midnights() -> None:
    """Момент в UTC — полночи всё равно по поясу владельца."""
    bounds = asks.bounds(NOW.astimezone(UTC), TZ)

    assert bounds.day_start == datetime(2026, 10, 3, 0, 0, tzinfo=TZ)
    assert bounds.asked_before == datetime(2026, 9, 27, 0, 0, tzinfo=TZ)


def test_week_is_by_calendar() -> None:
    """Спросил в понедельник — снова не раньше следующего понедельника (§19.1)."""
    asked = datetime(2026, 9, 28, 19, 50, tzinfo=TZ)  # понедельник, вечер
    sunday = asks.bounds(datetime(2026, 10, 4, 10, 0, tzinfo=TZ), TZ)
    monday = asks.bounds(datetime(2026, 10, 5, 10, 0, tzinfo=TZ), TZ)

    assert not asked < sunday.asked_before
    assert asked < monday.asked_before


def test_first_question_about_yesterday() -> None:
    """Записано вчера — «Вчера вы просили записать: …» (§19.3)."""
    task = undated(datetime(2026, 10, 2, 21, 30, tzinfo=TZ))

    assert asks.question_text(task, NOW, TZ) == (
        "Вчера вы просили записать: купить фильтр для воды. Когда займётесь?"
    )


def test_yesterday_is_by_owner_day_not_utc() -> None:
    """01:30 у владельца — вчера, хотя в UTC это позавчера."""
    task = undated(datetime(2026, 10, 1, 20, 30, tzinfo=UTC))

    assert asks.question_text(task, NOW, TZ).startswith("Вчера вы просили записать: ")


def test_first_question_about_older_names_the_date() -> None:
    """Записано раньше вчерашнего — числом и месяцем."""
    task = undated(datetime(2026, 9, 30, 9, 0, tzinfo=TZ))

    assert asks.question_text(task, NOW, TZ) == (
        "30 сентября вы просили записать: купить фильтр для воды. Когда займётесь?"
    )


def test_repeat_question_says_still_without_due() -> None:
    """Повторный — что срока всё нет и как убрать дело (§19.3)."""
    task = undated(
        datetime(2026, 9, 18, 9, 0, tzinfo=TZ),
        asked_at=datetime(2026, 9, 26, 10, 0, tzinfo=TZ),
    )

    assert asks.question_text(task, NOW, TZ) == (
        "Всё ещё без срока: купить фильтр для воды. Когда займётесь? "
        "Если уже не нужно, скажите — уберу из списка."
    )


def test_every_question_ends_with_the_question_the_bot_recognizes() -> None:
    """Открытым вопросом задачи пишется константа — по ней бот узнаёт свой вопрос."""
    first = undated(datetime(2026, 9, 30, 9, 0, tzinfo=TZ))
    again = undated(first.created_at, asked_at=datetime(2026, 9, 26, 10, 0, tzinfo=TZ))

    assert texts.UNDATED_QUESTION == "Когда займётесь?"
    assert texts.UNDATED_QUESTION in asks.question_text(first, NOW, TZ)
    assert texts.UNDATED_QUESTION in asks.question_text(again, NOW, TZ)


def test_help_tells_about_the_undated_question() -> None:
    """В /help — что о деле без срока бот на следующий день спросит (§19.3)."""
    assert "О деле без срока на следующий день спрошу, когда за него взяться" in texts.HELP
