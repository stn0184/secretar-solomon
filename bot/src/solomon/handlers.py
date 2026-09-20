"""Обработчики сообщений. Сюда приходит только владелец — см. middlewares."""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message,
    MessageOriginChannel,
    MessageOriginChat,
    MessageOriginHiddenUser,
    MessageOriginUser,
)

from solomon import texts
from solomon.services.tasks import TaskService

logger = logging.getLogger(__name__)


def is_plain_text(message: Message) -> bool:
    """Текст, не похожий на команду: команды разбирают свои обработчики."""
    text = message.text
    return text is not None and not text.startswith("/")


def is_not_text(message: Message) -> bool:
    """Голос, фотография, стикер, пересланное без текста."""
    return message.text is None


def forwarded_sender(message: Message) -> str | None:
    """Чьё это сообщение, если его переслали.

    Имя нужно разбору: «пришлю смету завтра» от Ани — обещание мне, а не моё
    (`spec.md` §3.3). Отправитель, скрывший себя, приходит одним именем;
    канал и группа — названием. Не переслано — `None`, и в промпте этой
    строки нет.
    """
    origin = message.forward_origin
    if isinstance(origin, MessageOriginUser):
        return origin.sender_user.full_name
    if isinstance(origin, MessageOriginHiddenUser):
        return origin.sender_user_name
    if isinstance(origin, MessageOriginChat):
        return origin.sender_chat.title
    if isinstance(origin, MessageOriginChannel):
        return origin.chat.title
    return None


async def handle_start(message: Message) -> None:
    """Приветствие владельцу."""
    logger.info("Команда /start")
    await message.answer(texts.START)


async def handle_help(message: Message) -> None:
    """Короткий список того, что уже работает."""
    logger.info("Команда /help")
    await message.answer(texts.HELP)


async def handle_text(message: Message, tasks: TaskService | None) -> None:
    """Текст владельца — поручение: записываем и подтверждаем своими словами.

    Пересланное сообщение с текстом — такой же текст: разбирается как
    поручение, а имя отправителя уходит в разбор отдельным полем. Решение
    принимает слой операций, обработчик только отправляет его ответ.
    """
    if tasks is None:
        # Бота запустили без клиента базы — записывать некуда, и молчать о
        # этом нельзя (инвариант 4).
        logger.error("Поручение некуда записать: бот собран без базы")
        await message.answer(texts.NOT_SAVED)
        return

    outcome = await tasks.record_from_message(
        chat_id=message.chat.id,
        telegram_message_id=message.message_id,
        text=message.text or "",
        forwarded_from=forwarded_sender(message),
    )
    await message.answer(outcome.message)


async def handle_not_text(message: Message) -> None:
    """Не текст — вежливый отказ, и ничего не сохраняется."""
    logger.info("Сообщение не текстом: %s", message.content_type)
    await message.answer(texts.NOT_TEXT)


def build_router() -> Router:
    """Новый роутер обработчиков.

    Именно фабрика, а не общий объект модуля: роутер aiogram привязывается
    к одному диспетчеру навсегда, и второй сборке достался бы занятый.
    Порядок важен: команды разбираются раньше свободного текста.
    """
    router = Router(name="basic")
    router.message.register(handle_start, CommandStart())
    router.message.register(handle_help, Command("help"))
    router.message.register(handle_text, is_plain_text)
    router.message.register(handle_not_text, is_not_text)
    return router
