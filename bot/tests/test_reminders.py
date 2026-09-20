"""Напоминания: что планируется, что уходит в Telegram и как гаснет кнопкой.

Сети здесь нет: «сейчас» внедряется, база и отправка подменены. Расписание —
`techspec/06-reminders.md` §6.1, цикл — §6.2, кнопка — §6.3.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from solomon.services.reminders import plan
from tests.conftest import OWNER_TIMEZONE

TZ = ZoneInfo(OWNER_TIMEZONE)

# Понедельник, 21 сентября 2026 года, 10:00 у владельца.
MONDAY_MORNING = datetime(2026, 9, 21, 10, 0, tzinfo=TZ)
# Пятница той же недели — день, на который человек ставит срок.
FRIDAY_END_OF_DAY = datetime(2026, 9, 25, 18, 0, tzinfo=TZ)


def stages(
    due_at: datetime | None, precision: str | None, now: datetime, kind: str = "task"
) -> list[tuple[str, datetime]]:
    """План в виде пар «ступень — момент»: так его удобно сверять глазами."""
    return [
        (planned.stage, planned.fire_at.astimezone(TZ))
        for planned in plan(due_at=due_at, due_precision=precision, kind=kind, timezone=TZ, now=now)
    ]


def test_day_ahead_gets_morning_and_end_of_day() -> None:
    """Назван день: заранее — 09:00 того дня, к сроку — сам `due_at` (18:00)."""
    assert stages(FRIDAY_END_OF_DAY, "day", MONDAY_MORNING) == [
        ("before", FRIDAY_END_OF_DAY.replace(hour=9)),
        ("due", FRIDAY_END_OF_DAY),
    ]


def test_today_after_nine_gets_only_the_due_one() -> None:
    """Утро сегодняшнего дня уже прошло — эта ступень не заводится."""
    today = MONDAY_MORNING.replace(hour=18)

    assert stages(today, "day", MONDAY_MORNING) == [("due", today)]


def test_named_time_gets_an_hour_ahead_and_the_moment() -> None:
    at_three = datetime(2026, 9, 25, 15, 0, tzinfo=TZ)

    assert stages(at_three, "time", MONDAY_MORNING) == [
        ("before", at_three.replace(hour=14)),
        ("due", at_three),
    ]


def test_hour_ahead_already_passed_leaves_only_the_moment() -> None:
    """Сказано «сегодня в 15:00» в 14:30: час до срока прошёл, и это не повод
    стучаться немедленно (§6.1)."""
    at_three = MONDAY_MORNING.replace(hour=15)
    half_past_two = MONDAY_MORNING.replace(hour=14, minute=30)

    assert stages(at_three, "time", half_past_two) == [("due", at_three)]


def test_due_in_the_past_plans_nothing() -> None:
    yesterday = MONDAY_MORNING.replace(day=20, hour=18)

    assert stages(yesterday, "day", MONDAY_MORNING) == []


def test_task_without_a_due_date_plans_nothing() -> None:
    assert stages(None, None, MONDAY_MORNING) == []


def test_idea_and_wish_are_not_reminded_about() -> None:
    """Идея и желание — не дела: стучаться не о чем (§6.1)."""
    assert stages(FRIDAY_END_OF_DAY, "day", MONDAY_MORNING, kind="idea") == []
    assert stages(FRIDAY_END_OF_DAY, "day", MONDAY_MORNING, kind="wish") == []


def test_due_at_in_another_zone_is_planned_in_the_owner_one() -> None:
    """`due_at` приходит из базы в UTC — утро считается по поясу владельца."""
    in_utc = FRIDAY_END_OF_DAY.astimezone(ZoneInfo("UTC"))

    assert stages(in_utc, "day", MONDAY_MORNING) == [
        ("before", FRIDAY_END_OF_DAY.replace(hour=9)),
        ("due", FRIDAY_END_OF_DAY),
    ]


def test_plan_is_ready_for_the_database_as_rows() -> None:
    """В `record_understanding` уходит список `{stage, fire_at}` (§3.5)."""
    planned = plan(
        due_at=FRIDAY_END_OF_DAY,
        due_precision="day",
        kind="task",
        timezone=TZ,
        now=MONDAY_MORNING,
    )

    rows = [item.as_row() for item in planned]

    assert rows[0]["stage"] == "before"
    assert rows[0]["fire_at"].startswith("2026-09-25T09:00")
    assert rows[1]["stage"] == "due"
