"""Промежуточные слои обработки обновлений."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject, Update, User

from solomon import texts

logger = logging.getLogger(__name__)


class OwnerOnlyMiddleware(BaseMiddleware):
    """Пропускает дальше только владельца.

    Инвариант 2 соблюдается с самой первой точки входа: обновление от чужого
    Telegram-id не доходит до обработчиков, а значит и до данных.
    """

    def __init__(self, owner_telegram_id: int) -> None:
        self.owner_telegram_id = owner_telegram_id

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        if user is not None and user.id == self.owner_telegram_id:
            return await handler(event, data)

        logger.info("Обновление не от владельца: %s", user.id if user else "без отправителя")
        message = event.message if isinstance(event, Update) else None
        if isinstance(message, Message):
            await message.answer(texts.STRANGER)
        return None
