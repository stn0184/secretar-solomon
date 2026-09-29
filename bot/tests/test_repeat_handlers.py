"""Повторяющиеся задачи в Telegram: кнопка «Сделано» с разом и «Вернуть».

Решения сервисов проверяет `test_repeat_service.py`; здесь — обвязка aiogram
(`techspec/13-repeat.md` §13.3): что обработчик берёт из callback, в каком
порядке идут база и правка сообщения и что уходит в ответ.
"""

from __future__ import annotations

from typing import cast

import pytest
from aiogram import Bot, Dispatcher
from aiogram.methods import SendMessage
from aiogram.types import InlineKeyboardMarkup, Update
from supabase import Client

from solomon import texts
from solomon.config import Settings
from solomon.handlers import done_keyboard, parse_done
from solomon.runner import build_dispatcher
from solomon.runner import build_reminders as build_reminders_service
from solomon.services.reminders import ReminderService
from solomon.services.tasks import TaskService
from solomon.services.understanding import Understanding
from tests.conftest import (
    OWNER_ID,
    STRANGER_ID,
    FakeAnalyst,
    FakeEdits,
    FakeMessages,
    FakeNext,
    FakePlanner,
    FakeTranscriber,
    FakeUnderstandings,
    RecordingSession,
    make_callback_update,
    make_settings,
    make_update,
)
from tests.test_chat_edit_handlers import rows, sent_markups
from tests.test_chat_edit_service import edited
from tests.test_reminders import (
    FRIDAY_END_OF_DAY,
    FakeAnnouncer,
    FakeClearMoved,
    FakeCloser,
    FakeDue,
    FakeMarks,
    FakeMoved,
    FakeNotifier,
    FakeRpcClient,
)
from tests.test_repeat_service import (
    AHEAD_DONE,
    FRIDAYS,
    MONDAY,
    MONDAY_PLAN,
    MONDAY_REMIND,
    MONDAY_UTC,
    NEXT_MONDAY,
    NEXT_WORDS,
    NOW,
    OPEN,
    PAST_MONDAY,
    REPORT_ID,
    repeating,
    seconds,
)

TASK_UUID = "0e2f8a3e-7c55-4b8e-9a0f-4e6b2c1d0a11"
REMINDER = "Напоминаю: отправить расчёт"
NEXT_MARK = f"✓ Сделано. Следующий раз: {NEXT_WORDS}"


def reminders_with(closer: FakeCloser) -> Dispatcher:
    """Диспетчер с подменённой кнопкой «Сделано»: сети и базы нет."""
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


# ---------------------------------------------------------------- «Сделано»


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ("done:0e2f", ("0e2f", None)),
        ("done:0e2f:1790000000", ("0e2f", 1790000000)),
        ("done:0e2f:", ("0e2f", None)),
        ("done:", None),
        ("done::1790000000", None),
        ("done:0e2f:-5", None),
        ("done:0e2f:17x", None),
        # Цифры не ASCII: `isdigit` их пропустил бы, а `int` принял бы.
        ("done:0e2f:١٢", None),
    ],
)
def test_done_data_is_parsed_strictly(data: str, expected: tuple[str, int | None] | None) -> None:
    assert parse_done(data) == expected


def test_reminder_of_a_repeating_task_carries_its_time_in_the_button() -> None:
    """`done:<id>:<раз>` — и с настоящим uuid влезает в 64 байта Telegram."""
    keyboard = done_keyboard(TASK_UUID, 1790000000)

    data = keyboard.inline_keyboard[0][0].callback_data
    assert data == f"done:{TASK_UUID}:1790000000"
    assert len(data.encode()) <= 64


async def test_done_moves_the_repeating_task_and_names_the_next_time(
    bot: Bot, session: RecordingSession
) -> None:
    """Сначала база, потом отметка со сроком, какой она вернула (§13.3)."""
    closer = FakeCloser(repeating())
    dispatcher = reminders_with(closer)
    occurrence = seconds(FRIDAY_END_OF_DAY)

    await dispatcher.feed_update(bot, make_callback_update(f"done:0e2f:{occurrence}"))

    assert closer.calls == [(OWNER_ID, "0e2f")]
    assert closer.occurrences == [occurrence]
    edit = session.edits[0]
    assert edit.text == f"{REMINDER}\n\n{NEXT_MARK}"
    assert edit.reply_markup is None
    assert session.answers == [f"Следующий раз: {NEXT_WORDS}"]


async def test_old_button_without_a_time_moves_the_current_one(
    bot: Bot, session: RecordingSession
) -> None:
    closer = FakeCloser(repeating())
    dispatcher = reminders_with(closer)

    await dispatcher.feed_update(bot, make_callback_update("done:0e2f"))

    assert closer.occurrences == [None]
    assert session.edits[0].text == f"{REMINDER}\n\n{NEXT_MARK}"


async def test_second_press_leaves_the_message_as_it_is(
    bot: Bot, session: RecordingSession
) -> None:
    """Отметка уже та же — редактировать нечего; ответ называет срок из базы."""
    dispatcher = reminders_with(FakeCloser(repeating()))
    marked = f"{REMINDER}\n\n{NEXT_MARK}"

    await dispatcher.feed_update(bot, make_callback_update("done:0e2f:1790000000", text=marked))

    assert session.edits == []
    assert session.answers == [f"Следующий раз: {NEXT_WORDS}"]


async def test_crooked_done_data_does_not_reach_the_database(
    bot: Bot, session: RecordingSession
) -> None:
    closer = FakeCloser(repeating())
    dispatcher = reminders_with(closer)

    await dispatcher.feed_update(bot, make_callback_update("done:0e2f:17x"))

    assert closer.calls == []
    assert session.edits == []
    assert session.answers == [texts.DONE_UNKNOWN]


async def test_reminder_of_a_repeating_task_goes_with_its_time(
    bot: Bot, session: RecordingSession
) -> None:
    """Сборка из `runner.py` целиком: раз из выборки базы — в кнопку."""
    row: dict[str, object] = {
        "id": "b17c",
        "task_id": "0e2f",
        "stage": "due",
        "fire_at": FRIDAY_END_OF_DAY.isoformat(),
        "title": "отправить расчёт",
        "due_at": FRIDAY_END_OF_DAY.isoformat(),
        "due_precision": "day",
        "repeat": FRIDAYS,
        "occurrence_at": FRIDAY_END_OF_DAY.isoformat(),
    }
    client = FakeRpcClient({"due_reminders": [row], "roll_repeats": 0})
    service = build_reminders_service(make_settings(), cast(Client, client), bot)

    assert await service.tick(FRIDAY_END_OF_DAY) == 1
    sent = session.sent[0]
    assert isinstance(sent, SendMessage)
    assert sent.text == "Напоминаю: отправить расчёт\nСрок: сегодня, 18:00"
    assert isinstance(sent.reply_markup, InlineKeyboardMarkup)
    data = sent.reply_markup.inline_keyboard[0][0].callback_data
    assert data == f"done:0e2f:{seconds(FRIDAY_END_OF_DAY)}"
    assert client.calls[0] == "roll_repeats"


# ----------------------------------------------------------------- «Вернуть»


def tasks_with(
    settings: Settings, store: FakeEdits, verdict: Understanding | None = None
) -> TaskService:
    return TaskService(
        settings=settings,
        record_message=FakeMessages(),
        record_understanding=FakeUnderstandings(),
        analyst=FakeAnalyst(verdict or edited(1, action="done")),
        transcriber=FakeTranscriber(),
        planner=FakePlanner(MONDAY_PLAN),
        clock=lambda: NOW,
        edit_store=store,
        repeat_next=FakeNext(MONDAY_UTC),
    )


BACK = f"back:{REPORT_ID}:{seconds(MONDAY)}:{seconds(NEXT_MONDAY)}"


def back_press(data: str = BACK) -> Update:
    return make_callback_update(data, text="Отметил: отправить отчёт.")


async def test_word_done_comes_with_the_back_button(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    dispatcher = build_dispatcher(settings, tasks=tasks_with(settings, FakeEdits(OPEN)))

    await dispatcher.feed_update(bot, make_update("отчёт отправил"))

    assert session.texts[-1].startswith("Отметил: отправить отчёт. Следующий раз:")
    back = f"back:{REPORT_ID}:{seconds(PAST_MONDAY)}:{seconds(MONDAY)}"
    assert rows(sent_markups(session)[-1]) == [[("Вернуть", back)]]


async def test_back_writes_first_and_then_says_back_in_work(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    store = FakeEdits([AHEAD_DONE])
    dispatcher = build_dispatcher(settings, tasks=tasks_with(settings, store))

    await dispatcher.feed_update(bot, back_press())

    assert store.returns == [(REPORT_ID, seconds(MONDAY), seconds(NEXT_MONDAY), MONDAY_PLAN)]
    assert [edit.text for edit in session.edits] == [
        "Вернул в работу: отправить отчёт. Повтор: каждый понедельник. "
        f"Срок: понедельник, 5 октября. {MONDAY_REMIND}"
    ]
    assert session.edits[0].reply_markup is None
    assert session.answers == [None]


async def test_back_after_the_task_went_further_keeps_the_message(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    store = FakeEdits([AHEAD_DONE])
    dispatcher = build_dispatcher(settings, tasks=tasks_with(settings, store))
    stale = f"back:{REPORT_ID}:{seconds(PAST_MONDAY)}:{seconds(MONDAY)}"

    await dispatcher.feed_update(bot, back_press(stale))

    assert store.returns == []
    assert session.edits == []
    assert session.answers == [texts.GONE_FURTHER]


async def test_crooked_back_data_does_not_reach_the_database(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    store = FakeEdits([AHEAD_DONE])
    dispatcher = build_dispatcher(settings, tasks=tasks_with(settings, store))

    await dispatcher.feed_update(bot, back_press(f"back:{REPORT_ID}:x:1"))

    assert store.calls == []
    assert session.answers == [texts.DONE_UNKNOWN]


async def test_back_without_a_database_says_it_did_not_return(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    dispatcher = build_dispatcher(settings)

    await dispatcher.feed_update(bot, back_press())

    assert session.edits == []
    assert session.answers == [texts.NOT_REOPENED]


async def test_back_from_a_stranger_never_reaches_the_handler(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    store = FakeEdits([AHEAD_DONE])
    dispatcher = build_dispatcher(settings, tasks=tasks_with(settings, store))

    await dispatcher.feed_update(bot, make_callback_update(BACK, from_id=STRANGER_ID))

    assert store.calls == []
    assert session.edits == []


async def test_help_names_repeating_tasks(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    dispatcher = build_dispatcher(settings)

    await dispatcher.feed_update(bot, make_update("/help"))

    assert session.texts == [texts.HELP]
    assert "повторяющейся" in session.texts[0]
