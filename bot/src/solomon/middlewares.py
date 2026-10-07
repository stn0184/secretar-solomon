"""Промежуточные слои обработки обновлений."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject, Update, User

from solomon import texts

logger = logging.getLogger(__name__)

# Бизнес-обновления личных чатов владельца (`techspec/25-chats.md` §25.2):
# их пишут собеседники, поэтому отправитель здесь не владелец. Их отбирает
# не этот фильтр, а подключение — в `services/chats.py` и в самой базе.
BUSINESS_UPDATES = frozenset(
    (
        "business_connection",
        "business_message",
        "edited_business_message",
        "deleted_business_messages",
    )
)


class OwnerOnlyMiddleware(BaseMiddleware):
    """Пропускает дальше только владельца.

    Инвариант 2 соблюдается с самой первой точки входа: обновление от чужого
    Telegram-id не доходит до обработчиков, а значит и до данных.

    Бизнес-обновления проходят мимо проверки отправителя и **без ответа**:
    «чужим сюда нельзя» в бизнес-чат владельца уйти не должно (§25.2), а
    владельца у них проверяет подключение.
    """

    def __init__(self, owner_telegram_id: int) -> None:
        self.owner_telegram_id = owner_telegram_id

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Update) and event.event_type in BUSINESS_UPDATES:
            return await handler(event, data)
        user: User | None = data.get("event_from_user")
        if user is not None and user.id == self.owner_telegram_id:
            return await handler(event, data)

        logger.info("Обновление не от владельца: %s", user.id if user else "без отправителя")
        message = event.message if isinstance(event, Update) else None
        if isinstance(message, Message):
            await message.answer(texts.STRANGER)
        return None
