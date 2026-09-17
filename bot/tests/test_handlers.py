"""Бот отвечает владельцу, записывает поручения и не отвечает по существу чужим."""

from __future__ import annotations

from aiogram import Bot

from solomon import texts
from solomon.config import Settings
from solomon.runner import build_dispatcher
from solomon.services.tasks import TaskService
from tests.conftest import (
    OWNER_ID,
    STRANGER_ID,
    BrokenRecorder,
    FakeRecorder,
    RecordingSession,
    make_update,
    make_voice_update,
)


async def test_start_answers_in_russian(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    dispatcher = build_dispatcher(settings)

    await dispatcher.feed_update(bot, make_update("/start"))

    assert session.texts == [texts.START]
    assert "Здравствуйте" in session.texts[0]


async def test_help_lists_working_commands(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    dispatcher = build_dispatcher(settings)

    await dispatcher.feed_update(bot, make_update("/help"))

    assert session.texts == [texts.HELP]
    assert "/start" in session.texts[0]


async def test_stranger_is_turned_away(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    dispatcher = build_dispatcher(settings)

    await dispatcher.feed_update(bot, make_update("/start", from_id=STRANGER_ID))

    assert session.texts == [texts.STRANGER]


async def test_text_is_recorded_and_confirmed(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    recorder = FakeRecorder()
    dispatcher = build_dispatcher(settings, tasks=TaskService(settings, recorder))

    await dispatcher.feed_update(bot, make_update("купить лампочку в коридор", update_id=5))

    assert session.texts == [texts.RECORDED.format(text="купить лампочку в коридор")]
    assert recorder.calls == [
        {
            "owner_telegram_id": OWNER_ID,
            "chat_id": OWNER_ID,
            "telegram_message_id": 5,
            "text": "купить лампочку в коридор",
        }
    ]


async def test_voice_is_refused_and_nothing_is_saved(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    recorder = FakeRecorder()
    dispatcher = build_dispatcher(settings, tasks=TaskService(settings, recorder))

    await dispatcher.feed_update(bot, make_voice_update(update_id=6))

    assert session.texts == [texts.NOT_TEXT]
    assert recorder.calls == []


async def test_broken_database_is_not_called_recorded(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    dispatcher = build_dispatcher(settings, tasks=TaskService(settings, BrokenRecorder()))

    await dispatcher.feed_update(bot, make_update("купить лампочку", update_id=7))

    # Инвариант 4: о записи сообщается только после ответа базы.
    assert session.texts == [texts.NOT_SAVED]
    assert "Записал" not in session.texts[0]


async def test_repeated_update_is_confirmed_twice(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    recorder = FakeRecorder()
    dispatcher = build_dispatcher(settings, tasks=TaskService(settings, recorder))

    await dispatcher.feed_update(bot, make_update("купить лампочку", update_id=8))
    await dispatcher.feed_update(bot, make_update("купить лампочку", update_id=8))

    # Вторую задачу не заводит база (unique в §3.2), а человеку отвечаем оба
    # раза: первый ответ он мог не увидеть.
    assert session.texts == [
        texts.RECORDED.format(text="купить лампочку"),
        texts.RECORDED.format(text="купить лампочку"),
    ]


async def test_owner_id_is_the_only_gate(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    dispatcher = build_dispatcher(settings, tasks=TaskService(settings, FakeRecorder()))

    await dispatcher.feed_update(bot, make_update("привет", from_id=OWNER_ID, update_id=2))
    await dispatcher.feed_update(bot, make_update("привет", from_id=STRANGER_ID, update_id=3))

    # Владельцу текст записывается, чужому уходит короткий отказ, и дальше
    # обновление не идёт — до слоя данных оно не доходит.
    assert session.texts == [texts.RECORDED.format(text="привет"), texts.STRANGER]
