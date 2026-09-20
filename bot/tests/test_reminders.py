"""Напоминания: что планируется, что уходит в Telegram и как гаснет кнопкой.

Сети здесь нет: «сейчас» внедряется, база и отправка подменены. Расписание —
`techspec/06-reminders.md` §6.1, цикл — §6.2, кнопка — §6.3.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import datetime
from typing import cast
from zoneinfo import ZoneInfo

import pytest

from solomon.db.reminders import DueReminder
from solomon.db.rpc import DatabaseError
from solomon.db.tasks import Task
from solomon.services.reminders import ReminderService, by_task, latest, plan
from solomon.services.tasks import TaskService
from solomon.services.understanding import Understanding
from tests.conftest import (
    OWNER_ID,
    OWNER_TIMEZONE,
    FakeAnalyst,
    FakeMessages,
    FakeUnderstandings,
    make_settings,
    make_understanding,
)

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


def build_service(
    understanding: Understanding, now: datetime
) -> tuple[TaskService, FakeUnderstandings]:
    """Приём поручения на подменённой базе и с остановленными часами."""
    understandings = FakeUnderstandings()
    service = TaskService(
        settings=make_settings(),
        record_message=FakeMessages(),
        record_understanding=understandings,
        analyst=FakeAnalyst(understanding),
        clock=lambda: now,
    )
    return service, understandings


async def record(understanding: Understanding, now: datetime) -> tuple[str, list[dict[str, str]]]:
    """Ответ человеку и напоминания, ушедшие в базу тем же вызовом."""
    service, understandings = build_service(understanding, now)
    outcome = await service.record_from_message(
        chat_id=OWNER_ID, telegram_message_id=7, text="в пятницу отправить расчёт"
    )
    rows = cast(list[dict[str, str]], understandings.calls[0]["reminders"])
    return outcome.message, rows


async def test_confirmation_names_the_nearest_reminder() -> None:
    """«Напомню» — про ближайшую ступень, а не про срок (§6.4)."""
    message, rows = await record(
        make_understanding(title="отправить расчёт", due_at=FRIDAY_END_OF_DAY, due_precision="day"),
        MONDAY_MORNING,
    )

    assert "Напомню: 25 сентября в 09:00" in message
    assert [row["stage"] for row in rows] == ["before", "due"]


async def test_confirmation_says_today_when_the_reminder_is_today() -> None:
    today = MONDAY_MORNING.replace(hour=18)
    message, rows = await record(
        make_understanding(title="отправить расчёт", due_at=today, due_precision="day"),
        MONDAY_MORNING,
    )

    assert "Напомню: сегодня в 18:00" in message
    assert [row["stage"] for row in rows] == ["due"]


async def test_task_without_a_due_date_promises_nothing() -> None:
    """Напоминания нет — и обещания нет: бот не говорит о том, чего не будет."""
    message, rows = await record(make_understanding(title="купить лампочку"), MONDAY_MORNING)

    assert "Напомню" not in message
    assert rows == []


def make_due(stage: str, fire_at: datetime, task_id: str = "0e2f", **fields: object) -> DueReminder:
    """Созревшее напоминание, как его приносит база (§3.5)."""
    base: dict[str, object] = {
        "id": f"{task_id}-{stage}",
        "task_id": task_id,
        "stage": stage,
        "fire_at": fire_at,
        "title": "отправить расчёт",
        "due_at": FRIDAY_END_OF_DAY,
        "due_precision": "day",
    }
    return DueReminder(**{**base, **fields})  # type: ignore[arg-type]


def test_both_stages_of_one_task_speak_with_the_later_one() -> None:
    """После простоя созрели обе ступени — сообщение одно, по позднейшей (§6.2)."""
    before = make_due("before", FRIDAY_END_OF_DAY.replace(hour=9))
    due = make_due("due", FRIDAY_END_OF_DAY)

    assert latest([before, due]) is due
    assert latest([due, before]) is due


def test_tasks_are_grouped_by_task_not_by_stage() -> None:
    first = make_due("due", FRIDAY_END_OF_DAY, task_id="0e2f")
    second = make_due("before", FRIDAY_END_OF_DAY.replace(hour=9), task_id="7c31")

    assert list(by_task([first, second])) == ["0e2f", "7c31"]


class FakeDue:
    """База в тике: что созрело и о чём её спросили."""

    def __init__(self, ripe: list[DueReminder] | None = None, broken: bool = False) -> None:
        self.ripe = ripe or []
        self.broken = broken
        self.calls: list[tuple[int, datetime]] = []

    async def __call__(self, *, owner_telegram_id: int, now: datetime) -> list[DueReminder]:
        if self.broken:
            raise DatabaseError("ConnectTimeout: timed out")
        self.calls.append((owner_telegram_id, now))
        return self.ripe


class FakeMarks:
    """Отметка «ушло»: тест смотрит, что и каким сообщением помечено."""

    def __init__(self, broken: bool = False) -> None:
        self.broken = broken
        self.calls: list[tuple[int, list[str], int]] = []

    async def __call__(
        self, *, owner_telegram_id: int, reminder_ids: Sequence[str], telegram_message_id: int
    ) -> None:
        if self.broken:
            raise DatabaseError("ConnectTimeout: timed out")
        self.calls.append((owner_telegram_id, list(reminder_ids), telegram_message_id))


class FakeNotifier:
    """Вместо Telegram — список отправленного; сломанный роняет отправку."""

    def __init__(self, broken: bool = False) -> None:
        self.broken = broken
        self.sent: list[tuple[str, str]] = []

    async def __call__(self, *, text: str, task_id: str) -> int:
        if self.broken:
            raise RuntimeError("Telegram: Bad Gateway")
        self.sent.append((task_id, text))
        return 40 + len(self.sent)


class FakeCloser:
    """`mark_task_done` без базы: что закрыли и что база на это ответила."""

    def __init__(self, task: Task | None = None, broken: bool = False) -> None:
        self.task = task
        self.broken = broken
        self.calls: list[tuple[int, str]] = []

    async def __call__(self, *, owner_telegram_id: int, task_id: str) -> Task | None:
        if self.broken:
            raise DatabaseError("ConnectTimeout: timed out")
        self.calls.append((owner_telegram_id, task_id))
        return self.task


def build_reminders(
    due: FakeDue | None = None,
    marks: FakeMarks | None = None,
    notifier: FakeNotifier | None = None,
    closer: FakeCloser | None = None,
) -> tuple[ReminderService, FakeDue, FakeMarks, FakeNotifier]:
    """Сервис напоминаний на подделках: ни базы, ни сети."""
    lister = due or FakeDue()
    marker = marks or FakeMarks()
    sender = notifier or FakeNotifier()
    service = ReminderService(
        settings=make_settings(),
        due=lister,
        mark_sent=marker,
        close_task=closer or FakeCloser(),
        notify=sender,
        clock=lambda: FRIDAY_END_OF_DAY,
    )
    return service, lister, marker, sender


async def test_ripe_reminder_is_sent_and_marked() -> None:
    """Созрело — ушло с текстом задачи и только потом помечено (§6.2)."""
    service, lister, marks, notifier = build_reminders(
        due=FakeDue([make_due("due", FRIDAY_END_OF_DAY)])
    )

    assert await service.tick() == 1
    assert lister.calls == [(OWNER_ID, FRIDAY_END_OF_DAY)]
    task_id, text = notifier.sent[0]
    assert task_id == "0e2f"
    assert text == "Напоминаю: отправить расчёт\nСрок: сегодня, 18:00"
    assert marks.calls == [(OWNER_ID, ["0e2f-due"], 41)]


async def test_both_stages_make_one_message_and_two_marks() -> None:
    """После простоя созрели обе ступени: сообщение одно, помечены обе."""
    ripe = [
        make_due("before", FRIDAY_END_OF_DAY.replace(hour=9)),
        make_due("due", FRIDAY_END_OF_DAY),
    ]
    service, _, marks, notifier = build_reminders(due=FakeDue(ripe))

    assert await service.tick() == 1
    assert len(notifier.sent) == 1
    assert marks.calls == [(OWNER_ID, ["0e2f-before", "0e2f-due"], 41)]


async def test_two_tasks_get_a_message_each() -> None:
    ripe = [
        make_due("due", FRIDAY_END_OF_DAY, task_id="0e2f"),
        make_due("due", FRIDAY_END_OF_DAY, task_id="7c31"),
    ]
    service, _, marks, notifier = build_reminders(due=FakeDue(ripe))

    assert await service.tick() == 2
    assert [task_id for task_id, _ in notifier.sent] == ["0e2f", "7c31"]
    assert [call[2] for call in marks.calls] == [41, 42]


async def test_empty_tick_sends_nothing() -> None:
    service, _, marks, notifier = build_reminders()

    assert await service.tick() == 0
    assert notifier.sent == []
    assert marks.calls == []


async def test_failed_send_leaves_the_reminder_unmarked() -> None:
    """Telegram не принял — `sent_at` не ставится, попытка повторится (§6.2)."""
    service, _, marks, _ = build_reminders(
        due=FakeDue([make_due("due", FRIDAY_END_OF_DAY)]), notifier=FakeNotifier(broken=True)
    )

    assert await service.tick() == 0
    assert marks.calls == []


async def test_failed_mark_does_not_lose_the_sent_message() -> None:
    """Ушло, но не помечено — тик не падает: дубль лучше потери (инвариант 5)."""
    service, _, _, notifier = build_reminders(
        due=FakeDue([make_due("due", FRIDAY_END_OF_DAY)]), marks=FakeMarks(broken=True)
    )

    assert await service.tick() == 1
    assert len(notifier.sent) == 1


async def test_broken_database_does_not_kill_the_loop() -> None:
    """Тик упал на базе — бот жив и принимает сообщения дальше (§6.2)."""
    service, _, _, notifier = build_reminders(due=FakeDue(broken=True))

    await service.tick_quietly()

    assert notifier.sent == []


async def test_overdue_task_says_the_due_date_has_passed() -> None:
    """Бот был выключен: срок прошёл к моменту отправки — «Срок был» (§6.2)."""
    ripe = [make_due("due", FRIDAY_END_OF_DAY, due_at=MONDAY_MORNING.replace(hour=18))]
    service, _, _, notifier = build_reminders(due=FakeDue(ripe))

    assert await service.tick() == 1
    _, text = notifier.sent[0]
    assert text == "Напоминаю: отправить расчёт\nСрок был: понедельник, 21 сентября, 18:00"


async def test_loop_stops_on_cancel() -> None:
    """Остановка процесса отменяет цикл штатно, без ошибки в логе (§6.2)."""
    service, lister, _, _ = build_reminders(due=FakeDue([]))
    ticking = asyncio.create_task(service.run(interval_seconds=0.01))
    await asyncio.sleep(0.03)

    ticking.cancel()
    with pytest.raises(asyncio.CancelledError):
        await ticking

    assert lister.calls
