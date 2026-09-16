"""Обработчики команд. Сюда приходит только владелец — см. middlewares."""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from solomon import texts

logger = logging.getLogger(__name__)


async def handle_start(message: Message) -> None:
    """Приветствие владельцу."""
    logger.info("Команда /start")
    await message.answer(texts.START)


async def handle_help(message: Message) -> None:
    """Короткий список того, что уже работает."""
    logger.info("Команда /help")
    await message.answer(texts.HELP)


def build_router() -> Router:
    """Новый роутер команд.

    Именно фабрика, а не общий объект модуля: роутер aiogram привязывается
    к одному диспетчеру навсегда, и второй сборке достался бы занятый.
    """
    router = Router(name="basic")
    router.message.register(handle_start, CommandStart())
    router.message.register(handle_help, Command("help"))
    return router
