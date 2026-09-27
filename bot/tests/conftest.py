"""Общая оснастка тестов: бот без сети.

Сессия подменена — запросы к Telegram не уходят, а записываются. Так
обработчики проверяются целиком через настоящий диспетчер aiogram.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.enums import MessageOriginType
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage, TelegramMethod
from aiogram.methods.base import TelegramType
from aiogram.types import (
    CallbackQuery,
    Chat,
    Message,
    MessageOriginUser,
    Update,
    User,
    Voice,
)

from solomon.config import Settings
from solomon.db.rpc import DatabaseError
from solomon.db.tasks import SavedMessage, Task
from solomon.services.understanding import Analysis, Understanding, Verdict

OWNER_ID = 777
STRANGER_ID = 999
# Пояс владельца в тестах: +05:00 круглый год, без перехода на летнее время —
# ожидаемые даты считаются глазами и не зависят от месяца.
OWNER_TIMEZONE = "Asia/Yekaterinburg"
# Игрушечный токен: сети в тестах нет, за S105/S106 здесь отвечает per-file-ignores.
TEST_TOKEN = "123456789:test-token"

_DEFAULT_TASK = Task(id="0e2f", title="купить лампочку", status="active")


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
        if isinstance(method, EditMessageText):
            edited = Message(
                message_id=int(method.message_id or 0),
                date=datetime.now(UTC),
                chat=Chat(id=int(method.chat_id or 0), type="private"),
                text=method.text,
            )
            return cast(TelegramType, edited)
        if isinstance(method, AnswerCallbackQuery):
            return cast(TelegramType, True)
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

    @property
    def edits(self) -> list[EditMessageText]:
        """Правки уже отправленных сообщений — например, отметка «Сделано»."""
        return [m for m in self.sent if isinstance(m, EditMessageText)]

    @property
    def answers(self) -> list[str | None]:
        """Ответы на нажатия кнопок: всплывающая подсказка в Telegram."""
        return [m.text for m in self.sent if isinstance(m, AnswerCallbackQuery)]


class FakeMessages:
    """Первый шаг приёма: вместо базы — список того, что в неё просили записать."""

    def __init__(self, message: SavedMessage | None = None, broken: bool = False) -> None:
        self.calls: list[dict[str, object]] = []
        self.message = message or SavedMessage(id="9a71", reply=None)
        self.broken = broken

    async def __call__(
        self, *, owner_telegram_id: int, chat_id: int, telegram_message_id: int, text: str
    ) -> SavedMessage:
        if self.broken:
            raise DatabaseError("ConnectTimeout: timed out")
        self.calls.append(
            {
                "owner_telegram_id": owner_telegram_id,
                "chat_id": chat_id,
                "telegram_message_id": telegram_message_id,
                "text": text,
            }
        )
        return self.message


class FakeUnderstandings:
    """Второй шаг приёма: разбор, ответ бота и задача одной транзакцией."""

    def __init__(self, task: Task | None = _DEFAULT_TASK, broken: bool = False) -> None:
        self.calls: list[dict[str, object]] = []
        self.task = task
        self.broken = broken

    async def __call__(
        self,
        *,
        message_id: str,
        owner_telegram_id: int,
        analysis: Mapping[str, Any] | None,
        ai_model: str | None,
        ai_input_tokens: int | None,
        ai_output_tokens: int | None,
        reply: str,
        task: Mapping[str, Any] | None,
        reminders: Sequence[Mapping[str, Any]],
        facts: Sequence[Mapping[str, Any]],
    ) -> Task | None:
        if self.broken:
            raise DatabaseError("ConnectTimeout: timed out")
        self.calls.append(
            {
                "message_id": message_id,
                "owner_telegram_id": owner_telegram_id,
                "analysis": analysis,
                "ai_model": ai_model,
                "ai_input_tokens": ai_input_tokens,
                "ai_output_tokens": ai_output_tokens,
                "reply": reply,
                "task": task,
                "reminders": list(reminders),
                "facts": list(facts),
            }
        )
        return self.task


def make_understanding(**fields: Any) -> Understanding:
    """Ответ модели по схеме §5.3 — меняется только то, что важно тесту."""
    base: dict[str, Any] = {
        "kind": "task",
        "title": "купить лампочку",
        "due_at": None,
        "due_precision": None,
        "priority": "normal",
        "promise": None,
        "people": [],
        "needs_review": False,
        "review_reason": None,
        "reply_hint": None,
        "facts": [],
    }
    return Understanding.model_validate({**base, **fields})


class FakeAnalyst:
    """Вместо Claude — заранее решённый вердикт и список того, что спросили."""

    def __init__(self, verdict: Understanding | Verdict) -> None:
        self.verdict: Verdict = (
            Analysis(
                understanding=verdict,
                model="claude-opus-5",
                input_tokens=120,
                output_tokens=45,
            )
            if isinstance(verdict, Understanding)
            else verdict
        )
        self.calls: list[tuple[str, str | None]] = []

    async def analyze(self, text: str, *, forwarded_from: str | None = None) -> Verdict:
        self.calls.append((text, forwarded_from))
        return self.verdict


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


def make_forwarded_update(
    text: str, sender: str = "Аня", from_id: int = OWNER_ID, update_id: int = 1
) -> Update:
    """Пересланное владельцу сообщение с текстом: автор оригинала назван."""
    user = User(id=from_id, is_bot=False, first_name="Тим")
    origin = MessageOriginUser(
        type=MessageOriginType.USER,
        date=datetime.now(UTC),
        sender_user=User(id=555, is_bot=False, first_name=sender),
    )
    message = Message(
        message_id=update_id,
        date=datetime.now(UTC),
        chat=Chat(id=from_id, type="private"),
        from_user=user,
        text=text,
        forward_origin=origin,
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


def make_callback_update(
    data: str,
    text: str = "Напоминаю: отправить расчёт",
    from_id: int = OWNER_ID,
    update_id: int = 1,
) -> Update:
    """Нажатие кнопки под напоминанием — как его приносит long polling."""
    user = User(id=from_id, is_bot=False, first_name="Тим")
    message = Message(
        message_id=update_id,
        date=datetime.now(UTC),
        chat=Chat(id=from_id, type="private"),
        from_user=User(id=1, is_bot=True, first_name="Соломон"),
        text=text,
    )
    callback = CallbackQuery(
        id=f"callback-{update_id}",
        from_user=user,
        chat_instance="chat-instance",
        message=message,
        data=data,
    )
    return Update(update_id=update_id, callback_query=callback)
