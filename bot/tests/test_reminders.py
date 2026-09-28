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
from aiogram import Bot, Dispatcher
from aiogram.methods import SendMessage
from aiogram.types import InlineKeyboardMarkup
from supabase import Client

from solomon import texts
from solomon.db.reminders import DueReminder, MovedTask, Planned
from solomon.db.rpc import DatabaseError
from solomon.db.tasks import Task
from solomon.handlers import done_keyboard
from solomon.runner import build_dispatcher
from solomon.runner import build_reminders as build_reminders_service
from solomon.services.reminders import (
    ReminderService,
    by_task,
    database_planner,
    latest,
    mirror_timezone,
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
        self.events = events if events is not None else []

    async def __call__(self, *, text: str, task_id: str) -> int:
        if self.broken:
            raise RuntimeError("Telegram: Bad Gateway")
        self.sent.append((task_id, text))
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
    """Строка в чат без кнопки: вместо Telegram — список текстов."""

    def __init__(self, broken: bool = False, events: list[str] | None = None) -> None:
        self.broken = broken
        self.sent: list[str] = []
        self.events = events if events is not None else []

    async def __call__(self, *, text: str) -> int:
        if self.broken:
            raise RuntimeError("Telegram: Bad Gateway")
        self.sent.append(text)
        self.events.append("moved")
        return 60 + len(self.sent)


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
    closer = FakeCloser(Task(id="0e2f", title="отправить расчёт", status="done"))
    dispatcher = build_dispatcher_with(closer)

    await dispatcher.feed_update(bot, make_callback_update("done:0e2f"))

    assert closer.calls == [(OWNER_ID, "0e2f")]
    edit = session.edits[0]
    assert edit.text == "Напоминаю: отправить расчёт\n\n✓ Сделано"
    assert edit.reply_markup is None
    assert session.answers == [texts.DONE_ANSWER]


async def test_second_press_changes_nothing(bot: Bot, session: RecordingSession) -> None:
    """Повтор безвреден: задача уже закрыта, отметка уже стоит (§6.3)."""
    closer = FakeCloser(Task(id="0e2f", title="отправить расчёт", status="done"))
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
    closer = FakeCloser(Task(id="0e2f", title="отправить расчёт", status="done"))
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
    assert sent.text == "Напоминаю: отправить расчёт\nСрок: сегодня, 18:00"
    assert isinstance(sent.reply_markup, InlineKeyboardMarkup)
    assert sent.reply_markup.inline_keyboard[0][0].callback_data == "done:0e2f"
    assert client.calls == ["due_reminders", "mark_reminders_sent", "moved_tasks"]


async def test_late_reminder_does_not_age_the_due_date() -> None:
    """Напоминание опоздало, а срок ещё впереди: «Срок», а не «Срок был»."""
    ripe = [make_due("before", FRIDAY_END_OF_DAY.replace(hour=9))]
    service, _, _, notifier = build_reminders(due=FakeDue(ripe))

    assert await service.tick(FRIDAY_END_OF_DAY.replace(hour=12)) == 1
    _, text = notifier.sent[0]
    assert text == "Напоминаю: отправить расчёт\nСрок: сегодня, 18:00"


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
    client = FakeRpcClient({"due_reminders": [], "moved_tasks": [row], "clear_due_moved": True})
    service = build_reminders_service(make_settings(), cast(Client, client), bot)

    assert await service.tick(MONDAY_MORNING) == 1
    sent = session.sent[0]
    assert isinstance(sent, SendMessage)
    assert sent.chat_id == OWNER_ID
    assert sent.text.startswith("Перенёс: отправить расчёт клиенту.")
    assert sent.reply_markup is None
    assert client.calls == ["due_reminders", "moved_tasks", "clear_due_moved"]
    assert client.params[-1] == {
        "owner_telegram_id": OWNER_ID,
        "task_id": "0e2f",
        "seen": MOVED_AT.isoformat(),
    }
