"""Правка словом в Telegram: свайп уходит в сервис, кнопки — в сообщение, нажатия — в базу.

Решения сервиса проверяет `test_chat_edit_service.py`; здесь — обвязка
aiogram (`techspec/12-chat-edit.md` §12.2, §12.6): что обработчик берёт из
обновления и что делает с ответом сервиса.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
from aiogram import Bot
from aiogram.enums import MessageOriginType
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText, SendMessage, TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import (
    Chat,
    InlineKeyboardMarkup,
    Message,
    MessageOriginUser,
    Update,
    User,
    Voice,
)

from solomon import texts
from solomon.config import Settings
from solomon.handlers import keyboard, swipe_of
from solomon.runner import build_dispatcher
from solomon.services.tasks import Button, Swipe, TaskService
from solomon.services.understanding import Understanding
from tests.conftest import (
    OWNER_ID,
    STRANGER_ID,
    TEST_TOKEN,
    FakeAnalyst,
    FakeEdits,
    FakeMessages,
    FakePlanner,
    FakeTranscriber,
    FakeUnderstandings,
    RecordingSession,
    make_callback_update,
    make_understanding,
    make_update,
)
from tests.test_chat_edit_service import (
    LAMP,
    MEETING,
    MEETING_ID,
    MEETING_PLAN,
    MOVE_CANDIDATES,
    NOW,
    OPEN,
    REPORT,
    REPORT_ID,
    candidate_message,
    edited,
)
from tests.test_duplicates_service import MEETING_REPLY, duplicate_message, repeated

QUESTION_ID = 41


def build_tasks(
    settings: Settings,
    verdict: Understanding | None = None,
    store: FakeEdits | None = None,
    planner: FakePlanner | None = None,
) -> tuple[TaskService, FakeAnalyst, FakeEdits]:
    analyst = FakeAnalyst(verdict or make_understanding())
    edits = store if store is not None else FakeEdits(OPEN)
    service = TaskService(
        settings=settings,
        record_message=FakeMessages(),
        record_understanding=FakeUnderstandings(),
        analyst=analyst,
        transcriber=FakeTranscriber(),
        planner=planner or FakePlanner(),
        clock=lambda: NOW,
        edit_store=edits,
    )
    return service, analyst, edits


def owner() -> User:
    return User(id=OWNER_ID, is_bot=False, first_name="Тим")


def replied(text: str | None, *, from_bot: bool, message_id: int = 40, **extra: Any) -> Message:
    """Сообщение, на которое ответили свайпом: бота или своё."""
    author = User(id=1, is_bot=True, first_name="Соломон") if from_bot else owner()
    return Message(
        message_id=message_id,
        date=datetime.now(UTC),
        chat=Chat(id=OWNER_ID, type="private"),
        from_user=author,
        text=text,
        **extra,
    )


def reply_update(text: str, to: Message, update_id: int = 5, **extra: Any) -> Update:
    """Ответ владельца свайпом «ответить»."""
    message = Message(
        message_id=update_id,
        date=datetime.now(UTC),
        chat=Chat(id=OWNER_ID, type="private"),
        from_user=owner(),
        text=text,
        reply_to_message=to,
        **extra,
    )
    return Update(update_id=update_id, message=message)


def voice_reply_update(to: Message, update_id: int = 5) -> Update:
    message = Message(
        message_id=update_id,
        date=datetime.now(UTC),
        chat=Chat(id=OWNER_ID, type="private"),
        from_user=owner(),
        voice=Voice(file_id="voice-1", file_unique_id="voice-1", duration=3),
        reply_to_message=to,
    )
    return Update(update_id=update_id, message=message)


def rows(markup: Any) -> list[list[tuple[str, str | None]]]:
    assert isinstance(markup, InlineKeyboardMarkup)
    return [
        [(button.text, button.callback_data) for button in row] for row in markup.inline_keyboard
    ]


def sent_markups(session: RecordingSession) -> list[Any]:
    return [method.reply_markup for method in session.sent if isinstance(method, SendMessage)]


# ------------------------------------------------------------------ свайп


def test_swipe_of_a_bot_message_takes_its_id_and_text() -> None:
    update = reply_update("сделал", replied("Напоминаю: отчёт", from_bot=True))
    assert update.message is not None

    assert swipe_of(update.message) == Swipe(40, from_bot=True, text="Напоминаю: отчёт")


def test_swipe_of_a_captioned_message_takes_the_caption() -> None:
    update = reply_update("это", replied(None, from_bot=False, caption="чек за свет"))
    assert update.message is not None

    assert swipe_of(update.message) == Swipe(40, from_bot=False, text="чек за свет")


def test_swipe_of_a_voice_has_no_text() -> None:
    """У голосового текста в Telegram нет — расшифровку возьмёт сервис."""
    voice = replied(None, from_bot=False, voice=Voice(file_id="v", file_unique_id="v", duration=2))
    update = reply_update("и хлеб", voice)
    assert update.message is not None

    assert swipe_of(update.message) == Swipe(40, from_bot=False, text=None)


def test_plain_message_has_no_swipe() -> None:
    update = make_update("перенеси встречу")
    assert update.message is not None

    assert swipe_of(update.message) is None


def test_forwarded_message_swipe_is_not_read() -> None:
    origin = MessageOriginUser(
        type=MessageOriginType.USER,
        date=datetime.now(UTC),
        sender_user=User(id=555, is_bot=False, first_name="Аня"),
    )
    update = reply_update(
        "пришлю смету", replied("Напоминаю", from_bot=True), forward_origin=origin
    )
    assert update.message is not None

    assert swipe_of(update.message) is None


async def test_text_swipe_on_a_reminder_reaches_the_prompt(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    store = FakeEdits(OPEN, reminders={40: REPORT_ID})
    service, analyst, _ = build_tasks(settings, edited(2, action="done"), store)
    dispatcher = build_dispatcher(settings, tasks=service)

    await dispatcher.feed_update(
        bot, reply_update("сделал", replied("Напоминаю: отправить отчёт", from_bot=True))
    )

    assert analyst.swipes == ["Ответ на напоминание о задаче №2"]
    assert session.texts == ["Закрыл: отправить отчёт."]


async def test_voice_swipe_on_own_message_reaches_the_prompt(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    service, analyst, store = build_tasks(settings)
    dispatcher = build_dispatcher(settings, tasks=service)

    await dispatcher.feed_update(
        bot, voice_reply_update(replied("купить молоко", from_bot=False, message_id=39))
    )

    assert analyst.swipes == ["Ответ на своё сообщение: «купить молоко»"]
    assert ("message", OWNER_ID, 39) in store.calls


# ----------------------------------------------------------------- кнопки


def test_keyboard_puts_one_button_per_row() -> None:
    markup = keyboard((Button("первая", "pick:1:a"), Button("вторая", "pick:1:b")))

    assert rows(markup) == [[("первая", "pick:1:a")], [("вторая", "pick:1:b")]]


def test_no_buttons_means_no_keyboard() -> None:
    assert keyboard(()) is None


async def test_candidates_come_with_pick_buttons(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    service, _, _ = build_tasks(settings, MOVE_CANDIDATES)
    dispatcher = build_dispatcher(settings, tasks=service)

    await dispatcher.feed_update(bot, make_update("перенеси на понедельник", update_id=41))

    assert session.texts == ["Какую задачу перенести на понедельник, 5 октября?"]
    assert rows(sent_markups(session)[0]) == [
        [("отправить отчёт — 2 окт", f"pick:41:{REPORT_ID}")],
        [("встреча с Ренатой — 2 окт, 17:00", f"pick:41:{MEETING_ID}")],
    ]


async def test_closed_task_answer_comes_with_the_back_button(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    service, _, _ = build_tasks(settings, edited(1, action="done"))
    dispatcher = build_dispatcher(settings, tasks=service)

    await dispatcher.feed_update(bot, make_update("встречу провёл"))

    assert rows(sent_markups(session)[0]) == [[("Вернуть", f"reopen:{MEETING_ID}")]]


async def test_ordinary_answer_has_no_keyboard(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    service, _, _ = build_tasks(settings)
    dispatcher = build_dispatcher(settings, tasks=service)

    await dispatcher.feed_update(bot, make_update("купить лампочку"))

    assert sent_markups(session) == [None]


# ------------------------------------------------------ нажатие кандидата


def pick_press(task_id: str, update_id: int = 7) -> Update:
    return make_callback_update(
        f"pick:{QUESTION_ID}:{task_id}",
        text="Какую задачу перенести на понедельник, 5 октября?",
        update_id=update_id,
    )


async def test_pick_writes_first_and_then_replaces_the_question(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    store = FakeEdits(OPEN, messages={QUESTION_ID: candidate_message(MOVE_CANDIDATES)})
    service, _, _ = build_tasks(settings, store=store)
    dispatcher = build_dispatcher(settings, tasks=service)

    await dispatcher.feed_update(bot, pick_press(MEETING_ID))

    assert store.picks[0][1]["task_id"] == MEETING_ID
    assert [edit.text for edit in session.edits] == [
        "Перенёс: встреча с Ренатой. Срок: понедельник, 5 октября"
    ]
    assert session.edits[0].message_id == 7
    assert session.edits[0].reply_markup is None
    assert session.answers == [None]


async def test_pick_of_done_leaves_the_back_button(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    verdict = edited(None, action="done", candidates=[1, 2])
    store = FakeEdits(OPEN, messages={QUESTION_ID: candidate_message(verdict)})
    service, _, _ = build_tasks(settings, store=store)
    dispatcher = build_dispatcher(settings, tasks=service)

    await dispatcher.feed_update(bot, pick_press(REPORT_ID))

    assert session.edits[0].text == "Закрыл: отправить отчёт."
    assert rows(session.edits[0].reply_markup) == [[("Вернуть", f"reopen:{REPORT_ID}")]]


async def test_pick_the_base_refused_keeps_the_question(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    store = FakeEdits(
        OPEN, messages={QUESTION_ID: candidate_message(MOVE_CANDIDATES)}, broken={"pick"}
    )
    service, _, _ = build_tasks(settings, store=store)
    dispatcher = build_dispatcher(settings, tasks=service)

    await dispatcher.feed_update(bot, pick_press(MEETING_ID))

    assert session.edits == []
    assert session.answers == [texts.NOT_PICKED]


async def test_pick_of_a_closed_task_keeps_the_question(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    store = FakeEdits(
        [LAMP, REPORT, replace(MEETING, status="done")],
        messages={QUESTION_ID: candidate_message(MOVE_CANDIDATES)},
    )
    service, _, _ = build_tasks(settings, store=store)
    dispatcher = build_dispatcher(settings, tasks=service)

    await dispatcher.feed_update(bot, pick_press(MEETING_ID))

    assert session.edits == []
    assert session.answers == [texts.PICKED_GONE]


async def test_pick_without_a_database_says_it_did_not_write(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    dispatcher = build_dispatcher(settings)

    await dispatcher.feed_update(bot, pick_press(MEETING_ID))

    assert session.edits == []
    assert session.answers == [texts.NOT_PICKED]


async def test_pick_with_broken_data_touches_nothing(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    service, _, store = build_tasks(settings)
    dispatcher = build_dispatcher(settings, tasks=service)

    await dispatcher.feed_update(bot, make_callback_update("pick:сорок:не-id"))

    assert store.calls == []
    assert session.answers == [texts.DONE_UNKNOWN]


async def test_press_from_a_stranger_never_reaches_the_handler(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    service, _, store = build_tasks(settings)
    dispatcher = build_dispatcher(settings, tasks=service)

    await dispatcher.feed_update(
        bot, make_callback_update(f"pick:{QUESTION_ID}:{MEETING_ID}", from_id=STRANGER_ID)
    )
    await dispatcher.feed_update(
        bot, make_callback_update(f"reopen:{MEETING_ID}", from_id=STRANGER_ID, update_id=2)
    )

    assert store.calls == []
    assert session.sent == []


# ---------------------------------------------------------------- «Вернуть»


def reopen_press(task_id: str = MEETING_ID) -> Update:
    return make_callback_update(f"reopen:{task_id}", text="Закрыл: встреча с Ренатой.")


async def test_reopen_writes_first_and_then_says_back_in_work(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    store = FakeEdits([LAMP, REPORT, replace(MEETING, status="done")])
    service, _, _ = build_tasks(settings, store=store, planner=FakePlanner(MEETING_PLAN))
    dispatcher = build_dispatcher(settings, tasks=service)

    await dispatcher.feed_update(bot, reopen_press())

    assert store.reopens == [(MEETING_ID, MEETING_PLAN)]
    assert [edit.text for edit in session.edits] == [
        "Вернул в работу: встреча с Ренатой. Срок: пятница, 2 октября, 17:00. "
        "Напомню: 2 октября в 16:00"
    ]
    assert session.edits[0].reply_markup is None
    assert session.answers == [None]


async def test_reopen_the_base_refused_keeps_the_button(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    store = FakeEdits([replace(MEETING, status="done")], broken={"reopen"})
    service, _, _ = build_tasks(settings, store=store)
    dispatcher = build_dispatcher(settings, tasks=service)

    await dispatcher.feed_update(bot, reopen_press())

    assert session.edits == []
    assert session.answers == [texts.NOT_REOPENED]


async def test_reopen_of_a_deleted_task_says_so(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    service, _, _ = build_tasks(settings, store=FakeEdits([LAMP]))
    dispatcher = build_dispatcher(settings, tasks=service)

    await dispatcher.feed_update(bot, reopen_press())

    assert session.edits == []
    assert session.answers == [texts.DONE_UNKNOWN]


async def test_reopen_without_a_database_says_it_did_not_return(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    dispatcher = build_dispatcher(settings)

    await dispatcher.feed_update(bot, reopen_press())

    assert session.answers == [texts.NOT_REOPENED]


class StubbornSession(RecordingSession):
    """Telegram, который не даёт отредактировать сообщение."""

    async def make_request(
        self, bot: Bot, method: TelegramMethod[TelegramType], timeout: int | None = None
    ) -> TelegramType:
        if isinstance(method, EditMessageText):
            self.sent.append(method)
            raise TelegramBadRequest(method=method, message="Bad Request: message can't be edited")
        return await super().make_request(bot, method, timeout)


@pytest.fixture
async def stubborn() -> AsyncGenerator[tuple[Bot, StubbornSession], None]:
    session = StubbornSession()
    instance = Bot(token=TEST_TOKEN, session=session)
    yield instance, session
    await instance.session.close()


async def test_unedited_message_still_tells_what_was_written(
    stubborn: tuple[Bot, StubbornSession], settings: Settings
) -> None:
    """База записала, а сообщение не сменилось — ответ всплывает (инвариант 4)."""
    bot, session = stubborn
    store = FakeEdits([replace(MEETING, status="done")])
    service, _, _ = build_tasks(settings, store=store, planner=FakePlanner(MEETING_PLAN))
    dispatcher = build_dispatcher(settings, tasks=service)

    await dispatcher.feed_update(bot, reopen_press())

    assert len(store.reopens) == 1
    assert session.answers == [session.edits[0].text]


# ------------------------------------------------------- «Записать отдельно»


def apart_press(update_id: int = 7) -> Update:
    return make_callback_update(f"apart:{QUESTION_ID}", text=MEETING_REPLY, update_id=update_id)


def duplicate_store(**fields: Any) -> FakeEdits:
    return FakeEdits(OPEN, messages={QUESTION_ID: duplicate_message(repeated(1))}, **fields)


async def test_duplicate_answer_comes_with_the_apart_button(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    """Под «Это уже записано» — кнопка с номером сообщения владельца (§15.4)."""
    service, _, _ = build_tasks(settings, repeated(1))
    dispatcher = build_dispatcher(settings, tasks=service)

    await dispatcher.feed_update(bot, make_update("созвон с Ренатой", update_id=41))

    assert session.texts == [MEETING_REPLY]
    assert rows(sent_markups(session)[0]) == [[("Записать отдельно", "apart:41")]]


async def test_apart_writes_first_and_then_replaces_the_duplicate_answer(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    store = duplicate_store()
    service, _, _ = build_tasks(settings, store=store, planner=FakePlanner(MEETING_PLAN))
    dispatcher = build_dispatcher(settings, tasks=service)

    await dispatcher.feed_update(bot, apart_press())

    assert len(store.separates) == 1
    assert [edit.text for edit in session.edits] == [
        "Записал: созвон с Ренатой. Срок: пятница, 2 октября, 17:00. Напомню: 2 октября в 16:00"
    ]
    assert session.edits[0].message_id == 7
    assert session.edits[0].reply_markup is None
    assert session.answers == [None]


async def test_apart_the_base_refused_keeps_the_button(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    service, _, _ = build_tasks(settings, store=duplicate_store(broken={"record_separately"}))
    dispatcher = build_dispatcher(settings, tasks=service)

    await dispatcher.feed_update(bot, apart_press())

    assert session.edits == []
    assert session.answers == [texts.NOT_SAVED]


async def test_apart_without_a_database_says_it_did_not_write(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    dispatcher = build_dispatcher(settings)

    await dispatcher.feed_update(bot, apart_press())

    assert session.edits == []
    assert session.answers == [texts.NOT_SAVED]


async def test_apart_with_broken_data_touches_nothing(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    service, _, store = build_tasks(settings)
    dispatcher = build_dispatcher(settings, tasks=service)

    await dispatcher.feed_update(bot, make_callback_update("apart:сорок"))

    assert store.calls == []
    assert session.answers == ["Не нашёл это сообщение."]


async def test_apart_from_a_stranger_never_reaches_the_handler(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    store = duplicate_store()
    service, _, _ = build_tasks(settings, store=store)
    dispatcher = build_dispatcher(settings, tasks=service)

    await dispatcher.feed_update(
        bot, make_callback_update(f"apart:{QUESTION_ID}", from_id=STRANGER_ID)
    )

    assert store.calls == []
    assert session.sent == []


async def test_unedited_duplicate_answer_still_tells_what_was_written(
    stubborn: tuple[Bot, StubbornSession], settings: Settings
) -> None:
    """Задача легла, а «Это уже записано» не сменилось — ответ всплывает."""
    bot, session = stubborn
    store = duplicate_store()
    service, _, _ = build_tasks(settings, store=store)
    dispatcher = build_dispatcher(settings, tasks=service)

    await dispatcher.feed_update(bot, apart_press())

    assert len(store.separates) == 1
    assert session.answers == [session.edits[0].text]


# ------------------------------------------------------------------ /help


async def test_help_tells_about_editing_by_word(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    dispatcher = build_dispatcher(settings)

    await dispatcher.feed_update(bot, make_update("/help"))

    assert "перенести, поправить, закрыть или убрать и словом в чате" in session.texts[0]
    assert "задачу можно закрыть, изменить или удалить" in session.texts[0]
