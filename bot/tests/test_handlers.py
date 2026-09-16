"""Бот отвечает владельцу и не отвечает по существу никому другому."""

from __future__ import annotations

from aiogram import Bot

from solomon import texts
from solomon.config import Settings
from solomon.runner import build_dispatcher
from tests.conftest import OWNER_ID, STRANGER_ID, RecordingSession, make_update


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


async def test_owner_id_is_the_only_gate(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    dispatcher = build_dispatcher(settings)

    await dispatcher.feed_update(bot, make_update("привет", from_id=OWNER_ID, update_id=2))
    await dispatcher.feed_update(bot, make_update("привет", from_id=STRANGER_ID, update_id=3))

    # Владельцу на свободный текст пока отвечать нечем — обработчика нет;
    # чужому уходит короткий отказ, и дальше обновление не идёт.
    assert session.texts == [texts.STRANGER]
