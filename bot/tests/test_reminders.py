"""Напоминания: что планируется, что уходит в Telegram и как гаснет кнопкой.

Сети здесь нет: «сейчас» внедряется, база и отправка подменены. Расписание —
`techspec/06-reminders.md` §6.1, цикл — §6.2, кнопка — §6.3.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import cast
from zoneinfo import ZoneInfo

import pytest
from aiogram import Bot, Dispatcher
from aiogram.methods import SendMessage
from aiogram.types import InlineKeyboardMarkup
from supabase import Client

from solomon import texts
from solomon.db.morning import DayTask
from solomon.db.reminders import DueReminder, MovedTask, OverdueTask, Planned, UndatedTask
from solomon.db.rpc import DatabaseError
from solomon.db.tasks import TaskDetails
from solomon.handlers import done_keyboard
from solomon.runner import build_dispatcher
from solomon.runner import build_reminders as build_reminders_service
from solomon.services import asks, morning, overdue
from solomon.services.reminders import (
    ReminderService,
    by_task,
    database_planner,
    latest,
    mirror_timezone,
    past_due,
)
from solomon.services.tasks import TaskService
from solomon.services.understanding import Understanding
from tests.conftest import (
    OWNER_ID,
    OWNER_TIMEZONE,
    STRANGER_ID,
    FakeAnalyst,
    FakeMessages,
    FakePlanner,
    FakeTranscriber,
    FakeUnderstandings,
    RecordingSession,
    make_callback_update,
    make_details,
    make_settings,
    make_understanding,
)

TZ = ZoneInfo(OWNER_TIMEZONE)

# Понедельник, 21 сентября 2026 года, 10:00 у владельца.
MONDAY_MORNING = datetime(2026, 9, 21, 10, 0, tzinfo=TZ)
# Пятница той же недели — день, на который человек ставит срок.
FRIDAY_END_OF_DAY = datetime(2026, 9, 25, 18, 0, tzinfo=TZ)


def build_service(
    understanding: Understanding, now: datetime, planner: FakePlanner | None = None
) -> tuple[TaskService, FakeUnderstandings, FakePlanner]:
    """Приём поручения на подменённой базе и с остановленными часами."""
    understandings = FakeUnderstandings()
    planned = planner or FakePlanner()
    service = TaskService(
        settings=make_settings(),
        record_message=FakeMessages(),
        record_understanding=understandings,
        analyst=FakeAnalyst(understanding),
        transcriber=FakeTranscriber(),
        planner=planned,
        clock=lambda: now,
    )
    return service, understandings, planned


async def record(
    understanding: Understanding, now: datetime, planner: FakePlanner | None = None
) -> tuple[str, list[dict[str, str]], FakePlanner]:
    """Ответ человеку, напоминания, ушедшие в базу тем же вызовом, и вопросы к плану."""
    service, understandings, planned = build_service(understanding, now, planner)
    outcome = await service.record_from_message(
        chat_id=OWNER_ID, telegram_message_id=7, text="в пятницу отправить расчёт"
    )
    rows = (
        cast(list[dict[str, str]], understandings.calls[0]["reminders"])
        if understandings.calls
        else []
    )
    return outcome.message, rows, planned


async def test_plan_is_asked_of_the_database_with_the_task_due() -> None:
    """Расписание считает база (§11.3): бот спрашивает его по сроку, точности и виду."""
    _, _, planner = await record(
        make_understanding(title="отправить расчёт", due_at=FRIDAY_END_OF_DAY, due_precision="day"),
        MONDAY_MORNING,
    )

    assert planner.calls == [
        {
            "due_at": FRIDAY_END_OF_DAY,
            "due_precision": "day",
            "kind": "task",
            "now": MONDAY_MORNING,
        }
    ]


async def test_confirmation_names_the_nearest_reminder() -> None:
    """«Напомню» — про ближайшую ступень плана, а не про срок (§6.4).

    План уходит в базу как есть: бот обещает ровно то, что записал.
    """
    morning = FRIDAY_END_OF_DAY.replace(hour=9)
    planner = FakePlanner(
        [Planned(stage="due", fire_at=FRIDAY_END_OF_DAY), Planned(stage="before", fire_at=morning)]
    )
    message, rows, _ = await record(
        make_understanding(title="отправить расчёт", due_at=FRIDAY_END_OF_DAY, due_precision="day"),
        MONDAY_MORNING,
        planner,
    )

    assert "Напомню: 25 сентября в 09:00" in message
    assert rows == [
        {"stage": "due", "fire_at": FRIDAY_END_OF_DAY.isoformat()},
        {"stage": "before", "fire_at": morning.isoformat()},
    ]


async def test_confirmation_says_today_when_the_reminder_is_today() -> None:
    today = MONDAY_MORNING.replace(hour=18)
    message, rows, _ = await record(
        make_understanding(title="отправить расчёт", due_at=today, due_precision="day"),
        MONDAY_MORNING,
        FakePlanner([Planned(stage="due", fire_at=today)]),
    )

    assert "Напомню: сегодня в 18:00" in message
    assert [row["stage"] for row in rows] == ["due"]


async def test_meeting_confirmation_names_an_hour_before() -> None:
    """Встреча с часом (этап 029): ближайшее — за час; «за 5 минут» тоже в базе."""
    meeting = FRIDAY_END_OF_DAY.replace(hour=15)
    hour_before = meeting.replace(hour=14)
    five_before = meeting.replace(hour=14, minute=55)
    message, rows, _ = await record(
        make_understanding(title="созвон с Игорем", due_at=meeting, due_precision="time"),
        MONDAY_MORNING,
        FakePlanner(
            [
                Planned(stage="before", fire_at=hour_before),
                Planned(stage="due", fire_at=five_before),
            ]
        ),
    )

    assert "Напомню: 25 сентября в 14:00" in message
    assert rows == [
        {"stage": "before", "fire_at": hour_before.isoformat()},
        {"stage": "due", "fire_at": five_before.isoformat()},
    ]


async def test_meeting_within_the_hour_names_five_minutes_before() -> None:
    """«В 15:00» сказано в 14:30: час прошёл — «Напомню» называет 14:55 (§6.4)."""
    meeting = MONDAY_MORNING.replace(hour=15)
    five_before = meeting.replace(hour=14, minute=55)
    message, rows, _ = await record(
        make_understanding(title="созвон с Игорем", due_at=meeting, due_precision="time"),
        MONDAY_MORNING.replace(hour=14, minute=30),
        FakePlanner([Planned(stage="due", fire_at=five_before)]),
    )

    assert "Напомню: сегодня в 14:55" in message
    assert rows == [{"stage": "due", "fire_at": five_before.isoformat()}]


async def test_moment_from_the_database_is_named_in_the_owner_zone() -> None:
    """База отдаёт момент в UTC — «Напомню» звучит по часам владельца."""
    morning_utc = FRIDAY_END_OF_DAY.replace(hour=9).astimezone(ZoneInfo("UTC"))
    message, _, _ = await record(
        make_understanding(title="отправить расчёт", due_at=FRIDAY_END_OF_DAY, due_precision="day"),
        MONDAY_MORNING,
        FakePlanner([Planned(stage="before", fire_at=morning_utc)]),
    )

    assert "Напомню: 25 сентября в 09:00" in message


async def test_empty_plan_promises_nothing() -> None:
    """Напоминания нет — и обещания нет: бот не говорит о том, чего не будет."""
    message, rows, _ = await record(make_understanding(title="купить лампочку"), MONDAY_MORNING)

    assert "Напомню" not in message
    assert rows == []


async def test_idea_is_planned_by_the_database_too() -> None:
    """Вид решает база: бот не отсекает идею сам, а передаёт её вид (§6.1)."""
    _, rows, planner = await record(
        make_understanding(kind="idea", title="курс по гончарке", due_at=FRIDAY_END_OF_DAY),
        MONDAY_MORNING,
    )

    assert [call["kind"] for call in planner.calls] == ["idea"]
    assert rows == []


async def test_talk_without_a_task_does_not_ask_for_a_plan() -> None:
    """Задачи нет — планировать нечего, и база не зовётся."""
    message, _, planner = await record(make_understanding(kind="chat", title=""), MONDAY_MORNING)

    assert planner.calls == []
    assert message == texts.NO_ERRAND


async def test_plan_failure_is_a_failed_record() -> None:
    """База не дала план — не записано: «Напомню» было бы неправдой (§11.3)."""
    service, understandings, _ = build_service(
        make_understanding(title="отправить расчёт", due_at=FRIDAY_END_OF_DAY, due_precision="day"),
        MONDAY_MORNING,
        FakePlanner(broken=True),
    )

    outcome = await service.record_from_message(
        chat_id=OWNER_ID, telegram_message_id=7, text="в пятницу отправить расчёт"
    )

    assert outcome.ok is False
    assert outcome.message == texts.NOT_SAVED
    assert understandings.calls == []


class FakeRpcResponse:
    def __init__(self, data: object) -> None:
        self.data = data


class FakeRpcQuery:
    def __init__(self, data: object) -> None:
        self._data = data

    def execute(self) -> FakeRpcResponse:
        return FakeRpcResponse(self._data)


class FakeRpcClient:
    """Клиент Supabase: на каждую функцию — свой ответ, вызовы записываются."""

    def __init__(self, answers: dict[str, object] | None = None, broken: bool = False) -> None:
        self.answers = answers or {}
        self.broken = broken
        self.calls: list[str] = []
        self.params: list[dict[str, object]] = []

    def rpc(self, function: str, params: dict[str, object]) -> FakeRpcQuery:
        self.calls.append(function)
        self.params.append(params)
        if self.broken:
            raise RuntimeError("APIError: connection refused")
        return FakeRpcQuery(self.answers.get(function))


async def test_database_plan_goes_with_the_owner_zone_and_comes_back_in_order() -> None:
    """`reminder_plan` получает пояс из настроек и «сейчас»; строки — в `Planned`."""
    client = FakeRpcClient(
        {
            "reminder_plan": [
                {"stage": "due", "fire_at": "2026-09-25T13:00:00+00:00"},
                {"stage": "before", "fire_at": "2026-09-25T04:00:00+00:00"},
            ]
        }
    )
    planner = database_planner(make_settings(), cast(Client, client))

    planned = await planner(
        due_at=FRIDAY_END_OF_DAY, due_precision="day", kind="task", now=MONDAY_MORNING
    )

    assert client.calls == ["reminder_plan"]
    assert client.params == [
        {
            "due_at": FRIDAY_END_OF_DAY.isoformat(),
            "due_precision": "day",
            "kind": "task",
            "timezone": OWNER_TIMEZONE,
            "now": MONDAY_MORNING.isoformat(),
        }
    ]
    assert [item.stage for item in planned] == ["before", "due"]
    assert planned[0].fire_at == FRIDAY_END_OF_DAY.replace(hour=9)


async def test_database_plan_without_due_sends_null() -> None:
    client = FakeRpcClient({"reminder_plan": []})
    planner = database_planner(make_settings(), cast(Client, client))

    assert await planner(due_at=None, due_precision=None, kind="task", now=MONDAY_MORNING) == []
    assert client.params[0]["due_at"] is None


async def test_unknown_stage_from_the_database_is_a_refusal() -> None:
    client = FakeRpcClient(
        {"reminder_plan": [{"stage": "later", "fire_at": "2026-09-25T04:00:00+00:00"}]}
    )
    planner = database_planner(make_settings(), cast(Client, client))

    with pytest.raises(DatabaseError):
        await planner(
            due_at=FRIDAY_END_OF_DAY, due_precision="day", kind="task", now=MONDAY_MORNING
        )


async def test_owner_zone_is_mirrored_into_the_database() -> None:
    """При запуске бот пишет пояс владельца в базу (§11.3)."""
    client = FakeRpcClient()

    assert await mirror_timezone(make_settings(), cast(Client, client)) is True
    assert client.calls == ["save_owner_timezone"]
    assert client.params == [{"owner_telegram_id": OWNER_ID, "timezone": OWNER_TIMEZONE}]


async def test_zone_mirror_failure_is_logged_and_survived(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Не записалось — строка в журнале, бот работает дальше."""
    client = FakeRpcClient(broken=True)

    with caplog.at_level("ERROR"):
        assert await mirror_timezone(make_settings(), cast(Client, client)) is False
    assert "Пояс владельца не записан" in caplog.text


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

    def __init__(self, broken: bool = False, events: list[str] | None = None) -> None:
        self.broken = broken
        self.sent: list[tuple[str, str]] = []
        # Раз повторяющейся задачи для кнопки «Сделано» — по отправке (§13.3).
        self.occurrences: list[int | None] = []
        # Есть ли под сообщением кнопка «Сделано» — по отправке (этап 029, §6.3).
        self.buttons: list[bool] = []
        self.events = events if events is not None else []

    async def __call__(
        self, *, text: str, task_id: str, occurrence: int | None = None, button: bool = True
    ) -> int:
        if self.broken:
            raise RuntimeError("Telegram: Bad Gateway")
        self.sent.append((task_id, text))
        self.occurrences.append(occurrence)
        self.buttons.append(button)
        self.events.append("reminder")
        return 40 + len(self.sent)


class FakeMoved:
    """`moved_tasks` без базы: что лежит с отметкой «срок перенесён»."""

    def __init__(self, tasks: list[MovedTask] | None = None, broken: bool = False) -> None:
        self.tasks = tasks or []
        self.broken = broken
        self.calls: list[int] = []

    async def __call__(self, *, owner_telegram_id: int) -> list[MovedTask]:
        if self.broken:
            raise DatabaseError("ConnectTimeout: timed out")
        self.calls.append(owner_telegram_id)
        return list(self.tasks)


class FakeClearMoved:
    """`clear_due_moved` без базы: что снимали и сменилась ли отметка."""

    def __init__(self, cleared: bool = True, broken: bool = False) -> None:
        self.cleared = cleared
        self.broken = broken
        self.calls: list[tuple[int, str, datetime]] = []

    async def __call__(self, *, owner_telegram_id: int, task_id: str, seen: datetime) -> bool:
        if self.broken:
            raise DatabaseError("ConnectTimeout: timed out")
        self.calls.append((owner_telegram_id, task_id, seen))
        return self.cleared


class FakeAnnouncer:
    """Строка в чат без кнопки: вместо Telegram — список текстов.

    `label` — чем сообщение отмечается в общем порядке событий: строкой
    «Перенёс» или утренним планом — оба уходят без кнопки.
    """

    def __init__(
        self, broken: bool = False, events: list[str] | None = None, label: str = "moved"
    ) -> None:
        self.broken = broken
        self.sent: list[str] = []
        self.events = events if events is not None else []
        self.label = label

    async def __call__(self, *, text: str) -> int:
        if self.broken:
            raise RuntimeError("Telegram: Bad Gateway")
        self.sent.append(text)
        self.events.append(self.label)
        return 60 + len(self.sent)


class FakeCloser:
    """`mark_task_done` без базы: что закрыли и что база на это ответила."""

    def __init__(self, task: TaskDetails | None = None, broken: bool = False) -> None:
        self.task = task
        self.broken = broken
        self.calls: list[tuple[int, str]] = []
        # Раз из кнопки — по нажатию; у кнопки до этапа 011 его нет (§13.3).
        self.occurrences: list[int | None] = []

    async def __call__(
        self, *, owner_telegram_id: int, task_id: str, occurrence: int | None = None
    ) -> TaskDetails | None:
        if self.broken:
            raise DatabaseError("ConnectTimeout: timed out")
        self.calls.append((owner_telegram_id, task_id))
        self.occurrences.append(occurrence)
        return self.task


def build_reminders(
    due: FakeDue | None = None,
    marks: FakeMarks | None = None,
    notifier: FakeNotifier | None = None,
    closer: FakeCloser | None = None,
    moved: FakeMoved | None = None,
    clear_moved: FakeClearMoved | None = None,
    announcer: FakeAnnouncer | None = None,
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
        moved=moved or FakeMoved(),
        clear_moved=clear_moved or FakeClearMoved(),
        announce=announcer or FakeAnnouncer(),
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
    assert text == "Напоминаю: отправить расчёт\nСрок: сегодня"
    assert notifier.buttons == [True]
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
    assert text == "Напоминаю: отправить расчёт\nСрок был: понедельник, 21 сентября"


async def test_loop_stops_on_cancel() -> None:
    """Остановка процесса отменяет цикл штатно, без ошибки в логе (§6.2)."""
    service, lister, _, _ = build_reminders(due=FakeDue([]))
    ticking = asyncio.create_task(service.run(interval_seconds=0.01))
    await asyncio.sleep(0.03)

    ticking.cancel()
    with pytest.raises(asyncio.CancelledError):
        await ticking

    assert lister.calls


def build_dispatcher_with(closer: FakeCloser) -> Dispatcher:
    """Диспетчер с подменённым закрытием задачи: сети и базы нет."""
    settings = make_settings()
    service = ReminderService(
        settings=settings,
        due=FakeDue(),
        mark_sent=FakeMarks(),
        close_task=closer,
        notify=FakeNotifier(),
        moved=FakeMoved(),
        clear_moved=FakeClearMoved(),
        announce=FakeAnnouncer(),
        clock=lambda: FRIDAY_END_OF_DAY,
    )
    return build_dispatcher(settings, reminders=service)


async def test_done_button_closes_the_task_and_marks_the_message(
    bot: Bot, session: RecordingSession
) -> None:
    """Нажата кнопка: задача закрыта, кнопка убрана, внизу «✓ Сделано» (§6.3)."""
    closer = FakeCloser(make_details(id="0e2f", title="отправить расчёт", status="done"))
    dispatcher = build_dispatcher_with(closer)

    await dispatcher.feed_update(bot, make_callback_update("done:0e2f"))

    assert closer.calls == [(OWNER_ID, "0e2f")]
    edit = session.edits[0]
    assert edit.text == "Напоминаю: отправить расчёт\n\n✓ Сделано"
    assert edit.reply_markup is None
    assert session.answers == [texts.DONE_ANSWER]


async def test_second_press_changes_nothing(bot: Bot, session: RecordingSession) -> None:
    """Повтор безвреден: задача уже закрыта, отметка уже стоит (§6.3)."""
    closer = FakeCloser(make_details(id="0e2f", title="отправить расчёт", status="done"))
    dispatcher = build_dispatcher_with(closer)
    marked = "Напоминаю: отправить расчёт\n\n✓ Сделано"

    await dispatcher.feed_update(bot, make_callback_update("done:0e2f", text=marked))

    assert closer.calls == [(OWNER_ID, "0e2f")]
    assert session.edits == []
    assert session.answers == [texts.DONE_ANSWER]


async def test_unknown_task_is_not_marked_done(bot: Bot, session: RecordingSession) -> None:
    """База не нашла задачу — сообщение не трогаем и говорим об этом."""
    dispatcher = build_dispatcher_with(FakeCloser(None))

    await dispatcher.feed_update(bot, make_callback_update("done:7c31"))

    assert session.edits == []
    assert session.answers == [texts.DONE_UNKNOWN]


async def test_broken_database_does_not_pretend_the_task_is_closed(
    bot: Bot, session: RecordingSession
) -> None:
    """Инвариант 4: база не ответила — «✓ Сделано» не появляется."""
    dispatcher = build_dispatcher_with(FakeCloser(broken=True))

    await dispatcher.feed_update(bot, make_callback_update("done:0e2f"))

    assert session.edits == []
    assert session.answers == [texts.NOT_CLOSED]


async def test_callback_from_a_stranger_never_reaches_the_handler(
    bot: Bot, session: RecordingSession
) -> None:
    """Калитка владельца режет чужой callback до обработчика (§6.3, инвариант 2)."""
    closer = FakeCloser(make_details(id="0e2f", title="отправить расчёт", status="done"))
    dispatcher = build_dispatcher_with(closer)

    await dispatcher.feed_update(bot, make_callback_update("done:0e2f", from_id=STRANGER_ID))

    assert closer.calls == []
    assert session.sent == []


def test_reminder_carries_the_done_button() -> None:
    """Под напоминанием одна кнопка, и в ней id задачи (§6.3)."""
    keyboard = done_keyboard("0e2f")

    button = keyboard.inline_keyboard[0][0]
    assert button.text == texts.DONE_BUTTON
    assert button.callback_data == "done:0e2f"


async def test_reminder_goes_to_the_owner_with_the_button(
    bot: Bot, session: RecordingSession
) -> None:
    """Сборка из `runner.py` целиком: текст, чат владельца и кнопка (§6.2)."""
    row: dict[str, object] = {
        "id": "b17c",
        "task_id": "0e2f",
        "stage": "due",
        "fire_at": FRIDAY_END_OF_DAY.isoformat(),
        "title": "отправить расчёт",
        "due_at": FRIDAY_END_OF_DAY.isoformat(),
        "due_precision": "day",
    }
    client = FakeRpcClient({"due_reminders": [row]})
    service = build_reminders_service(make_settings(), cast(Client, client), bot)

    assert await service.tick(FRIDAY_END_OF_DAY) == 1
    sent = session.sent[0]
    assert isinstance(sent, SendMessage)
    assert sent.chat_id == OWNER_ID
    assert sent.text == "Напоминаю: отправить расчёт\nСрок: сегодня"
    assert isinstance(sent.reply_markup, InlineKeyboardMarkup)
    assert sent.reply_markup.inline_keyboard[0][0].callback_data == "done:0e2f"
    # Тик сначала перекатывает пропущенные разы (§13.4), потом отбирает созревшее.
    assert client.calls == [
        "roll_repeats",
        "due_reminders",
        "mark_reminders_sent",
        "moved_tasks",
    ]


# Встреча — дело с часом — напоминает без кнопки «Сделано»: «это просто
# напоминание» (этап 029, `techspec/06-reminders.md` §6.3).

MEETING = datetime(2026, 9, 25, 15, 0, tzinfo=TZ)


@pytest.mark.parametrize(
    ("stage", "fire_at"),
    [("before", MEETING.replace(hour=14)), ("due", MEETING.replace(hour=14, minute=55))],
    ids=["hour-before", "five-minutes-before"],
)
async def test_reminder_of_a_task_with_an_hour_goes_without_the_button(
    stage: str, fire_at: datetime
) -> None:
    """За час и за 5 минут до встречи — текст тот же, кнопки нет."""
    ripe = [make_due(stage, fire_at, title="созвон с Игорем", due_at=MEETING, due_precision="time")]
    service, _, marks, notifier = build_reminders(due=FakeDue(ripe))

    assert await service.tick(fire_at) == 1
    assert notifier.sent == [("0e2f", "Напоминаю: созвон с Игорем\nСрок: сегодня, 15:00")]
    assert notifier.buttons == [False]
    assert marks.calls == [(OWNER_ID, [f"0e2f-{stage}"], 41)]


async def test_catch_up_reminder_of_a_meeting_goes_without_the_button() -> None:
    """Бот лежал: созрели обе ступени встречи — одно сообщение, и тоже без кнопки."""
    ripe = [
        make_due("before", MEETING.replace(hour=14), due_at=MEETING, due_precision="time"),
        make_due("due", MEETING.replace(hour=14, minute=55), due_at=MEETING, due_precision="time"),
    ]
    service, _, marks, notifier = build_reminders(due=FakeDue(ripe))

    assert await service.tick(MEETING.replace(hour=16)) == 1
    assert notifier.sent == [("0e2f", "Напоминаю: отправить расчёт\nСрок был: сегодня, 15:00")]
    assert notifier.buttons == [False]
    assert marks.calls == [(OWNER_ID, ["0e2f-before", "0e2f-due"], 41)]


async def test_reminder_of_a_repeated_meeting_goes_without_the_button() -> None:
    """Повторяющаяся встреча — тоже дело с часом: напоминание без кнопки."""
    ripe = [
        make_due(
            "due",
            MEETING.replace(hour=14, minute=55),
            title="планёрка",
            due_at=MEETING,
            due_precision="time",
            repeat={"every": "week", "interval": 1, "weekdays": [5], "time": "15:00"},
            occurrence_at=MEETING,
        )
    ]
    service, _, _, notifier = build_reminders(due=FakeDue(ripe))

    assert await service.tick(MEETING.replace(hour=14, minute=55)) == 1
    assert notifier.buttons == [False]


@pytest.mark.parametrize(
    ("precision", "due_at"),
    [
        ("day", FRIDAY_END_OF_DAY),
        (None, FRIDAY_END_OF_DAY),
        ("morning", FRIDAY_END_OF_DAY.replace(hour=8)),
        ("afternoon", FRIDAY_END_OF_DAY.replace(hour=12)),
        ("evening", FRIDAY_END_OF_DAY),
    ],
)
async def test_reminder_of_a_task_without_an_hour_keeps_the_button(
    precision: str | None, due_at: datetime
) -> None:
    """Дело на день и часть дня — «Сделано», как раньше: «купить лампочку»."""
    ripe = [
        make_due("due", due_at, title="купить лампочку", due_at=due_at, due_precision=precision)
    ]
    service, _, _, notifier = build_reminders(due=FakeDue(ripe))

    assert await service.tick(due_at) == 1
    assert notifier.buttons == [True]


async def test_meeting_reminder_goes_to_the_owner_without_a_keyboard(
    bot: Bot, session: RecordingSession
) -> None:
    """Сборка из `runner.py`: напоминание о встрече уходит без клавиатуры."""
    row: dict[str, object] = {
        "id": "b17c",
        "task_id": "0e2f",
        "stage": "due",
        "fire_at": MEETING.replace(hour=14, minute=55).isoformat(),
        "title": "созвон с Игорем",
        "due_at": MEETING.isoformat(),
        "due_precision": "time",
    }
    client = FakeRpcClient({"due_reminders": [row]})
    service = build_reminders_service(make_settings(), cast(Client, client), bot)

    assert await service.tick(MEETING.replace(hour=14, minute=55)) == 1
    sent = session.sent[0]
    assert isinstance(sent, SendMessage)
    assert sent.chat_id == OWNER_ID
    assert sent.text == "Напоминаю: созвон с Игорем\nСрок: сегодня, 15:00"
    assert sent.reply_markup is None


async def test_late_reminder_does_not_age_the_due_date() -> None:
    """Напоминание опоздало, а срок ещё впереди: «Срок», а не «Срок был»."""
    ripe = [make_due("before", FRIDAY_END_OF_DAY.replace(hour=9))]
    service, _, _, notifier = build_reminders(due=FakeDue(ripe))

    assert await service.tick(FRIDAY_END_OF_DAY.replace(hour=12)) == 1
    _, text = notifier.sent[0]
    assert text == "Напоминаю: отправить расчёт\nСрок: сегодня"


# Срок в напоминании и «Срок был» (`techspec/21-part-of-day.md` §21.3).

FRIDAY = FRIDAY_END_OF_DAY.date()
SATURDAY = date(2026, 9, 26)


def local(hour: int, minute: int = 0, second: int = 0, day: date = FRIDAY) -> datetime:
    """Этот момент пятницы, 25 сентября, — или другого дня — в поясе владельца."""
    return datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=TZ)


@pytest.mark.parametrize(
    ("now", "passed"),
    [
        (local(14, 59, 59), False),
        # Минута срока: тик пришёл с секундами — срок ещё не прошёл.
        (local(15, 0, 0), False),
        (local(15, 0, 42), False),
        (local(15, 1, 0), True),
    ],
)
def test_time_due_passes_after_its_minute(now: datetime, passed: bool) -> None:
    assert past_due(local(15), "time", now, TZ) is passed


@pytest.mark.parametrize("precision", ["day", "morning", "afternoon", "evening", None])
@pytest.mark.parametrize(
    ("now", "passed"),
    [(local(23, 59, 59), False), (local(0, 0, 0, day=SATURDAY), True)],
)
def test_day_and_part_pass_only_the_next_day(
    precision: str | None, now: datetime, passed: bool
) -> None:
    """У дела на день и части — день срока раньше сегодняшнего (§21.3)."""
    due = local(18) if precision in ("day", None) else local(8)
    assert past_due(due, precision, now, TZ) is passed


def test_day_passes_by_the_owner_timezone() -> None:
    """Полночь — по поясу владельца: в UTC ещё пятница, у владельца — суббота."""
    due = local(18).astimezone(UTC)
    now = local(0, 30, day=SATURDAY).astimezone(UTC)

    assert due.date() == now.date()
    assert past_due(due, "evening", now, TZ) is True


async def test_reminder_in_the_minute_of_the_due_says_due() -> None:
    """Ступень «к сроку» ушла в минуту срока: «Срок», а не «Срок был»."""
    ripe = [make_due("due", local(15), due_at=local(15), due_precision="time")]
    service, _, _, notifier = build_reminders(due=FakeDue(ripe))

    assert await service.tick(local(15, 0, 37)) == 1
    _, text = notifier.sent[0]
    assert text == "Напоминаю: отправить расчёт\nСрок: сегодня, 15:00"


async def test_reminder_after_the_minute_of_the_due_says_it_was() -> None:
    ripe = [make_due("due", local(15), due_at=local(15), due_precision="time")]
    service, _, _, notifier = build_reminders(due=FakeDue(ripe))

    assert await service.tick(local(15, 1)) == 1
    _, text = notifier.sent[0]
    assert text == "Напоминаю: отправить расчёт\nСрок был: сегодня, 15:00"


@pytest.mark.parametrize(
    ("precision", "start", "words"),
    [("morning", 8, "утром"), ("afternoon", 12, "днём"), ("evening", 18, "вечером")],
)
async def test_part_reminder_after_downtime_the_same_day_says_due(
    precision: str, start: int, words: str
) -> None:
    """Бот лежал с начала части — догнавшее напоминание всё ещё «Срок» (§21.3)."""
    ripe = [make_due("due", local(start), due_at=local(start), due_precision=precision)]
    service, _, _, notifier = build_reminders(due=FakeDue(ripe))

    assert await service.tick(local(23, 30)) == 1
    _, text = notifier.sent[0]
    assert text == f"Напоминаю: отправить расчёт\nСрок: сегодня {words}"


async def test_part_reminder_the_next_day_says_it_was() -> None:
    ripe = [make_due("due", local(8), due_at=local(8), due_precision="morning")]
    service, _, _, notifier = build_reminders(due=FakeDue(ripe))

    assert await service.tick(local(9, day=SATURDAY)) == 1
    _, text = notifier.sent[0]
    assert text == "Напоминаю: отправить расчёт\nСрок был: пятница, 25 сентября, утром"


# Строка «Перенёс» (`techspec/11-edit.md` §11.4). Срок сдвинули в приложении
# в понедельник утром: с пятницы 25-го на пятницу 2 октября.
NEXT_FRIDAY = datetime(2026, 10, 2, 18, 0, tzinfo=TZ)
MOVED_AT = datetime(2026, 9, 21, 9, 55, 12, 345678, tzinfo=ZoneInfo("UTC"))


def make_moved(**fields: object) -> MovedTask:
    """Задача с отметкой «срок перенесён», как её приносит `moved_tasks`."""
    base: dict[str, object] = {
        "id": "0e2f",
        "title": "отправить расчёт клиенту",
        "due_at": NEXT_FRIDAY,
        "due_precision": "day",
        "due_moved_at": MOVED_AT,
        # База отдаёт момент в UTC — строка называет его по часам владельца.
        "next_fire_at": NEXT_FRIDAY.replace(hour=9).astimezone(ZoneInfo("UTC")),
    }
    return MovedTask(**{**base, **fields})  # type: ignore[arg-type]


async def test_moved_due_is_announced_and_then_cleared() -> None:
    """Правка перенесла срок: строка в чат, потом снятие прочитанной отметки."""
    moved = FakeMoved([make_moved()])
    clear = FakeClearMoved()
    announcer = FakeAnnouncer()
    service, _, _, notifier = build_reminders(moved=moved, clear_moved=clear, announcer=announcer)

    assert await service.tick(MONDAY_MORNING) == 1
    assert moved.calls == [OWNER_ID]
    assert announcer.sent == [
        "Перенёс: отправить расчёт клиенту. Срок: пятница, 2 октября. Напомню: 2 октября в 09:00"
    ]
    assert clear.calls == [(OWNER_ID, "0e2f", MOVED_AT)]
    # Это не напоминание: кнопки «Сделано» под строкой нет.
    assert notifier.sent == []


async def test_due_with_an_hour_is_named_with_the_hour() -> None:
    at_three = NEXT_FRIDAY.replace(hour=15)
    announcer = FakeAnnouncer()
    service, _, _, _ = build_reminders(
        moved=FakeMoved(
            [
                make_moved(
                    due_at=at_three,
                    due_precision="time",
                    next_fire_at=at_three.replace(hour=14),
                )
            ]
        ),
        announcer=announcer,
    )

    await service.tick(MONDAY_MORNING)

    assert announcer.sent == [
        "Перенёс: отправить расчёт клиенту. Срок: пятница, 2 октября, 15:00. "
        "Напомню: 2 октября в 14:00"
    ]


async def test_reminders_go_first_then_moved_lines() -> None:
    """Сначала созревшее, потом «Перенёс»: «Напомню» не назовёт ушедшую ступень."""
    events: list[str] = []
    service, _, _, _ = build_reminders(
        due=FakeDue([make_due("due", FRIDAY_END_OF_DAY)]),
        notifier=FakeNotifier(events=events),
        moved=FakeMoved([make_moved()]),
        announcer=FakeAnnouncer(events=events),
    )

    assert await service.tick(FRIDAY_END_OF_DAY) == 2
    assert events == ["reminder", "moved"]


async def test_edit_between_read_and_clear_is_not_lost(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Отметка сменилась до снятия — она остаётся, и следующий тик скажет о новом сроке."""
    later = NEXT_FRIDAY.replace(day=9)
    second_mark = MOVED_AT.replace(minute=58)
    moved = FakeMoved([make_moved()])
    clear = FakeClearMoved(cleared=False)
    announcer = FakeAnnouncer()
    service, _, _, _ = build_reminders(moved=moved, clear_moved=clear, announcer=announcer)

    with caplog.at_level("INFO"):
        assert await service.tick(MONDAY_MORNING) == 1
    assert "сменилась" in caplog.text

    moved.tasks = [
        make_moved(
            due_at=later,
            due_moved_at=second_mark,
            next_fire_at=later.replace(hour=9),
        )
    ]
    clear.cleared = True
    assert await service.tick(MONDAY_MORNING) == 1

    assert announcer.sent[1] == (
        "Перенёс: отправить расчёт клиенту. Срок: пятница, 9 октября. Напомню: 9 октября в 09:00"
    )
    assert [call[2] for call in clear.calls] == [MOVED_AT, second_mark]


async def test_closed_task_gets_no_line() -> None:
    """Закрытую и удалённую задачу база не отдаёт — строки нет и снимать нечего."""
    clear = FakeClearMoved()
    announcer = FakeAnnouncer()
    service, _, _, _ = build_reminders(moved=FakeMoved([]), clear_moved=clear, announcer=announcer)

    assert await service.tick(MONDAY_MORNING) == 0
    assert announcer.sent == []
    assert clear.calls == []


async def test_past_due_has_no_remind_line() -> None:
    """Срок перенесли в прошлое — «Напомню» не звучит (§6.4)."""
    sunday = MONDAY_MORNING.replace(day=20, hour=18)
    announcer = FakeAnnouncer()
    service, _, _, _ = build_reminders(
        moved=FakeMoved([make_moved(due_at=sunday, next_fire_at=None)]),
        announcer=announcer,
    )

    await service.tick(MONDAY_MORNING)

    assert announcer.sent == ["Перенёс: отправить расчёт клиенту. Срок: воскресенье, 20 сентября"]


async def test_reminder_already_due_is_not_promised() -> None:
    """Ближайшее напоминание в прошлом (бот лежал) — обещать его поздно."""
    announcer = FakeAnnouncer()
    service, _, _, _ = build_reminders(
        moved=FakeMoved([make_moved(next_fire_at=MONDAY_MORNING.replace(hour=9))]),
        announcer=announcer,
    )

    await service.tick(MONDAY_MORNING)

    assert announcer.sent == ["Перенёс: отправить расчёт клиенту. Срок: пятница, 2 октября"]


async def test_removed_due_says_so() -> None:
    """Срок снят: «Убрал срок» и больше ничего не обещано."""
    announcer = FakeAnnouncer()
    service, _, _, _ = build_reminders(
        moved=FakeMoved([make_moved(due_at=None, due_precision=None, next_fire_at=None)]),
        announcer=announcer,
    )

    await service.tick(MONDAY_MORNING)

    assert announcer.sent == ["Убрал срок: отправить расчёт клиенту. Напоминать не буду."]
    assert texts.moved_reply(title="купить лампочку", due=None, remind_at=None) == (
        "Убрал срок: купить лампочку. Напоминать не буду."
    )


async def test_failed_line_keeps_the_mark() -> None:
    """Telegram не принял — отметка не снимается, следующий тик попробует снова."""
    clear = FakeClearMoved()
    service, _, _, _ = build_reminders(
        moved=FakeMoved([make_moved()]),
        clear_moved=clear,
        announcer=FakeAnnouncer(broken=True),
    )

    assert await service.tick(MONDAY_MORNING) == 0
    assert clear.calls == []


async def test_failed_clear_is_logged_and_the_line_counts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Ушло, но отметка не снята — строка в журнал: дубль лучше потери."""
    announcer = FakeAnnouncer()
    service, _, _, _ = build_reminders(
        moved=FakeMoved([make_moved()]),
        clear_moved=FakeClearMoved(broken=True),
        announcer=announcer,
    )

    with caplog.at_level("WARNING"):
        assert await service.tick(MONDAY_MORNING) == 1
    assert len(announcer.sent) == 1
    assert "отметка не снята" in caplog.text


async def test_moved_line_goes_to_the_owner_without_a_button(
    bot: Bot, session: RecordingSession
) -> None:
    """Сборка из `runner.py`: строка в чат владельца, без кнопки, и снятие отметки."""
    row: dict[str, object] = {
        "id": "0e2f",
        "title": "отправить расчёт клиенту",
        "due_at": NEXT_FRIDAY.isoformat(),
        "due_precision": "day",
        "due_moved_at": MOVED_AT.isoformat(),
        "next_fire_at": NEXT_FRIDAY.replace(hour=9).isoformat(),
    }
    # Утренний план в 10:00 уже был (§20.2): шаг плана только спрашивает базу.
    client = FakeRpcClient(
        {
            "morning_plan_sent": True,
            "due_reminders": [],
            "moved_tasks": [row],
            "clear_due_moved": True,
        }
    )
    service = build_reminders_service(make_settings(), cast(Client, client), bot)

    assert await service.tick(MONDAY_MORNING) == 1
    sent = session.sent[0]
    assert isinstance(sent, SendMessage)
    assert sent.chat_id == OWNER_ID
    assert sent.text.startswith("Перенёс: отправить расчёт клиенту.")
    assert sent.reply_markup is None
    assert client.calls == [
        "roll_repeats",
        "morning_plan_sent",
        "due_reminders",
        "moved_tasks",
        "clear_due_moved",
    ]
    assert client.params[-1] == {
        "owner_telegram_id": OWNER_ID,
        "task_id": "0e2f",
        "seen": MOVED_AT.isoformat(),
    }


# --- Вопрос о деле без срока (techspec/19-undated.md §19.2, §19.4) ---

UNDATED_ID = "5b0c7a52-8f3e-4c1d-9a6b-2e4f1d3c8b90"
# Суббота, 3 октября 2026 года, полдень у владельца: окно вопроса открыто.
SATURDAY_NOON = datetime(2026, 10, 3, 12, 0, tzinfo=TZ)


def make_undated(asked_at: datetime | None = None) -> UndatedTask:
    """Дело без срока, записанное вчера вечером, — как его отдаёт `undated_to_ask`."""
    return UndatedTask(
        task_id=UNDATED_ID,
        title="купить фильтр для воды",
        created_at=datetime(2026, 10, 2, 21, 30, tzinfo=TZ),
        asked_at=asked_at,
    )


class FakeUndated:
    """`undated_to_ask` без базы: какое дело отдать и с какими границами спросили."""

    def __init__(self, task: UndatedTask | None = None, broken: bool = False) -> None:
        self.task = task
        self.broken = broken
        self.calls: list[tuple[int, asks.AskBounds]] = []

    async def __call__(
        self, *, owner_telegram_id: int, bounds: asks.AskBounds
    ) -> UndatedTask | None:
        self.calls.append((owner_telegram_id, bounds))
        if self.broken:
            raise DatabaseError("ConnectTimeout: timed out")
        return self.task


class FakeAskRecorder:
    """`record_ask` без базы: что записали; `None` — дело уже не то."""

    def __init__(
        self, broken: bool = False, events: list[str] | None = None, missing: bool = False
    ) -> None:
        self.task = None if missing else make_details(id=UNDATED_ID)
        self.broken = broken
        self.calls: list[dict[str, object]] = []
        self.events = events if events is not None else []

    async def __call__(
        self, *, owner_telegram_id: int, task_id: str, question: str, telegram_message_id: int
    ) -> TaskDetails | None:
        self.events.append("record")
        if self.broken:
            raise DatabaseError("ConnectTimeout: timed out")
        self.calls.append(
            {
                "owner_telegram_id": owner_telegram_id,
                "task_id": task_id,
                "question": question,
                "telegram_message_id": telegram_message_id,
            }
        )
        return self.task


def build_asking(
    undated: FakeUndated,
    recorder: FakeAskRecorder | None = None,
    notifier: FakeNotifier | None = None,
    due: FakeDue | None = None,
    moved: FakeMoved | None = None,
) -> tuple[ReminderService, FakeAskRecorder, FakeNotifier]:
    """Сервис напоминаний с шагом вопроса — на подделках, без базы и сети."""
    record = recorder or FakeAskRecorder()
    sender = notifier or FakeNotifier()
    service = ReminderService(
        settings=make_settings(),
        due=due or FakeDue(),
        mark_sent=FakeMarks(),
        close_task=FakeCloser(),
        notify=sender,
        moved=moved or FakeMoved(),
        clear_moved=FakeClearMoved(),
        announce=FakeAnnouncer(),
        clock=lambda: SATURDAY_NOON,
        undated=undated,
        record_ask=record,
    )
    return service, record, sender


async def test_undated_question_is_sent_with_the_button_and_then_recorded() -> None:
    """Ушло с кнопкой «Сделано» — и только потом записано (§19.4)."""
    events: list[str] = []
    undated = FakeUndated(make_undated())
    service, recorder, notifier = build_asking(
        undated, FakeAskRecorder(events=events), FakeNotifier(events=events)
    )

    assert await service.tick(SATURDAY_NOON) == 1
    assert undated.calls == [(OWNER_ID, asks.bounds(SATURDAY_NOON, TZ))]
    assert notifier.sent == [
        (UNDATED_ID, "Вчера вы просили записать: купить фильтр для воды. Когда займётесь?")
    ]
    # Кнопка «Сделано» — та же, что под напоминанием; раза у дела без срока нет.
    assert notifier.occurrences == [None]
    assert events == ["reminder", "record"]
    assert recorder.calls == [
        {
            "owner_telegram_id": OWNER_ID,
            "task_id": UNDATED_ID,
            "question": texts.UNDATED_QUESTION,
            "telegram_message_id": 41,
        }
    ]


async def test_repeat_question_says_still_without_due() -> None:
    """Спрашивал неделю назад — повторный текст (§19.3)."""
    asked = datetime(2026, 9, 26, 11, 0, tzinfo=TZ)
    service, _, notifier = build_asking(FakeUndated(make_undated(asked_at=asked)))

    assert await service.tick(SATURDAY_NOON) == 1
    assert notifier.sent[0][1].startswith("Всё ещё без срока: купить фильтр для воды.")


@pytest.mark.parametrize(
    "moment",
    [
        datetime(2026, 10, 3, 9, 59, tzinfo=TZ),
        datetime(2026, 10, 3, 20, 0, tzinfo=TZ),
        datetime(2026, 10, 3, 23, 0, tzinfo=TZ),
        datetime(2026, 10, 3, 6, 0, tzinfo=TZ),
    ],
)
async def test_no_question_outside_the_window(moment: datetime) -> None:
    """До 10:00 и с 20:00 база о деле даже не спрашивается (§19.2)."""
    undated = FakeUndated(make_undated())
    service, recorder, notifier = build_asking(undated)

    assert await service.tick(moment) == 0
    assert undated.calls == []
    assert notifier.sent == []
    assert recorder.calls == []


async def test_no_question_when_a_reminder_went_out_this_tick() -> None:
    """В этом тике ушло напоминание — тишины нет, вопроса тоже (§19.2)."""
    undated = FakeUndated(make_undated())
    ripe = [make_due("due", SATURDAY_NOON, task_id="0e2f")]
    service, recorder, notifier = build_asking(undated, due=FakeDue(ripe))

    assert await service.tick(SATURDAY_NOON) == 1
    assert [task_id for task_id, _ in notifier.sent] == ["0e2f"]
    assert undated.calls == []
    assert recorder.calls == []


async def test_no_question_when_a_moved_line_went_out_this_tick() -> None:
    """Ушла строка «Перенёс» — вопрос ждёт следующей тишины (§19.2)."""
    undated = FakeUndated(make_undated())
    service, _, notifier = build_asking(undated, moved=FakeMoved([make_moved()]))

    assert await service.tick(SATURDAY_NOON) == 1
    assert notifier.sent == []
    assert undated.calls == []


async def test_failed_reminder_does_not_count_as_noise() -> None:
    """Напоминание не ушло — в этом тике ничего не ушло, и база решает о вопросе."""
    undated = FakeUndated(make_undated())
    ripe = [make_due("due", SATURDAY_NOON, task_id="0e2f")]
    service, _, _ = build_asking(undated, due=FakeDue(ripe), notifier=FakeNotifier(broken=True))

    assert await service.tick(SATURDAY_NOON) == 0
    assert len(undated.calls) == 1


async def test_one_question_a_day_by_process_memory() -> None:
    """Спросил — до конца дня владельца база о деле больше не спрашивается."""
    undated = FakeUndated(make_undated())
    service, recorder, notifier = build_asking(undated)

    assert await service.tick(SATURDAY_NOON) == 1
    assert await service.tick(SATURDAY_NOON.replace(hour=15)) == 0
    assert await service.tick(SATURDAY_NOON.replace(hour=19, minute=59)) == 0

    assert len(undated.calls) == 1
    assert len(notifier.sent) == 1
    assert len(recorder.calls) == 1


async def test_next_day_asks_again() -> None:
    """Назавтра с 10:00 — снова можно: память — о дне, а не навсегда."""
    service, _, notifier = build_asking(FakeUndated(make_undated()))

    assert await service.tick(SATURDAY_NOON) == 1
    assert await service.tick(datetime(2026, 10, 4, 10, 0, tzinfo=TZ)) == 1
    assert len(notifier.sent) == 2


async def test_nothing_to_ask_is_asked_again_next_tick() -> None:
    """Дела нет или нет тишины — база решает заново на следующем тике."""
    undated = FakeUndated(None)
    service, recorder, notifier = build_asking(undated)

    assert await service.tick(SATURDAY_NOON) == 0
    undated.task = make_undated()
    assert await service.tick(SATURDAY_NOON.replace(minute=1)) == 1

    assert len(undated.calls) == 2
    assert len(notifier.sent) == 1
    assert len(recorder.calls) == 1


async def test_failed_send_records_nothing_and_next_tick_asks_again(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Не ушло — ничего не записано, следующий тик того же дня спросит снова (§19.4)."""
    notifier = FakeNotifier(broken=True)
    service, recorder, _ = build_asking(FakeUndated(make_undated()), notifier=notifier)

    assert await service.tick(SATURDAY_NOON) == 0
    assert recorder.calls == []
    assert f"Вопрос о задаче {UNDATED_ID} не ушёл" in caplog.text

    notifier.broken = False
    assert await service.tick(SATURDAY_NOON.replace(minute=1)) == 1
    assert len(recorder.calls) == 1


async def test_failed_record_keeps_the_day(caplog: pytest.LogCaptureFixture) -> None:
    """Ушло, а база не записала: второго вопроса сегодня нет (§19.4)."""
    undated = FakeUndated(make_undated())
    service, _, notifier = build_asking(undated, FakeAskRecorder(broken=True))

    assert await service.tick(SATURDAY_NOON) == 1
    assert f"Вопрос о задаче {UNDATED_ID} ушёл, но не записан" in caplog.text
    assert any(record.levelname == "ERROR" for record in caplog.records)

    assert await service.tick(SATURDAY_NOON.replace(minute=1)) == 0
    assert len(undated.calls) == 1
    assert len(notifier.sent) == 1


async def test_task_changed_between_pick_and_record_is_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`record_ask` вернула `null`: дело за этот миг получило срок или закрыто."""
    service, _, notifier = build_asking(FakeUndated(make_undated()), FakeAskRecorder(missing=True))

    assert await service.tick(SATURDAY_NOON) == 1
    warnings = [record for record in caplog.records if record.levelname == "WARNING"]
    assert any("ушёл, но не записан" in record.getMessage() for record in warnings)

    assert await service.tick(SATURDAY_NOON.replace(minute=1)) == 0
    assert len(notifier.sent) == 1


async def test_broken_pick_does_not_kill_the_tick(caplog: pytest.LogCaptureFixture) -> None:
    """Сбой `undated_to_ask` — строка в журнал, тик живёт дальше (§19.4)."""
    service, recorder, notifier = build_asking(FakeUndated(broken=True))

    assert await service.tick(SATURDAY_NOON) == 0
    assert notifier.sent == []
    assert recorder.calls == []
    assert "Дело без срока для вопроса не выбрано" in caplog.text


async def test_log_names_the_task_not_its_title(caplog: pytest.LogCaptureFixture) -> None:
    """Журнал: id задачи и первый вопрос или повторный; сути дела в нём нет."""
    caplog.set_level(logging.INFO)
    asked = datetime(2026, 9, 26, 11, 0, tzinfo=TZ)
    first, _, _ = build_asking(FakeUndated(make_undated()))
    again, _, _ = build_asking(FakeUndated(make_undated(asked_at=asked)))

    await first.tick(SATURDAY_NOON)
    await again.tick(SATURDAY_NOON)

    assert f"Вопрос о задаче {UNDATED_ID}: первый" in caplog.text
    assert f"Вопрос о задаче {UNDATED_ID}: повторный" in caplog.text
    assert "фильтр" not in caplog.text


async def test_without_the_new_dependencies_there_is_no_question() -> None:
    """Сервис без поиска и записи вопроса — как до этапа 018: шага нет."""
    service, _, _, notifier = build_reminders()

    assert await service.tick(SATURDAY_NOON) == 0
    assert notifier.sent == []


async def test_undated_question_goes_to_the_owner_with_the_button(
    bot: Bot, session: RecordingSession
) -> None:
    """Сборка из `runner.py`: границы в базу, вопрос с кнопкой, запись с id сообщения."""
    created = datetime(2026, 10, 2, 21, 30, tzinfo=TZ).isoformat()
    row: dict[str, object] = {
        "task_id": UNDATED_ID,
        "title": "купить фильтр для воды",
        "created_at": created,
        "asked_at": None,
    }
    recorded: dict[str, object] = {
        "id": UNDATED_ID,
        "title": "купить фильтр для воды",
        "kind": "task",
        "status": "active",
        "due_at": None,
        "due_precision": None,
        "priority": "normal",
        "promise": None,
        "people": [],
        "created_at": created,
    }
    answers = {"due_reminders": [], "moved_tasks": [], "undated_to_ask": [row]}
    client = FakeRpcClient({**answers, "record_ask": recorded})
    service = build_reminders_service(make_settings(), cast(Client, client), bot)

    assert await service.tick(SATURDAY_NOON) == 1
    sent = session.sent[0]
    assert isinstance(sent, SendMessage)
    assert sent.chat_id == OWNER_ID
    assert sent.text == "Вчера вы просили записать: купить фильтр для воды. Когда займётесь?"
    assert isinstance(sent.reply_markup, InlineKeyboardMarkup)
    assert sent.reply_markup.inline_keyboard[0][0].callback_data == f"done:{UNDATED_ID}"
    # В полдень шаг о прошедшем деле уже открыт: дел нет — очередь вопроса
    # о деле без срока (§22.2).
    assert client.calls == [
        "roll_repeats",
        "due_reminders",
        "moved_tasks",
        "overdue_to_ask",
        "undated_to_ask",
        "record_ask",
    ]
    bounds = asks.bounds(SATURDAY_NOON, TZ)
    assert client.params[4] == {
        "owner_telegram_id": OWNER_ID,
        "day_start": bounds.day_start.isoformat(),
        "asked_before": bounds.asked_before.isoformat(),
        "question_since": bounds.question_since.isoformat(),
        "quiet_since": bounds.quiet_since.isoformat(),
    }
    assert client.params[5] == {
        "owner_telegram_id": OWNER_ID,
        "task_id": UNDATED_ID,
        "question": texts.UNDATED_QUESTION,
        "telegram_message_id": 1,
    }


# --- Утренний план (techspec/20-morning-plan.md §20.2, §20.4) ---

# Понедельник, 5 октября 2026 года, 08:00 у владельца: окно плана открылось.
PLAN_MORNING = datetime(2026, 10, 5, 8, 0, tzinfo=TZ)
PLAN_DAY = date(2026, 10, 5)
MEETING_ID = "7c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f"
BULB_ID = "0f0e0d0c-0b0a-4908-8706-050403020100"


def make_day_tasks() -> list[DayTask]:
    """Дела дня, как их отдаёт `day_tasks`: в порядке срока, дело на день — в 18:00."""
    return [
        DayTask(
            task_id=MEETING_ID,
            title="встреча с Ольгой",
            due_at=PLAN_MORNING.replace(hour=9),
            due_precision="time",
        ),
        DayTask(
            task_id=BULB_ID,
            title="купить лампочку в коридор",
            due_at=PLAN_MORNING.replace(hour=18),
            due_precision="day",
        ),
    ]


PLAN_TEXT = "\n".join(
    [
        "Доброе утро! На сегодня:",
        "09:00 — встреча с Ольгой",
        "В течение дня — купить лампочку в коридор",
    ]
)


class FakePlanSent:
    """`morning_plan_sent` без базы: был ли план и о каком дне спросили."""

    def __init__(self, sent: bool = False, broken: bool = False) -> None:
        self.sent = sent
        self.broken = broken
        self.calls: list[tuple[int, date]] = []

    async def __call__(self, *, owner_telegram_id: int, day: date) -> bool:
        self.calls.append((owner_telegram_id, day))
        if self.broken:
            raise DatabaseError("ConnectTimeout: timed out")
        return self.sent


class FakeDayTasks:
    """`day_tasks` без базы: какие дела отдать и с какими границами спросили."""

    def __init__(self, tasks: list[DayTask] | None = None, broken: bool = False) -> None:
        self.tasks = make_day_tasks() if tasks is None else tasks
        self.broken = broken
        self.calls: list[tuple[int, morning.DayBounds]] = []

    async def __call__(self, *, owner_telegram_id: int, bounds: morning.DayBounds) -> list[DayTask]:
        self.calls.append((owner_telegram_id, bounds))
        if self.broken:
            raise DatabaseError("ConnectTimeout: timed out")
        return list(self.tasks)


class FakePlanRecorder:
    """`record_morning_plan` без базы: что записали; `False` — план за день уже был."""

    def __init__(
        self, recorded: bool = True, broken: bool = False, events: list[str] | None = None
    ) -> None:
        self.recorded = recorded
        self.broken = broken
        self.calls: list[tuple[int, date, int]] = []
        self.events = events if events is not None else []

    async def __call__(
        self, *, owner_telegram_id: int, day: date, telegram_message_id: int
    ) -> bool:
        self.events.append("record")
        if self.broken:
            raise DatabaseError("ConnectTimeout: timed out")
        self.calls.append((owner_telegram_id, day, telegram_message_id))
        return self.recorded


def build_planning(
    plan_sent: FakePlanSent | None = None,
    day_tasks: FakeDayTasks | None = None,
    recorder: FakePlanRecorder | None = None,
    announcer: FakeAnnouncer | None = None,
    notifier: FakeNotifier | None = None,
    due: FakeDue | None = None,
    undated: FakeUndated | None = None,
) -> tuple[ReminderService, FakePlanSent, FakeDayTasks, FakePlanRecorder, FakeAnnouncer]:
    """Сервис напоминаний с шагом утреннего плана — на подделках, без базы и сети."""
    checker = plan_sent or FakePlanSent()
    lister = day_tasks or FakeDayTasks()
    record = recorder or FakePlanRecorder()
    speaker = announcer or FakeAnnouncer(label="plan")
    service = ReminderService(
        settings=make_settings(),
        due=due or FakeDue(),
        mark_sent=FakeMarks(),
        close_task=FakeCloser(),
        notify=notifier or FakeNotifier(),
        moved=FakeMoved(),
        clear_moved=FakeClearMoved(),
        announce=speaker,
        clock=lambda: PLAN_MORNING,
        undated=undated,
        record_ask=FakeAskRecorder() if undated is not None else None,
        plan_sent=checker,
        day_tasks=lister,
        record_plan=record,
    )
    return service, checker, lister, record, speaker


async def test_morning_plan_is_announced_and_then_recorded() -> None:
    """В 08:00 план уходит строкой без кнопки — и только потом записывается (§20.4)."""
    events: list[str] = []
    service, plan_sent, day_tasks, recorder, announcer = build_planning(
        recorder=FakePlanRecorder(events=events),
        announcer=FakeAnnouncer(label="plan", events=events),
    )

    assert await service.tick(PLAN_MORNING) == 1
    assert plan_sent.calls == [(OWNER_ID, PLAN_DAY)]
    assert day_tasks.calls == [(OWNER_ID, morning.day_bounds(PLAN_MORNING, TZ))]
    assert announcer.sent == [PLAN_TEXT]
    assert events == ["plan", "record"]
    assert recorder.calls == [(OWNER_ID, PLAN_DAY, 61)]


async def test_empty_day_plan_says_there_is_nothing() -> None:
    """Дел на сегодня нет — план всё равно уходит: «дел нет» тоже ответ (§20.3)."""
    service, _, _, recorder, announcer = build_planning(day_tasks=FakeDayTasks([]))

    assert await service.tick(PLAN_MORNING) == 1
    assert announcer.sent == ["Доброе утро! На сегодня дел нет."]
    assert len(recorder.calls) == 1


@pytest.mark.parametrize(
    ("moment", "planned"),
    [
        (datetime(2026, 10, 5, 7, 59, tzinfo=TZ), False),
        (datetime(2026, 10, 5, 8, 0, tzinfo=TZ), True),
        (datetime(2026, 10, 5, 11, 59, tzinfo=TZ), True),
        (datetime(2026, 10, 5, 12, 0, tzinfo=TZ), False),
        (datetime(2026, 10, 5, 23, 0, tzinfo=TZ), False),
        (datetime(2026, 10, 5, 3, 0, tzinfo=TZ), False),
    ],
)
async def test_plan_only_between_eight_and_noon(moment: datetime, planned: bool) -> None:
    """Вне 08:00–12:00 база о плане даже не спрашивается (§20.2)."""
    service, plan_sent, _, _, announcer = build_planning()

    assert await service.tick(moment) == (1 if planned else 0)
    assert bool(plan_sent.calls) is planned
    assert bool(announcer.sent) is planned


async def test_late_start_catches_up_with_the_whole_day() -> None:
    """Бот запустился в 11:00 — план первым тиком, и дело на 10:00 в нём есть (§20.1)."""
    late = PLAN_MORNING.replace(hour=11)
    passed = DayTask(
        task_id=MEETING_ID,
        title="встреча с Ольгой",
        due_at=PLAN_MORNING.replace(hour=10),
        due_precision="time",
    )
    service, _, _, _, announcer = build_planning(day_tasks=FakeDayTasks([passed]))

    assert await service.tick(late) == 1
    assert announcer.sent == ["Доброе утро! На сегодня:\n10:00 — встреча с Ольгой"]


async def test_start_at_noon_waits_for_tomorrow_morning() -> None:
    """С 12:00 сегодняшний план не догоняется; следующий — завтра в 08:00 (§20.2)."""
    service, plan_sent, _, _, announcer = build_planning()

    assert await service.tick(PLAN_MORNING.replace(hour=12)) == 0
    assert await service.tick(datetime(2026, 10, 6, 7, 59, tzinfo=TZ)) == 0
    assert plan_sent.calls == []

    assert await service.tick(datetime(2026, 10, 6, 8, 0, tzinfo=TZ)) == 1
    assert plan_sent.calls == [(OWNER_ID, date(2026, 10, 6))]
    assert len(announcer.sent) == 1


async def test_one_plan_a_day_by_process_memory() -> None:
    """План ушёл — до конца дня база о плане больше не спрашивается."""
    service, plan_sent, day_tasks, recorder, announcer = build_planning()

    assert await service.tick(PLAN_MORNING) == 1
    assert await service.tick(PLAN_MORNING.replace(minute=1)) == 0
    assert await service.tick(PLAN_MORNING.replace(hour=11, minute=59)) == 0

    assert len(plan_sent.calls) == 1
    assert len(day_tasks.calls) == 1
    assert len(announcer.sent) == 1
    assert len(recorder.calls) == 1


async def test_plan_already_in_the_database_is_not_sent_after_restart() -> None:
    """После перезапуска память процесса пуста, но строка в базе есть — второго плана нет."""
    service, plan_sent, day_tasks, recorder, announcer = build_planning(
        plan_sent=FakePlanSent(sent=True)
    )

    assert await service.tick(PLAN_MORNING.replace(hour=9)) == 0
    assert await service.tick(PLAN_MORNING.replace(hour=9, minute=1)) == 0

    # Нашёлся в базе — процесс запомнил день и больше туда не ходит.
    assert len(plan_sent.calls) == 1
    assert day_tasks.calls == []
    assert announcer.sent == []
    assert recorder.calls == []


async def test_next_day_plans_again() -> None:
    """Назавтра в 08:00 — снова план: память — о дне, а не навсегда."""
    service, _, _, _, announcer = build_planning()

    assert await service.tick(PLAN_MORNING) == 1
    assert await service.tick(datetime(2026, 10, 6, 8, 0, tzinfo=TZ)) == 1
    assert len(announcer.sent) == 2


async def test_reminder_of_the_same_minute_goes_after_the_plan() -> None:
    """Сначала обзор дня, потом созревшее напоминание — оба в одном тике (§20.2)."""
    events: list[str] = []
    ripe = [make_due("before", PLAN_MORNING, task_id=MEETING_ID)]
    service, _, _, _, _ = build_planning(
        announcer=FakeAnnouncer(label="plan", events=events),
        notifier=FakeNotifier(events=events),
        due=FakeDue(ripe),
    )

    assert await service.tick(PLAN_MORNING) == 2
    assert events == ["plan", "reminder"]


async def test_morning_part_goes_in_the_plan_and_reminds_right_after_it() -> None:
    """Дело на утро — и в плане, и напоминанием следом в том же тике (§21.3)."""
    events: list[str] = []
    meeting = DayTask(
        task_id=MEETING_ID,
        title="встреча с Ренатой",
        due_at=PLAN_MORNING,
        due_precision="morning",
    )
    ripe = [
        make_due(
            "due",
            PLAN_MORNING,
            task_id=MEETING_ID,
            title="встреча с Ренатой",
            due_at=PLAN_MORNING,
            due_precision="morning",
        )
    ]
    notifier = FakeNotifier(events=events)
    service, _, _, _, announcer = build_planning(
        day_tasks=FakeDayTasks([meeting]),
        announcer=FakeAnnouncer(label="plan", events=events),
        notifier=notifier,
        due=FakeDue(ripe),
    )

    assert await service.tick(PLAN_MORNING) == 2
    assert events == ["plan", "reminder"]
    assert announcer.sent == ["Доброе утро! На сегодня:\nУтром — встреча с Ренатой"]
    assert notifier.sent == [(MEETING_ID, "Напоминаю: встреча с Ренатой\nСрок: сегодня утром")]


async def test_no_undated_question_in_the_plan_tick() -> None:
    """План ушёл в этом тике — вопрос о деле без срока ждёт (§20.2, §19.2)."""
    late = PLAN_MORNING.replace(hour=10, minute=30)
    undated = FakeUndated(make_undated())
    service, _, _, _, announcer = build_planning(undated=undated)

    assert await service.tick(late) == 1
    assert announcer.sent == [PLAN_TEXT]
    assert undated.calls == []

    # Следующий тик плана не шлёт, и о вопросе решает база — с её 15 минутами.
    await service.tick(late.replace(minute=31))
    assert len(undated.calls) == 1


async def test_failed_send_records_nothing_and_next_tick_sends_again(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Не ушло — ничего не записано, следующий тик до 12:00 пришлёт снова (§20.4)."""
    announcer = FakeAnnouncer(label="plan", broken=True)
    service, _, _, recorder, _ = build_planning(announcer=announcer)

    assert await service.tick(PLAN_MORNING) == 0
    assert recorder.calls == []
    assert "Утренний план не ушёл" in caplog.text
    assert any(record.levelname == "WARNING" for record in caplog.records)

    announcer.broken = False
    assert await service.tick(PLAN_MORNING.replace(minute=1)) == 1
    assert announcer.sent == [PLAN_TEXT]
    assert len(recorder.calls) == 1


async def test_failed_plan_record_keeps_the_day(caplog: pytest.LogCaptureFixture) -> None:
    """Ушло, а база не записала: второго плана сегодня нет (§20.4)."""
    service, plan_sent, _, _, announcer = build_planning(recorder=FakePlanRecorder(broken=True))

    assert await service.tick(PLAN_MORNING) == 1
    assert "Утренний план ушёл, но не записан" in caplog.text
    assert any(record.levelname == "ERROR" for record in caplog.records)

    assert await service.tick(PLAN_MORNING.replace(minute=1)) == 0
    assert len(plan_sent.calls) == 1
    assert len(announcer.sent) == 1


async def test_plan_already_recorded_is_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    """`record_morning_plan` вернула `false`: строка за этот день уже была."""
    service, _, _, _, announcer = build_planning(recorder=FakePlanRecorder(recorded=False))

    assert await service.tick(PLAN_MORNING) == 1
    warnings = [record for record in caplog.records if record.levelname == "WARNING"]
    assert any("ушёл, но не записан" in record.getMessage() for record in warnings)

    assert await service.tick(PLAN_MORNING.replace(minute=1)) == 0
    assert len(announcer.sent) == 1


@pytest.mark.parametrize("broken", ["plan_sent", "day_tasks"])
async def test_broken_database_is_logged_and_reminders_still_go(
    broken: str, caplog: pytest.LogCaptureFixture
) -> None:
    """База не ответила при сборе — строка в журнал, напоминания уходят (§20.4)."""
    plan_sent = FakePlanSent(broken=broken == "plan_sent")
    day_tasks = FakeDayTasks(broken=broken == "day_tasks")
    notifier = FakeNotifier()
    due = FakeDue([make_due("before", PLAN_MORNING, task_id=MEETING_ID)])
    service, _, _, recorder, announcer = build_planning(
        plan_sent=plan_sent, day_tasks=day_tasks, notifier=notifier, due=due
    )

    assert await service.tick(PLAN_MORNING) == 1
    assert [task_id for task_id, _ in notifier.sent] == [MEETING_ID]
    assert announcer.sent == []
    assert recorder.calls == []
    assert "Утренний план не собран" in caplog.text
    assert any(record.levelname == "ERROR" for record in caplog.records)

    # Следующий тик до 12:00 пробует снова; напоминание уже ушло.
    plan_sent.broken = False
    day_tasks.broken = False
    due.ripe = []
    assert await service.tick(PLAN_MORNING.replace(minute=1)) == 1
    assert announcer.sent == [PLAN_TEXT]


async def test_log_counts_tasks_not_their_titles(caplog: pytest.LogCaptureFixture) -> None:
    """Журнал: сколько дел в ушедшем плане; сути дел в нём нет (§20.4)."""
    caplog.set_level(logging.INFO)
    service, _, _, _, _ = build_planning()

    await service.tick(PLAN_MORNING)

    assert "Утренний план ушёл: дел 2" in caplog.text
    assert "Ольг" not in caplog.text
    assert "лампочк" not in caplog.text


async def test_without_the_new_dependencies_there_is_no_plan() -> None:
    """Сервис без шага плана — как до этапа 019: в 08:00 ничего не уходит."""
    announcer = FakeAnnouncer()
    service, _, _, _ = build_reminders(announcer=announcer)

    assert await service.tick(PLAN_MORNING) == 0
    assert announcer.sent == []


async def test_morning_plan_goes_to_the_owner_without_a_button(
    bot: Bot, session: RecordingSession
) -> None:
    """Сборка из `runner.py`: после перекатывания, до напоминаний, без кнопки, с записью."""
    tasks: list[dict[str, object]] = [
        {
            "task_id": MEETING_ID,
            "title": "встреча с Ольгой",
            "due_at": "2026-10-05T04:00:00+00:00",
            "due_precision": "time",
        },
        {
            "task_id": BULB_ID,
            "title": "купить лампочку в коридор",
            "due_at": "2026-10-05T13:00:00+00:00",
            "due_precision": "day",
        },
    ]
    ripe: dict[str, object] = {
        "id": "b17c",
        "task_id": MEETING_ID,
        "stage": "before",
        "fire_at": PLAN_MORNING.isoformat(),
        "title": "встреча с Ольгой",
        "due_at": PLAN_MORNING.replace(hour=9).isoformat(),
        "due_precision": "time",
    }
    client = FakeRpcClient(
        {
            "morning_plan_sent": False,
            "day_tasks": tasks,
            "record_morning_plan": True,
            "due_reminders": [ripe],
            "moved_tasks": [],
        }
    )
    service = build_reminders_service(make_settings(), cast(Client, client), bot)

    assert await service.tick(PLAN_MORNING) == 2
    plan, reminder = session.sent[0], session.sent[1]
    assert isinstance(plan, SendMessage)
    assert plan.chat_id == OWNER_ID
    assert plan.text == PLAN_TEXT
    assert plan.reply_markup is None
    assert isinstance(reminder, SendMessage)
    # Следом — напоминание о встрече за час; у дела с часом оно тоже без
    # кнопки (этап 029, §6.3).
    assert reminder.text == "Напоминаю: встреча с Ольгой\nСрок: сегодня, 09:00"
    assert reminder.reply_markup is None
    # Прошедших дел нет: план без абзаца, записывать вопрос нечего (§22.4).
    assert client.calls == [
        "roll_repeats",
        "morning_plan_sent",
        "day_tasks",
        "overdue_to_ask",
        "record_morning_plan",
        "due_reminders",
        "mark_reminders_sent",
        "moved_tasks",
    ]
    bounds = morning.day_bounds(PLAN_MORNING, TZ)
    assert client.params[1] == {"owner_telegram_id": OWNER_ID, "day": "2026-10-05"}
    assert client.params[2] == {
        "owner_telegram_id": OWNER_ID,
        "day_start": bounds.day_start.isoformat(),
        "day_end": bounds.day_end.isoformat(),
    }
    # Строка в `morning_plans` — за сегодняшний день, с id сообщения плана.
    assert client.params[4] == {
        "owner_telegram_id": OWNER_ID,
        "day": "2026-10-05",
        "telegram_message_id": 1,
    }


# --- Вопрос о прошедшем деле (techspec/22-overdue.md §22.2, §22.4) ---

OVERDUE_ID = "3d4e5f60-7a8b-4c9d-8e0f-1a2b3c4d5e6f"
NEXT_OVERDUE_ID = "4e5f6071-8b9c-4dae-9f10-2b3c4d5e6f70"
# Понедельник, 5 октября 2026 года, 14:00 у владельца: план уже не догоняет.
MONDAY_AFTERNOON = datetime(2026, 10, 5, 14, 0, tzinfo=TZ)
YESTERDAY_EVENING = datetime(2026, 10, 4, 18, 0, tzinfo=TZ)
OCTOBER_SECOND = datetime(2026, 10, 2, 10, 0, tzinfo=TZ)
QUESTION_YESTERDAY = "Вчера осталось: позвонить в сервис. Получилось?"


def make_overdue(
    task_id: str = OVERDUE_ID,
    title: str = "позвонить в сервис",
    due_at: datetime = YESTERDAY_EVENING,
    asked_at: datetime | None = None,
) -> OverdueTask:
    """Задача со вчерашним сроком — как её отдаёт `overdue_to_ask`."""
    return OverdueTask(
        task_id=task_id, title=title, due_at=due_at, due_precision="time", asked_at=asked_at
    )


class FakeOverdue:
    """`overdue_to_ask` без базы: какое дело отдать и с какими границами спросили."""

    def __init__(self, task: OverdueTask | None = None, broken: bool = False) -> None:
        self.task = task
        self.broken = broken
        self.calls: list[tuple[int, overdue.OverdueBounds]] = []

    async def __call__(
        self, *, owner_telegram_id: int, bounds: overdue.OverdueBounds
    ) -> OverdueTask | None:
        self.calls.append((owner_telegram_id, bounds))
        if self.broken:
            raise DatabaseError("ConnectTimeout: timed out")
        return self.task


class FakeOverdueRecorder:
    """`record_overdue_ask` без базы: что записали; `None` — дело уже не то."""

    def __init__(
        self, broken: bool = False, events: list[str] | None = None, missing: bool = False
    ) -> None:
        self.missing = missing
        self.broken = broken
        self.calls: list[dict[str, object]] = []
        self.events = events if events is not None else []

    async def __call__(
        self,
        *,
        owner_telegram_id: int,
        task_id: str,
        question: str,
        telegram_message_id: int | None,
    ) -> TaskDetails | None:
        self.events.append("record_overdue")
        if self.broken:
            raise DatabaseError("ConnectTimeout: timed out")
        self.calls.append(
            {
                "owner_telegram_id": owner_telegram_id,
                "task_id": task_id,
                "question": question,
                "telegram_message_id": telegram_message_id,
            }
        )
        return None if self.missing else make_details(id=task_id)


def build_overdue(
    finder: FakeOverdue,
    recorder: FakeOverdueRecorder | None = None,
    notifier: FakeNotifier | None = None,
    *,
    planning: bool = False,
    plan_sent: FakePlanSent | None = None,
    day_tasks: FakeDayTasks | None = None,
    plan_recorder: FakePlanRecorder | None = None,
    announcer: FakeAnnouncer | None = None,
    due: FakeDue | None = None,
    moved: FakeMoved | None = None,
    undated: FakeUndated | None = None,
) -> tuple[ReminderService, FakeOverdueRecorder, FakeNotifier, FakeAnnouncer]:
    """Сервис напоминаний с вопросом о прошедшем деле; `planning` — и с планом."""
    record = recorder or FakeOverdueRecorder()
    sender = notifier or FakeNotifier()
    speaker = announcer or FakeAnnouncer(label="plan")
    service = ReminderService(
        settings=make_settings(),
        due=due or FakeDue(),
        mark_sent=FakeMarks(),
        close_task=FakeCloser(),
        notify=sender,
        moved=moved or FakeMoved(),
        clear_moved=FakeClearMoved(),
        announce=speaker,
        clock=lambda: MONDAY_AFTERNOON,
        undated=undated,
        record_ask=FakeAskRecorder() if undated is not None else None,
        plan_sent=(plan_sent or FakePlanSent()) if planning else None,
        day_tasks=(day_tasks or FakeDayTasks()) if planning else None,
        record_plan=(plan_recorder or FakePlanRecorder()) if planning else None,
        overdue_task=finder,
        record_overdue=record,
    )
    return service, record, sender, speaker


async def test_overdue_question_is_sent_with_the_button_and_then_recorded() -> None:
    """Отдельный вопрос: ушёл с кнопкой «Сделано» — и только потом записан (§22.4)."""
    events: list[str] = []
    finder = FakeOverdue(make_overdue())
    service, recorder, notifier, _ = build_overdue(
        finder, FakeOverdueRecorder(events=events), FakeNotifier(events=events)
    )

    assert await service.tick(MONDAY_AFTERNOON) == 1
    assert finder.calls == [(OWNER_ID, overdue.step_bounds(MONDAY_AFTERNOON, TZ))]
    assert notifier.sent == [(OVERDUE_ID, QUESTION_YESTERDAY)]
    # Кнопка «Сделано» — та же, что под напоминанием; задача разовая. Это
    # вопрос, а не напоминание: кнопка есть и у дела с часом (этап 029).
    assert notifier.occurrences == [None]
    assert notifier.buttons == [True]
    assert events == ["reminder", "record_overdue"]
    assert recorder.calls == [
        {
            "owner_telegram_id": OWNER_ID,
            "task_id": OVERDUE_ID,
            "question": "Получилось?",
            "telegram_message_id": 41,
        }
    ]


async def test_earlier_due_is_named_by_date() -> None:
    """Срок раньше вчерашнего — «Срок был 2 октября: …» (§22.3)."""
    finder = FakeOverdue(make_overdue(due_at=OCTOBER_SECOND))
    service, _, notifier, _ = build_overdue(finder)

    assert await service.tick(MONDAY_AFTERNOON) == 1
    assert notifier.sent[0][1] == "Срок был 2 октября: позвонить в сервис. Получилось?"


async def test_next_question_of_the_day_says_more() -> None:
    """Ответили, 15 минут тишины — следующий вопрос с «Ещё» (§22.2, §22.3)."""
    finder = FakeOverdue(make_overdue())
    service, recorder, notifier, _ = build_overdue(finder)

    assert await service.tick(MONDAY_AFTERNOON) == 1
    finder.task = make_overdue(task_id=NEXT_OVERDUE_ID, title="отправить расчёт")
    assert await service.tick(MONDAY_AFTERNOON.replace(minute=20)) == 1
    finder.task = make_overdue(task_id="5f60", title="забрать посылку", due_at=OCTOBER_SECOND)
    assert await service.tick(MONDAY_AFTERNOON.replace(minute=40)) == 1

    assert [text for _, text in notifier.sent] == [
        QUESTION_YESTERDAY,
        "Ещё вчера осталось: отправить расчёт. Получилось?",
        "Ещё одно, срок был 2 октября: забрать посылку. Получилось?",
    ]
    assert [call["task_id"] for call in recorder.calls] == [OVERDUE_ID, NEXT_OVERDUE_ID, "5f60"]


async def test_repeated_question_offers_to_remove_the_task() -> None:
    """Спрашивал неделю назад — в конце «Если уже не нужно…» (§22.3)."""
    asked = datetime(2026, 9, 28, 8, 0, tzinfo=TZ)
    finder = FakeOverdue(make_overdue(due_at=OCTOBER_SECOND.replace(day=1), asked_at=asked))
    service, _, notifier, _ = build_overdue(finder)

    assert await service.tick(MONDAY_AFTERNOON) == 1
    assert notifier.sent[0][1] == (
        "Срок был 1 октября: позвонить в сервис. Получилось? "
        "Если уже не нужно, скажите — уберу из списка."
    )


@pytest.mark.parametrize(
    "moment",
    [
        datetime(2026, 10, 5, 7, 59, tzinfo=TZ),
        datetime(2026, 10, 5, 20, 0, tzinfo=TZ),
        datetime(2026, 10, 5, 23, 0, tzinfo=TZ),
        datetime(2026, 10, 5, 3, 0, tzinfo=TZ),
    ],
)
async def test_no_overdue_question_outside_the_window(moment: datetime) -> None:
    """До 08:00 и с 20:00 база о прошедшем деле не спрашивается (§22.2)."""
    finder = FakeOverdue(make_overdue())
    service, recorder, notifier, _ = build_overdue(finder)

    assert await service.tick(moment) == 0
    assert finder.calls == []
    assert notifier.sent == []
    assert recorder.calls == []


@pytest.mark.parametrize(
    ("moment", "asked"),
    [
        (datetime(2026, 10, 5, 8, 0, tzinfo=TZ), False),
        (datetime(2026, 10, 5, 11, 59, tzinfo=TZ), False),
        (datetime(2026, 10, 5, 12, 0, tzinfo=TZ), True),
        (datetime(2026, 10, 5, 19, 59, tzinfo=TZ), True),
    ],
)
async def test_before_noon_the_step_waits_for_today_plan(moment: datetime, asked: bool) -> None:
    """Плана сегодня не было: до 12:00 шаг ждёт его, с 12:00 спрашивает сам (§22.2)."""
    finder = FakeOverdue(make_overdue())
    service, _, notifier, _ = build_overdue(finder)

    assert await service.tick(moment) == (1 if asked else 0)
    assert bool(finder.calls) is asked
    assert bool(notifier.sent) is asked


async def test_after_the_plan_the_step_goes_before_noon() -> None:
    """План с вопросом ушёл в 08:00 — в 08:20 следующий вопрос уже отдельно, с «Ещё»."""
    finder = FakeOverdue(make_overdue())
    service, recorder, notifier, announcer = build_overdue(finder, planning=True)

    assert await service.tick(PLAN_MORNING) == 1
    finder.task = make_overdue(task_id=NEXT_OVERDUE_ID, title="отправить расчёт")
    later = PLAN_MORNING.replace(minute=20)
    assert await service.tick(later) == 1

    assert announcer.sent[0].endswith("\n\n" + QUESTION_YESTERDAY)
    assert notifier.sent == [(NEXT_OVERDUE_ID, "Ещё вчера осталось: отправить расчёт. Получилось?")]
    assert finder.calls[1] == (OWNER_ID, overdue.step_bounds(later, TZ))
    assert [call["telegram_message_id"] for call in recorder.calls] == [None, 41]


async def test_plan_found_in_the_database_lets_the_step_go() -> None:
    """Перезапуск после плана: план уже в базе — шаг идёт и до 12:00 (§22.2)."""
    finder = FakeOverdue(make_overdue())
    service, _, notifier, announcer = build_overdue(
        finder, planning=True, plan_sent=FakePlanSent(sent=True)
    )

    assert await service.tick(PLAN_MORNING.replace(hour=9)) == 1
    assert announcer.sent == []
    assert notifier.sent == [(OVERDUE_ID, QUESTION_YESTERDAY)]


async def test_no_overdue_question_when_a_reminder_went_out_this_tick() -> None:
    """В этом тике ушло напоминание — тишины нет, вопроса тоже (§22.2)."""
    finder = FakeOverdue(make_overdue())
    ripe = [make_due("due", MONDAY_AFTERNOON, task_id="0e2f")]
    service, recorder, notifier, _ = build_overdue(finder, due=FakeDue(ripe))

    assert await service.tick(MONDAY_AFTERNOON) == 1
    assert [task_id for task_id, _ in notifier.sent] == ["0e2f"]
    assert finder.calls == []
    assert recorder.calls == []


async def test_no_overdue_question_when_a_moved_line_went_out_this_tick() -> None:
    """Ушла строка «Перенёс» — вопрос ждёт следующей тишины (§22.2)."""
    finder = FakeOverdue(make_overdue())
    service, _, notifier, _ = build_overdue(finder, moved=FakeMoved([make_moved()]))

    assert await service.tick(MONDAY_AFTERNOON) == 1
    assert notifier.sent == []
    assert finder.calls == []


async def test_undated_question_waits_while_overdue_ones_go() -> None:
    """Пока идут вопросы о прошедшем, вопрос о деле без срока ждёт; кончились — уходит."""
    finder = FakeOverdue(make_overdue())
    undated = FakeUndated(make_undated())
    service, _, notifier, _ = build_overdue(finder, undated=undated)

    assert await service.tick(MONDAY_AFTERNOON) == 1
    assert undated.calls == []

    finder.task = None
    assert await service.tick(MONDAY_AFTERNOON.replace(minute=20)) == 1
    assert len(undated.calls) == 1
    assert [task_id for task_id, _ in notifier.sent] == [OVERDUE_ID, UNDATED_ID]


async def test_nothing_overdue_is_asked_again_next_tick() -> None:
    """Дела нет, живой вопрос или нет тишины — база решает заново на следующем тике."""
    finder = FakeOverdue(None)
    service, recorder, notifier, _ = build_overdue(finder)

    assert await service.tick(MONDAY_AFTERNOON) == 0
    finder.task = make_overdue()
    assert await service.tick(MONDAY_AFTERNOON.replace(minute=1)) == 1

    assert len(finder.calls) == 2
    assert len(notifier.sent) == 1
    assert len(recorder.calls) == 1


async def test_failed_overdue_send_records_nothing_and_next_tick_asks_again(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Не ушло — ничего не записано, следующий тик спросит снова и без «Ещё» (§22.4)."""
    notifier = FakeNotifier(broken=True)
    service, recorder, _, _ = build_overdue(FakeOverdue(make_overdue()), notifier=notifier)

    assert await service.tick(MONDAY_AFTERNOON) == 0
    assert recorder.calls == []
    assert f"Вопрос о прошедшем деле {OVERDUE_ID} не ушёл" in caplog.text
    assert any(record.levelname == "WARNING" for record in caplog.records)

    notifier.broken = False
    assert await service.tick(MONDAY_AFTERNOON.replace(minute=1)) == 1
    assert notifier.sent == [(OVERDUE_ID, QUESTION_YESTERDAY)]
    assert len(recorder.calls) == 1


async def test_unrecorded_question_is_not_repeated_today(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Ушло, запись не легла, отбор вернул то же дело — до завтра шаг молчит (§22.4)."""
    finder = FakeOverdue(make_overdue())
    service, _, notifier, _ = build_overdue(finder, FakeOverdueRecorder(broken=True))

    assert await service.tick(MONDAY_AFTERNOON) == 1
    assert f"Вопрос о прошедшем деле {OVERDUE_ID} ушёл, но не записан" in caplog.text
    assert any(record.levelname == "ERROR" for record in caplog.records)

    assert await service.tick(MONDAY_AFTERNOON.replace(minute=20)) == 0
    assert await service.tick(MONDAY_AFTERNOON.replace(hour=16)) == 0
    assert len(notifier.sent) == 1
    # Второй тик увидел то же дело и остановил шаг; третий в базу не ходил.
    assert len(finder.calls) == 2

    assert await service.tick(datetime(2026, 10, 6, 12, 0, tzinfo=TZ)) == 1
    assert len(notifier.sent) == 2
    assert notifier.sent[1][1] == "Срок был 4 октября: позвонить в сервис. Получилось?"


async def test_overdue_task_changed_between_pick_and_record_is_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`record_overdue_ask` вернула `null`: дело за этот миг закрыли или перенесли."""
    service, _, notifier, _ = build_overdue(
        FakeOverdue(make_overdue()), FakeOverdueRecorder(missing=True)
    )

    assert await service.tick(MONDAY_AFTERNOON) == 1
    warnings = [record for record in caplog.records if record.levelname == "WARNING"]
    assert any("ушёл, но не записан" in record.getMessage() for record in warnings)
    assert len(notifier.sent) == 1


async def test_broken_overdue_pick_does_not_kill_the_tick(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Сбой `overdue_to_ask` — строка в журнал, шаг кончился, тик живёт (§22.4)."""
    service, recorder, notifier, _ = build_overdue(FakeOverdue(broken=True))

    assert await service.tick(MONDAY_AFTERNOON) == 0
    assert notifier.sent == []
    assert recorder.calls == []
    assert "Прошедшее дело для вопроса не выбрано" in caplog.text
    assert any(record.levelname == "ERROR" for record in caplog.records)


async def test_overdue_log_names_the_task_not_its_title(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Журнал: id задачи, первый или повторный, в плане или отдельно; сути нет."""
    caplog.set_level(logging.INFO)
    asked = datetime(2026, 9, 28, 8, 0, tzinfo=TZ)
    first, _, _, _ = build_overdue(FakeOverdue(make_overdue()))
    again, _, _, _ = build_overdue(FakeOverdue(make_overdue(asked_at=asked)))
    planned, _, _, _ = build_overdue(FakeOverdue(make_overdue()), planning=True)

    await first.tick(MONDAY_AFTERNOON)
    await again.tick(MONDAY_AFTERNOON)
    await planned.tick(PLAN_MORNING)

    assert f"Вопрос о прошедшем деле {OVERDUE_ID}: первый, отдельно" in caplog.text
    assert f"Вопрос о прошедшем деле {OVERDUE_ID}: повторный, отдельно" in caplog.text
    assert f"Вопрос о прошедшем деле {OVERDUE_ID}: первый, в плане" in caplog.text
    assert "сервис" not in caplog.text


async def test_without_the_overdue_dependencies_there_is_no_question() -> None:
    """Сервис без отбора и записи — как до этапа 022: ни шага, ни абзаца."""
    service, _, _, notifier = build_reminders()

    assert await service.tick(MONDAY_AFTERNOON) == 0
    assert notifier.sent == []

    planning, _, _, _, announcer = build_planning()
    assert await planning.tick(PLAN_MORNING) == 1
    assert announcer.sent == [PLAN_TEXT]


async def test_plan_asks_about_the_overdue_and_records_it_after_the_plan() -> None:
    """Абзац в плане (§22.2): отбор с границами плана, запись — после плана, без id."""
    events: list[str] = []
    finder = FakeOverdue(make_overdue())
    service, recorder, notifier, announcer = build_overdue(
        finder,
        FakeOverdueRecorder(events=events),
        planning=True,
        plan_recorder=FakePlanRecorder(events=events),
        announcer=FakeAnnouncer(label="plan", events=events),
    )

    assert await service.tick(PLAN_MORNING) == 1
    assert finder.calls == [(OWNER_ID, overdue.plan_bounds(PLAN_MORNING, TZ))]
    assert announcer.sent == [PLAN_TEXT + "\n\n" + QUESTION_YESTERDAY]
    # Кнопок под планом нет: отдельного сообщения с «Сделано» тоже.
    assert notifier.sent == []
    assert events == ["plan", "record", "record_overdue"]
    assert recorder.calls == [
        {
            "owner_telegram_id": OWNER_ID,
            "task_id": OVERDUE_ID,
            "question": "Получилось?",
            "telegram_message_id": None,
        }
    ]


async def test_empty_day_plan_keeps_the_overdue_question() -> None:
    """Дел на сегодня нет — «дел нет» и тот же абзац (§22.3)."""
    service, _, _, announcer = build_overdue(
        FakeOverdue(make_overdue(due_at=OCTOBER_SECOND)),
        planning=True,
        day_tasks=FakeDayTasks([]),
    )

    assert await service.tick(PLAN_MORNING) == 1
    assert announcer.sent == [
        "Доброе утро! На сегодня дел нет.\n\nСрок был 2 октября: позвонить в сервис. Получилось?"
    ]


async def test_broken_pick_leaves_the_plan_without_the_paragraph(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Сбой отбора — план уходит без абзаца, строка в журнал (§22.4)."""
    plan_recorder = FakePlanRecorder()
    service, recorder, _, announcer = build_overdue(
        FakeOverdue(broken=True), planning=True, plan_recorder=plan_recorder
    )

    assert await service.tick(PLAN_MORNING) == 1
    assert announcer.sent == [PLAN_TEXT]
    assert len(plan_recorder.calls) == 1
    assert recorder.calls == []
    assert "Прошедшее дело для вопроса не выбрано" in caplog.text


async def test_failed_plan_records_neither_plan_nor_question() -> None:
    """План не ушёл — ничего не записано; следующий тик пришлёт план с вопросом."""
    announcer = FakeAnnouncer(label="plan", broken=True)
    plan_recorder = FakePlanRecorder()
    service, recorder, _, _ = build_overdue(
        FakeOverdue(make_overdue()),
        planning=True,
        plan_recorder=plan_recorder,
        announcer=announcer,
    )

    assert await service.tick(PLAN_MORNING) == 0
    assert plan_recorder.calls == []
    assert recorder.calls == []

    announcer.broken = False
    assert await service.tick(PLAN_MORNING.replace(minute=1)) == 1
    # Вопрос — всё ещё первый за день: несостоявшийся не считается.
    assert announcer.sent == [PLAN_TEXT + "\n\n" + QUESTION_YESTERDAY]
    assert len(recorder.calls) == 1


async def test_failed_question_record_does_not_undo_the_plan(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Вопрос в плане не записан — строка в журнал; план ушёл и записан (§22.4)."""
    plan_recorder = FakePlanRecorder()
    service, _, _, announcer = build_overdue(
        FakeOverdue(make_overdue()),
        FakeOverdueRecorder(broken=True),
        planning=True,
        plan_recorder=plan_recorder,
    )

    assert await service.tick(PLAN_MORNING) == 1
    assert len(announcer.sent) == 1
    assert len(plan_recorder.calls) == 1
    assert f"Вопрос о прошедшем деле {OVERDUE_ID} ушёл, но не записан" in caplog.text


async def test_failed_plan_record_still_records_the_question(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """План не записался — вопрос всё равно записывается: он ушёл (§22.4)."""
    service, recorder, _, _ = build_overdue(
        FakeOverdue(make_overdue()),
        planning=True,
        plan_recorder=FakePlanRecorder(broken=True),
    )

    assert await service.tick(PLAN_MORNING) == 1
    assert "Утренний план ушёл, но не записан" in caplog.text
    assert len(recorder.calls) == 1


def overdue_rows() -> tuple[dict[str, object], dict[str, object]]:
    """Строка `overdue_to_ask` и задача из `record_overdue_ask` — как их отдаёт PostgREST."""
    due = YESTERDAY_EVENING.isoformat()
    row: dict[str, object] = {
        "task_id": OVERDUE_ID,
        "title": "позвонить в сервис",
        "due_at": due,
        "due_precision": "time",
        "asked_at": None,
    }
    recorded: dict[str, object] = {
        "id": OVERDUE_ID,
        "title": "позвонить в сервис",
        "kind": "task",
        "status": "active",
        "due_at": due,
        "due_precision": "time",
        "priority": "normal",
        "promise": None,
        "people": [],
        "created_at": datetime(2026, 10, 1, 9, 0, tzinfo=TZ).isoformat(),
    }
    return row, recorded


async def test_overdue_question_in_the_plan_goes_through_the_runner(
    bot: Bot, session: RecordingSession
) -> None:
    """Сборка из `runner.py`: план с абзацем, без кнопки; вопрос записан без id."""
    row, recorded = overdue_rows()
    client = FakeRpcClient(
        {
            "morning_plan_sent": False,
            "day_tasks": [],
            "overdue_to_ask": [row],
            "record_morning_plan": True,
            "record_overdue_ask": recorded,
            "due_reminders": [],
            "moved_tasks": [],
        }
    )
    service = build_reminders_service(make_settings(), cast(Client, client), bot)

    assert await service.tick(PLAN_MORNING) == 1
    plan = session.sent[0]
    assert isinstance(plan, SendMessage)
    assert plan.text == "Доброе утро! На сегодня дел нет.\n\n" + QUESTION_YESTERDAY
    assert plan.reply_markup is None
    assert client.calls == [
        "roll_repeats",
        "morning_plan_sent",
        "day_tasks",
        "overdue_to_ask",
        "record_morning_plan",
        "record_overdue_ask",
        "due_reminders",
        "moved_tasks",
    ]
    bounds = overdue.plan_bounds(PLAN_MORNING, TZ)
    assert client.params[3] == {
        "owner_telegram_id": OWNER_ID,
        "day_start": bounds.day_start.isoformat(),
        "asked_before": bounds.asked_before.isoformat(),
        "question_since": bounds.day_start.isoformat(),
        "quiet_since": None,
    }
    assert client.params[5] == {
        "owner_telegram_id": OWNER_ID,
        "task_id": OVERDUE_ID,
        "question": texts.OVERDUE_QUESTION,
        "telegram_message_id": None,
    }


async def test_separate_overdue_question_goes_through_the_runner(
    bot: Bot, session: RecordingSession
) -> None:
    """Сборка из `runner.py`: отдельный вопрос с кнопкой, запись с id сообщения."""
    row, recorded = overdue_rows()
    client = FakeRpcClient(
        {
            "due_reminders": [],
            "moved_tasks": [],
            "overdue_to_ask": [row],
            "record_overdue_ask": recorded,
        }
    )
    service = build_reminders_service(make_settings(), cast(Client, client), bot)

    assert await service.tick(MONDAY_AFTERNOON) == 1
    sent = session.sent[0]
    assert isinstance(sent, SendMessage)
    assert sent.chat_id == OWNER_ID
    assert sent.text == QUESTION_YESTERDAY
    assert isinstance(sent.reply_markup, InlineKeyboardMarkup)
    assert sent.reply_markup.inline_keyboard[0][0].callback_data == f"done:{OVERDUE_ID}"
    assert client.calls == [
        "roll_repeats",
        "due_reminders",
        "moved_tasks",
        "overdue_to_ask",
        "record_overdue_ask",
    ]
    bounds = overdue.step_bounds(MONDAY_AFTERNOON, TZ)
    assert bounds.quiet_since is not None
    assert client.params[3] == {
        "owner_telegram_id": OWNER_ID,
        "day_start": bounds.day_start.isoformat(),
        "asked_before": bounds.asked_before.isoformat(),
        "question_since": bounds.question_since.isoformat(),
        "quiet_since": bounds.quiet_since.isoformat(),
    }
    assert client.params[4] == {
        "owner_telegram_id": OWNER_ID,
        "task_id": OVERDUE_ID,
        "question": texts.OVERDUE_QUESTION,
        "telegram_message_id": 1,
    }
