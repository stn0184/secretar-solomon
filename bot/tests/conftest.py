"""Общая оснастка тестов: бот без сети.

Сессия подменена — запросы к Telegram не уходят, а записываются. Так
обработчики проверяются целиком через настоящий диспетчер aiogram.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import SendMessage, TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import Chat, Message, Update, User

from solomon.config import Settings

OWNER_ID = 777
STRANGER_ID = 999
# Игрушечный токен: сети в тестах нет, за S105/S106 здесь отвечает per-file-ignores.
TEST_TOKEN = "123456789:test-token"


class RecordingSession(BaseSession):
    """Вместо запроса к Telegram — запись в список."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[TelegramMethod[Any]] = []

    async def close(self) -> None:
        return None

    async def make_request(
        self,
        bot: Bot,
        method: TelegramMethod[TelegramType],
        timeout: int | None = None,
    ) -> TelegramType:
        self.sent.append(method)
        if isinstance(method, SendMessage):
            answer = Message(
                message_id=len(self.sent),
                date=datetime.now(UTC),
                chat=Chat(id=int(method.chat_id), type="private"),
                text=method.text,
            )
            return cast(TelegramType, answer)
        raise NotImplementedError(f"В тестах не ожидается метод {type(method).__name__}")

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        yield b""

    @property
    def texts(self) -> list[str]:
        """Тексты отправленных сообщений."""
        return [m.text for m in self.sent if isinstance(m, SendMessage)]


@pytest.fixture
def settings() -> Settings:
    return Settings(
        telegram_bot_token=TEST_TOKEN,
        owner_telegram_id=OWNER_ID,
        supabase_url="https://example.supabase.co",
        supabase_service_role_key="service-role-key",
    )


@pytest.fixture
def session() -> RecordingSession:
    return RecordingSession()


@pytest.fixture
async def bot(session: RecordingSession) -> AsyncGenerator[Bot, None]:
    instance = Bot(token=TEST_TOKEN, session=session)
    yield instance
    await instance.session.close()


def make_update(text: str, from_id: int = OWNER_ID, update_id: int = 1) -> Update:
    """Сообщение от человека — как его приносит long polling."""
    user = User(id=from_id, is_bot=False, first_name="Тим")
    message = Message(
        message_id=update_id,
        date=datetime.now(UTC),
        chat=Chat(id=from_id, type="private"),
        from_user=user,
        text=text,
    )
    return Update(update_id=update_id, message=message)
