"""Общая оснастка тестов: бот без сети.

Сессия подменена — запросы к Telegram не уходят, а записываются. Так
обработчики проверяются целиком через настоящий диспетчер aiogram.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable, Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.enums import MessageOriginType
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageText,
    GetFile,
    SendChatAction,
    SendMessage,
    TelegramMethod,
)
from aiogram.methods.base import TelegramType
from aiogram.types import (
    CallbackQuery,
    Chat,
    File,
    Message,
    MessageOriginUser,
    PhotoSize,
    Update,
    User,
    VideoNote,
    Voice,
)

from solomon.config import Settings
from solomon.db.reminders import Planned
from solomon.db.rpc import DatabaseError
from solomon.db.tasks import (
    MessageKind,
    OpenQuestion,
    PickedMessage,
    SavedMessage,
    SpeechKind,
    StoredMessage,
    Task,
    TaskDetails,
    TaskEvent,
)
from solomon.services.transcription import Transcript, TranscriptionResult
from solomon.services.understanding import (
    Analysis,
    AskedQuestion,
    OpenTask,
    SpeechQuality,
    Understanding,
    Verdict,
)

OWNER_ID = 777
STRANGER_ID = 999
# Пояс владельца в тестах: +05:00 круглый год, без перехода на летнее время —
# ожидаемые даты считаются глазами и не зависят от месяца.
OWNER_TIMEZONE = "Asia/Yekaterinburg"
# Игрушечный токен: сети в тестах нет, за S105/S106 здесь отвечает per-file-ignores.
TEST_TOKEN = "123456789:test-token"

_DEFAULT_TASK = Task(id="0e2f", title="купить лампочку", status="active")
# Что «слышит» подменённый транскрайбер, если тест не сказал иного.
SPOKEN = "в пятницу отправить расчёт клиенту"
AUDIO = b"OggS\x00fake-opus"


class RecordingSession(BaseSession):
    """Вместо запроса к Telegram — запись в список."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[TelegramMethod[Any]] = []
        # Что «лежит» в Telegram под любым file_id: скачивание отдаёт эти байты.
        self.file_bytes = AUDIO

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
        if isinstance(method, SendChatAction):
            return cast(TelegramType, True)
        if isinstance(method, GetFile):
            # Первый шаг `bot.download`: Telegram называет путь, по которому
            # потом качается содержимое (`stream_content`).
            found = File(
                file_id=method.file_id,
                file_unique_id=method.file_id,
                file_path=f"voice/{method.file_id}.oga",
            )
            return cast(TelegramType, found)
        raise NotImplementedError(f"В тестах не ожидается метод {type(method).__name__}")

    async def stream_content(
        self,
        url: str,
        headers: dict[str, Any] | None = None,
        timeout: int = 30,
        chunk_size: int = 65536,
        raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        yield self.file_bytes

    @property
    def texts(self) -> list[str]:
        """Тексты отправленных сообщений."""
        return [m.text for m in self.sent if isinstance(m, SendMessage)]

    @property
    def actions(self) -> list[str]:
        """Статусы чата («печатает…»), которые бот показывал по дороге."""
        return [m.action for m in self.sent if isinstance(m, SendChatAction)]

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
        self,
        *,
        owner_telegram_id: int,
        chat_id: int,
        telegram_message_id: int,
        text: str,
        kind: MessageKind = "text",
        telegram_file_id: str | None = None,
        duration_seconds: int | None = None,
    ) -> SavedMessage:
        if self.broken:
            raise DatabaseError("ConnectTimeout: timed out")
        self.calls.append(
            {
                "owner_telegram_id": owner_telegram_id,
                "chat_id": chat_id,
                "telegram_message_id": telegram_message_id,
                "text": text,
                "kind": kind,
                "telegram_file_id": telegram_file_id,
                "duration_seconds": duration_seconds,
            }
        )
        return self.message


class FakeUnderstandings:
    """Второй шаг приёма: разбор, ответ бота и задача одной транзакцией.

    С `questions` фейк ведёт открытый вопрос так же, как `record_understanding`
    (`techspec/03-schema.md` §3.4): повтор по сообщению, у которого задача уже
    есть, вопроса не трогает; «не расслышал» — ни разбора, ни задачи, ни
    поправки — тоже; любая другая запись вопрос снимает, а задача с
    `open_question` ставит новый — со временем от `clock`. Правка (`edit`)
    вопрос тоже снимает; вопрос по правке фейк не ставит — ответ на него
    тест начинает с готового вопроса.
    """

    def __init__(
        self,
        task: Task | None = _DEFAULT_TASK,
        broken: bool = False,
        questions: FakeQuestions | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.calls: list[dict[str, object]] = []
        self.task = task
        self.broken = broken
        self.questions = questions
        self._clock = clock or (lambda: datetime.now(UTC))
        # Сообщения, по которым задача уже заведена: повтор её вернёт как есть.
        self._with_task: set[str] = set()

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
        transcript: str | None = None,
        transcript_confidence: float | None = None,
        amend: Mapping[str, Any] | None = None,
        edit: Mapping[str, Any] | None = None,
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
                "transcript": transcript,
                "transcript_confidence": transcript_confidence,
                "amend": amend,
                "edit": edit,
            }
        )
        if message_id in self._with_task:
            return self.task
        if self.questions is not None:
            self._follow_question(self.questions, analysis, task, amend or edit)
        if task is not None and amend is None:
            self._with_task.add(message_id)
        return self.task

    def _follow_question(
        self,
        questions: FakeQuestions,
        analysis: Mapping[str, Any] | None,
        task: Mapping[str, Any] | None,
        amend: Mapping[str, Any] | None,
    ) -> None:
        """Снять и поставить открытый вопрос — по тем же правилам, что база."""
        if analysis is None and task is None and amend is None:
            return
        questions.asked = None
        if task is None or amend is not None:
            return
        question = str(task.get("open_question") or "").strip()
        if not question:
            return
        due_at = task.get("due_at")
        questions.asked = OpenQuestion(
            task_id=self.task.id if self.task is not None else "new-task",
            question=question,
            title=str(task["title"]),
            kind=str(task.get("kind") or "task"),
            due_at=datetime.fromisoformat(due_at) if isinstance(due_at, str) else None,
            due_precision=task.get("due_precision"),
            priority=str(task.get("priority") or "normal"),
            promise=task.get("promise"),
            people=tuple(task.get("people") or ()),
            asked_at=self._clock(),
        )


class FakeQuestions:
    """Открытый вопрос владельца: отдаётся, только если задан не раньше `since`.

    Так тест видит и границу суток, которую считает сервис, и то, что старый
    вопрос до модели не доходит (`techspec/10-dialog.md` §10.3).
    """

    def __init__(self, asked: OpenQuestion | None = None, broken: bool = False) -> None:
        self.asked = asked
        self.broken = broken
        self.calls: list[tuple[int, datetime]] = []

    async def __call__(self, *, owner_telegram_id: int, since: datetime) -> OpenQuestion | None:
        self.calls.append((owner_telegram_id, since))
        if self.broken:
            raise DatabaseError("ConnectTimeout: timed out")
        if self.asked is None or self.asked.asked_at < since:
            return None
        return self.asked


class FakePlanner:
    """Вместо `reminder_plan` в базе — заранее решённый план и список вызовов.

    Само правило §6.1 здесь не повторяется: оно живёт в базе и проверяется
    тестами PGlite (`supabase/tests/reminder_plan.test.ts`). Тест бота
    смотрит, с чем сервис спросил план и что сделал с ответом.
    """

    def __init__(self, planned: Sequence[Planned] = (), broken: bool = False) -> None:
        self.planned = list(planned)
        self.broken = broken
        self.calls: list[dict[str, object]] = []

    async def __call__(
        self,
        *,
        due_at: datetime | None,
        due_precision: str | None,
        kind: str,
        now: datetime,
    ) -> list[Planned]:
        self.calls.append(
            {"due_at": due_at, "due_precision": due_precision, "kind": kind, "now": now}
        )
        if self.broken:
            raise DatabaseError("ConnectTimeout: timed out")
        return list(self.planned)


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
        "question": None,
        "answers_question": False,
        "edit": None,
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
        self.calls: list[tuple[str, str | None, SpeechQuality | None]] = []
        # Открытый вопрос, с которым звали модель (§10.2), — по вызову.
        self.questions: list[AskedQuestion | None] = []
        # Подсказки правки словом (§12.2) — по вызову: список задач (`None` —
        # блока 5 нет), номер последней задачи и строка свайпа.
        self.tasks: list[list[OpenTask] | None] = []
        self.last_tasks: list[int | None] = []
        self.swipes: list[str | None] = []

    async def analyze(
        self,
        text: str,
        *,
        forwarded_from: str | None = None,
        spoken: SpeechQuality | None = None,
        open_question: AskedQuestion | None = None,
        tasks: Sequence[OpenTask] | None = None,
        last_task: int | None = None,
        swipe: str | None = None,
    ) -> Verdict:
        self.calls.append((text, forwarded_from, spoken))
        self.questions.append(open_question)
        self.tasks.append(None if tasks is None else list(tasks))
        self.last_tasks.append(last_task)
        self.swipes.append(swipe)
        return self.verdict


def make_details(**fields: Any) -> TaskDetails:
    """Задача со всеми полями правки: активная, без срока, людей и срочности."""
    base: dict[str, Any] = {
        "id": "5b0c7a52-8f3e-4c1d-9a6b-2e4f1d3c8b90",
        "title": "купить лампочку",
        "kind": "task",
        "status": "active",
        "due_at": None,
        "due_precision": None,
        "priority": "normal",
        "promise": None,
        "people": (),
        "created_at": datetime(2026, 9, 15, 10, 0, tzinfo=ZoneInfo(OWNER_TIMEZONE)),
    }
    return TaskDetails(**{**base, **fields})


class FakeEdits:
    """Хранилище правки словом (`EditStore` в `services/tasks.py`) без базы.

    Задачи — в любом статусе; список для промпта — только активные, в том
    порядке, в каком их дал тест: нумерует сервис. `pick` и `reopen` ведут
    себя как `pick_task` и `reopen_task` (`techspec/03-schema.md` §3.4):
    второй выбор по тому же сообщению не пишется, неактивная задача не
    правится, возврат активной — без записи. `broken` — имена методов,
    которые отвечают отказом базы.
    """

    def __init__(
        self,
        tasks: Sequence[TaskDetails] = (),
        *,
        message_event: TaskEvent | None = None,
        reminder_event: TaskEvent | None = None,
        reminders: Mapping[int, str] | None = None,
        messages: Mapping[int, StoredMessage] | None = None,
        broken: Iterable[str] = (),
    ) -> None:
        self.tasks = {task.id: task for task in tasks}
        self.message_event = message_event
        self.reminder_event = reminder_event
        self.reminders = dict(reminders or {})
        self.messages = dict(messages or {})
        self.broken = set(broken)
        self.calls: list[tuple[Any, ...]] = []
        # Что записано: выбор кнопкой (сообщение, правка, ответ) и возврат.
        self.picks: list[tuple[str, dict[str, Any], str]] = []
        self.reopens: list[tuple[str, list[Planned]]] = []

    def _touch(self, name: str, *args: Any) -> None:
        self.calls.append((name, *args))
        if name in self.broken:
            raise DatabaseError("ConnectTimeout: timed out")

    async def open_tasks(self, limit: int) -> list[TaskDetails]:
        self._touch("open_tasks", limit)
        return [task for task in self.tasks.values() if task.status == "active"][:limit]

    async def last_message_event(self, since: datetime) -> TaskEvent | None:
        self._touch("last_message_event", since)
        event = self.message_event
        return event if event is not None and event.at >= since else None

    async def last_reminder_event(self, since: datetime) -> TaskEvent | None:
        self._touch("last_reminder_event", since)
        event = self.reminder_event
        return event if event is not None and event.at >= since else None

    async def reminder_task(self, telegram_message_id: int) -> str | None:
        self._touch("reminder_task", telegram_message_id)
        return self.reminders.get(telegram_message_id)

    async def message(self, chat_id: int, telegram_message_id: int) -> StoredMessage | None:
        self._touch("message", chat_id, telegram_message_id)
        return self.messages.get(telegram_message_id)

    async def task(self, task_id: str) -> TaskDetails | None:
        self._touch("task", task_id)
        return self.tasks.get(task_id)

    async def pick(self, message_id: str, edit: Mapping[str, Any], reply: str) -> PickedMessage:
        self._touch("pick", message_id)
        key, stored = next(
            (key, stored) for key, stored in self.messages.items() if stored.id == message_id
        )
        if stored.task_id is not None:
            return PickedMessage(id=stored.id, task_id=stored.task_id, reply=stored.reply)
        task = self.tasks.get(str(edit["task_id"]))
        if task is None or task.status != "active":
            return PickedMessage(id=stored.id, task_id=None, reply=stored.reply)
        self.picks.append((message_id, dict(edit), reply))
        status = {"done": "done", "cancel": "cancelled"}.get(str(edit["action"]), task.status)
        self.tasks[task.id] = replace(task, status=status)
        self.messages[key] = replace(stored, task_id=task.id, reply=reply)
        return PickedMessage(id=stored.id, task_id=task.id, reply=reply)

    async def reopen(self, task_id: str, schedule: Sequence[Planned]) -> TaskDetails | None:
        self._touch("reopen", task_id)
        task = self.tasks.get(task_id)
        if task is None:
            return None
        if task.status == "active":
            return task
        self.reopens.append((task_id, list(schedule)))
        reopened = replace(task, status="active")
        self.tasks[task_id] = reopened
        return reopened


class FakeTranscriber:
    """Вместо Deepgram — заранее решённый результат и список того, что прислали."""

    def __init__(self, result: TranscriptionResult | None = None) -> None:
        self.result: TranscriptionResult = result or Transcript(text=SPOKEN, confidence=0.93)
        self.calls: list[bytes] = []

    async def transcribe(self, audio: bytes) -> TranscriptionResult:
        self.calls.append(audio)
        # Настоящее распознавание ждёт сети. Без настоящей паузы фоновый статус
        # «печатает…» не успел бы ни разу отправиться: до первой отправки его
        # задаче нужно несколько ходов цикла событий. Пауза короче ~16 мс на
        # Windows попадает под разрешение часов цикла и ведёт себя как `sleep(0)`.
        await asyncio.sleep(0.05)
        return self.result


async def load_audio() -> bytes:
    """Скачивание из Telegram без сети: те же байты каждый раз."""
    return AUDIO


def make_settings() -> Settings:
    """Настройки для тестов: один набор на все файлы, а не копия в каждом."""
    return Settings(
        telegram_bot_token=TEST_TOKEN,
        owner_telegram_id=OWNER_ID,
        owner_timezone=ZoneInfo(OWNER_TIMEZONE),
        supabase_url="https://example.supabase.co",
        supabase_service_role_key="service-role-key",
        anthropic_api_key="sk-ant-test",
        deepgram_api_key="dg-test",
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


def make_voice_update(
    from_id: int = OWNER_ID,
    update_id: int = 1,
    kind: SpeechKind = "voice",
    duration: int = 32,
    sender: str | None = None,
) -> Update:
    """Голосовое или видео-кружок: текста нет, есть файл и длительность.

    `sender` — сообщение переслано владельцу от этого человека.
    """
    user = User(id=from_id, is_bot=False, first_name="Тим")
    origin = (
        MessageOriginUser(
            type=MessageOriginType.USER,
            date=datetime.now(UTC),
            sender_user=User(id=555, is_bot=False, first_name=sender),
        )
        if sender
        else None
    )
    message = Message(
        message_id=update_id,
        date=datetime.now(UTC),
        chat=Chat(id=from_id, type="private"),
        from_user=user,
        forward_origin=origin,
        voice=(
            Voice(file_id="voice-1", file_unique_id="voice-1", duration=duration)
            if kind == "voice"
            else None
        ),
        video_note=(
            VideoNote(file_id="note-1", file_unique_id="note-1", length=240, duration=duration)
            if kind == "video_note"
            else None
        ),
    )
    return Update(update_id=update_id, message=message)


def make_photo_update(from_id: int = OWNER_ID, update_id: int = 1) -> Update:
    """Фотография: ни текста, ни речи — бот такое пока не понимает."""
    user = User(id=from_id, is_bot=False, first_name="Тим")
    message = Message(
        message_id=update_id,
        date=datetime.now(UTC),
        chat=Chat(id=from_id, type="private"),
        from_user=user,
        photo=[PhotoSize(file_id="photo-1", file_unique_id="photo-1", width=90, height=90)],
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
