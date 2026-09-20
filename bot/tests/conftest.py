"""Общая оснастка тестов: бот без сети.

Сессия подменена — запросы к Telegram не уходят, а записываются. Так
обработчики проверяются целиком через настоящий диспетчер aiogram.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import SendMessage, TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import Chat, Message, Update, User, Voice

from solomon.config import Settings
from solomon.db.tasks import DatabaseError, Task

OWNER_ID = 777
STRANGER_ID = 999
# Пояс владельца в тестах: +05:00 круглый год, без перехода на летнее время —
# ожидаемые даты считаются глазами и не зависят от месяца.
OWNER_TIMEZONE = "Asia/Yekaterinburg"
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


class FakeRecorder:
    """Вместо базы — список того, что в неё просили записать."""

    def __init__(self, task: Task | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self.task = task or Task(id="0e2f", title="купить лампочку", status="active")

    async def __call__(
        self, *, owner_telegram_id: int, chat_id: int, telegram_message_id: int, text: str
    ) -> Task:
        self.calls.append(
            {
                "owner_telegram_id": owner_telegram_id,
                "chat_id": chat_id,
                "telegram_message_id": telegram_message_id,
                "text": text,
            }
        )
        return self.task


class BrokenRecorder:
    """База не ответила."""

    async def __call__(
        self, *, owner_telegram_id: int, chat_id: int, telegram_message_id: int, text: str
    ) -> Task:
        raise DatabaseError("ConnectTimeout: timed out")


def make_settings() -> Settings:
    """Настройки для тестов: один набор на все файлы, а не копия в каждом."""
    return Settings(
        telegram_bot_token=TEST_TOKEN,
        owner_telegram_id=OWNER_ID,
        owner_timezone=ZoneInfo(OWNER_TIMEZONE),
        supabase_url="https://example.supabase.co",
        supabase_service_role_key="service-role-key",
        anthropic_api_key="sk-ant-test",
    )


@pytest.fixture
def settings() -> Settings:
    return make_settings()


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


def make_voice_update(from_id: int = OWNER_ID, update_id: int = 1) -> Update:
    """Голосовое сообщение: текста нет, сохранять нечего."""
    user = User(id=from_id, is_bot=False, first_name="Тим")
    message = Message(
        message_id=update_id,
        date=datetime.now(UTC),
        chat=Chat(id=from_id, type="private"),
        from_user=user,
        voice=Voice(file_id="voice-1", file_unique_id="voice-1", duration=3),
    )
    return Update(update_id=update_id, message=message)
