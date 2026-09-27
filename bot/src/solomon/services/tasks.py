"""Приём поручения: сообщение в базу, разбор моделью, задача и ответ словами.

Порядок шагов — `techspec/03-schema.md` §3.4: сначала `record_message`
(поручение в базе с первой секунды, инвариант 5), потом модель, потом
`record_understanding` — разбор, ответ бота и задача одной транзакцией.
Повтор того же обновления отсекается на первом шаге: у сообщения уже есть
ответ, и модель не зовётся.

Голосовое идёт тем же путём с одним шагом посередине (`techspec/09-voice.md`
§9.3): сообщение с файлом пишется до того, как его расслышали, потом
скачивание и распознавание, дальше — как с текстом. Не расслышали — честный
ответ вместо задачи, и «Записал» не говорится (инвариант 4).

Обработчик ничего не решает: он зовёт `record_from_message` или
`record_from_voice` и отправляет то, что вернулось. Владелец берётся из
настроек, а не из сообщения — чужие обновления до этого слоя не доходят
(`middlewares.py`), и подставить чужой id из текста некому
(`techspec/04-access.md` §4.3).

Отказ базы разбирается в слова здесь, как в `db/health.py`: наружу выходит
причина для человека, а не трассировка.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from supabase import Client

from solomon import texts
from solomon.config import Settings
from solomon.db import tasks as db_tasks
from solomon.db.rpc import DatabaseError
from solomon.db.tasks import MessageKind, SavedMessage, SpeechKind, Task
from solomon.services.reminders import Planned, next_fire_at, plan
from solomon.services.transcription import (
    NotTranscribed,
    Transcriber,
    Transcript,
    TranscriptionResult,
)
from solomon.services.understanding import (
    TASK_KINDS,
    Analysis,
    Clock,
    SpeechQuality,
    Understanding,
    UnderstandingService,
    Verdict,
    fact_status,
)

logger = logging.getLogger(__name__)

# Пересказ в ответе. В базу текст уходит целиком (инвариант 5), а ответ
# бота остаётся коротким и в лимит Telegram укладывается всегда.
SUMMARY_LIMIT = 200


def summarize(text: str) -> str:
    """Пересказ поручения для ответа: длинное обрезается, целое уже в базе."""
    if len(text) <= SUMMARY_LIMIT:
        return text
    return text[:SUMMARY_LIMIT] + "…"


@dataclass(frozen=True, slots=True)
class RecordOutcome:
    """Чем кончился приём поручения и что сказать человеку."""

    ok: bool
    message: str


class MessageRecorder(Protocol):
    """Первый шаг: сообщение в базу до всякого разбора.

    У голоса и кружка — вид, файл и длительность, а текст пуст до расшифровки
    (§9.3).
    """

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
    ) -> SavedMessage: ...


class UnderstandingRecorder(Protocol):
    """Второй шаг: разбор, ответ бота и задача одной транзакцией.

    `transcript` — расшифровка голоса: тем же вызовом становится текстом
    сообщения (§9.3).
    """

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
    ) -> Task | None: ...


class Analyst(Protocol):
    """Разбор сообщения моделью — то, что подменяет тест вместо сети."""

    async def analyze(
        self,
        text: str,
        *,
        forwarded_from: str | None = None,
        spoken: SpeechQuality | None = None,
    ) -> Verdict: ...


# Скачивание звука из Telegram. Приходит из обработчика замыканием над
# `bot.download`, чтобы сервис не знал про aiogram — как отправка напоминаний
# приходит в `services/reminders.py`. Байты живут в памяти и на диск не
# пишутся (§9.2).
AudioLoader = Callable[[], Awaitable[bytes]]


def task_fields(understanding: Understanding) -> dict[str, Any]:
    """Поля задачи для `record_understanding` — по именам колонок §3.3."""
    return {
        "title": understanding.title,
        "kind": understanding.kind,
        "due_at": understanding.due_at.isoformat() if understanding.due_at else None,
        "due_precision": understanding.due_precision,
        "priority": understanding.priority,
        "promise": understanding.promise,
        "people": understanding.people,
        "needs_review": understanding.needs_review,
    }


def fact_rows(understanding: Understanding) -> list[dict[str, Any]]:
    """Записи памяти для `record_understanding` (§3.7): статус — по виду сообщения.

    Модель отдаёт только категорию и текст; факт это или предположение,
    решает бот по `kind` (`techspec/08-memory.md` §8.2), и решение
    проверяется кодом, а не моделью.
    """
    status = fact_status(understanding.kind)
    return [
        {"category": item.category, "text": item.text, "status": status}
        for item in understanding.facts
    ]


def literal_fields(text: str) -> dict[str, Any]:
    """Задача без разбора: текст как есть и пометка «человеку стоит взглянуть».

    Так поручение переживает отказ модели (`techspec/05-ai.md` §5.4): полей
    нет, но сама задача есть и не теряется.
    """
    return {
        "title": text,
        "kind": "task",
        "due_at": None,
        "due_precision": None,
        "priority": "normal",
        "promise": None,
        "people": [],
        "needs_review": True,
    }


class TaskService:
    """Операции над задачами. Собирается один раз при запуске бота."""

    def __init__(
        self,
        settings: Settings,
        record_message: MessageRecorder,
        record_understanding: UnderstandingRecorder,
        analyst: Analyst,
        transcriber: Transcriber,
        clock: Clock | None = None,
    ) -> None:
        self._settings = settings
        self._record_message = record_message
        self._record_understanding = record_understanding
        self._analyst = analyst
        self._transcriber = transcriber
        # «Сейчас» внедряется: от него зависит расписание напоминаний, и
        # тесты не должны угадывать, который час (`services/reminders.py`).
        self._clock = clock or self._now

    def _now(self) -> datetime:
        return datetime.now(self._settings.owner_timezone)

    @classmethod
    def with_database(
        cls, settings: Settings, db: Client, analyst: Analyst, transcriber: Transcriber
    ) -> TaskService:
        """Обычная сборка: пишет в настоящую базу."""

        async def record_message(
            *,
            owner_telegram_id: int,
            chat_id: int,
            telegram_message_id: int,
            text: str,
            kind: MessageKind = "text",
            telegram_file_id: str | None = None,
            duration_seconds: int | None = None,
        ) -> SavedMessage:
            return await db_tasks.record_message(
                db,
                owner_telegram_id=owner_telegram_id,
                chat_id=chat_id,
                telegram_message_id=telegram_message_id,
                text=text,
                kind=kind,
                telegram_file_id=telegram_file_id,
                duration_seconds=duration_seconds,
            )

        async def record_understanding(
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
        ) -> Task | None:
            return await db_tasks.record_understanding(
                db,
                message_id=message_id,
                owner_telegram_id=owner_telegram_id,
                analysis=analysis,
                ai_model=ai_model,
                ai_input_tokens=ai_input_tokens,
                ai_output_tokens=ai_output_tokens,
                reply=reply,
                task=task,
                reminders=reminders,
                facts=facts,
                transcript=transcript,
                transcript_confidence=transcript_confidence,
            )

        return cls(
            settings=settings,
            record_message=record_message,
            record_understanding=record_understanding,
            analyst=analyst,
            transcriber=transcriber,
        )

    @classmethod
    def with_understanding(
        cls,
        settings: Settings,
        db: Client,
        understanding: UnderstandingService,
        transcriber: Transcriber,
    ) -> TaskService:
        """Сборка бота целиком: настоящая база, настоящая модель, настоящее распознавание."""
        return cls.with_database(settings, db, understanding, transcriber)

    async def record_from_message(
        self,
        *,
        chat_id: int,
        telegram_message_id: int,
        text: str,
        forwarded_from: str | None = None,
    ) -> RecordOutcome:
        """Принять текстовое поручение и вернуть готовый ответ."""
        try:
            saved = await self._record_message(
                owner_telegram_id=self._settings.owner_telegram_id,
                chat_id=chat_id,
                telegram_message_id=telegram_message_id,
                text=text,
            )
        except DatabaseError as error:
            # Инвариант 4: не отвечаем «Записал», пока база не подтвердила.
            logger.warning("Сообщение не записано: %s", error)
            return RecordOutcome(ok=False, message=texts.NOT_SAVED)

        if saved.reply:
            return self._repeated(saved.id, saved.reply)
        return await self._understand(saved, text, forwarded_from=forwarded_from)

    async def record_from_voice(
        self,
        *,
        chat_id: int,
        telegram_message_id: int,
        kind: SpeechKind,
        file_id: str,
        duration: int,
        load_audio: AudioLoader,
        forwarded_from: str | None = None,
    ) -> RecordOutcome:
        """Принять голосовое или кружок: сохранить, расслышать, дальше как текст (§9.3).

        Файл не качается, пока база не подтвердила, что сообщение записано:
        иначе отвечать было бы не о чем (инвариант 4). Повтор обновления виден
        там же — ответ уже есть, и ни скачивания, ни распознавания не будет.
        """
        try:
            saved = await self._record_message(
                owner_telegram_id=self._settings.owner_telegram_id,
                chat_id=chat_id,
                telegram_message_id=telegram_message_id,
                text="",
                kind=kind,
                telegram_file_id=file_id,
                duration_seconds=duration,
            )
        except DatabaseError as error:
            logger.warning("Голосовое не записано: %s", error)
            return RecordOutcome(ok=False, message=texts.NOT_SAVED)

        if saved.reply:
            return self._repeated(saved.id, saved.reply)

        heard = await self._hear(load_audio)
        if isinstance(heard, NotTranscribed):
            return await self._not_heard(saved, heard)
        logger.info("Расслышано сообщение %s: %s с, знаков %s", saved.id, duration, len(heard.text))
        return await self._understand(
            saved, heard.text, forwarded_from=forwarded_from, transcript=heard
        )

    def _repeated(self, message_id: str, reply: str) -> RecordOutcome:
        """Повтор того же обновления: ответ уже давали, модель не зовём."""
        logger.info("Повтор сообщения %s: отвечаем сохранённым ответом", message_id)
        return RecordOutcome(ok=True, message=reply)

    async def _hear(self, load_audio: AudioLoader) -> TranscriptionResult:
        """Скачать файл и распознать. Отказ скачивания — тоже «не расслышал».

        Скачивание — граница с Telegram, и какие исключения оттуда придут,
        сервис не знает и знать не должен: любое из них — причина в журнал,
        человеку честный ответ, сообщение с `file_id` уже в базе.
        """
        try:
            audio = await load_audio()
        except Exception as error:  # noqa: BLE001 - граница Telegram, см. доккомментарий
            logger.warning("Файл не скачан из Telegram: %s: %s", type(error).__name__, error)
            return NotTranscribed(reason=f"download: {type(error).__name__}")
        return await self._transcriber.transcribe(audio)

    async def _not_heard(self, saved: SavedMessage, result: NotTranscribed) -> RecordOutcome:
        """Расшифровки нет: ответ в `reply`, задачи и разбора нет (§9.3)."""
        reply = texts.NOT_HEARD
        try:
            await self._record_understanding(
                message_id=saved.id,
                owner_telegram_id=self._settings.owner_telegram_id,
                analysis=None,
                ai_model=None,
                ai_input_tokens=None,
                ai_output_tokens=None,
                reply=reply,
                task=None,
                reminders=[],
                facts=[],
            )
        except DatabaseError as error:
            # Сообщение с файлом уже в базе, и «сохранил» — правда. Без
            # записанного ответа повтор обновления распознает заново; это не
            # потеря, а лишний запрос.
            logger.warning("Ответ «не расслышал» не записан: %s", error)
        logger.info("Не расслышал сообщение %s: %s", saved.id, result.reason)
        return RecordOutcome(ok=False, message=reply)

    async def _understand(
        self,
        saved: SavedMessage,
        text: str,
        *,
        forwarded_from: str | None,
        transcript: Transcript | None = None,
    ) -> RecordOutcome:
        """Разбор моделью и запись разбора — общий хвост текста и голоса.

        `transcript` есть у голоса: модели говорится, что текст распознан и
        с каким качеством (§9.4), а расшифровка уходит в базу тем же вызовом,
        что разбор и задача (§9.3).
        """
        spoken: SpeechQuality | None = None
        if transcript is not None:
            spoken = "low" if transcript.low_confidence else "fine"

        verdict = await self._analyst.analyze(text, forwarded_from=forwarded_from, spoken=spoken)
        now = self._clock()
        if isinstance(verdict, Analysis):
            understanding = verdict.understanding
            planned = plan(
                due_at=understanding.due_at,
                due_precision=understanding.due_precision,
                kind=understanding.kind,
                timezone=self._settings.owner_timezone,
                now=now,
            )
            reply = self._reply_for(understanding, planned, now)
            analysis: Mapping[str, Any] | None = understanding.model_dump(mode="json")
            task: Mapping[str, Any] | None = (
                task_fields(understanding) if understanding.kind in TASK_KINDS else None
            )
            facts = fact_rows(understanding)
            ai_model: str | None = verdict.model
            input_tokens: int | None = verdict.input_tokens
            output_tokens: int | None = verdict.output_tokens
        else:
            # Разбора не случилось: записываем буквально и говорим об этом.
            # Срока у такой задачи нет, значит и напоминать не о чем.
            planned = []
            reply = texts.RECORDED_AS_IS.format(text=summarize(text))
            analysis = None
            task = literal_fields(text)
            facts = []
            ai_model = None
            input_tokens = None
            output_tokens = None

        try:
            recorded = await self._record_understanding(
                message_id=saved.id,
                owner_telegram_id=self._settings.owner_telegram_id,
                analysis=analysis,
                ai_model=ai_model,
                ai_input_tokens=input_tokens,
                ai_output_tokens=output_tokens,
                reply=reply,
                task=task,
                reminders=[item.as_row() for item in planned],
                facts=facts,
                transcript=transcript.text if transcript is not None else None,
                transcript_confidence=transcript.confidence if transcript is not None else None,
            )
        except DatabaseError as error:
            logger.warning("Разбор не записан: %s", error)
            return RecordOutcome(ok=False, message=texts.NOT_SAVED)

        if facts:
            logger.info("Записано сведений о владельце: %s", len(facts))

        if recorded is not None:
            logger.info("Записана задача %s", recorded.id)
        else:
            logger.info("Задачи нет: сообщение %s сохранено с разбором", saved.id)
        return RecordOutcome(ok=True, message=reply)

    def _reply_for(
        self, understanding: Understanding, planned: list[Planned], now: datetime
    ) -> str:
        """Ответ человеку по видам. Дословно из модели — причина и текст записи.

        Строка «Напомню» берётся из того же плана, который уходит в базу
        (§6.4): бот обещает ровно то, что записал, — и ничего сверх того
        (инвариант 4). Сведение о себе подтверждается словами «Запомнил: …»
        (`techspec/08-memory.md` §8.2); предположения из поручения в ответ
        не попадают — они видны в приложении.
        """
        if understanding.kind == "about_me" and understanding.facts:
            return texts.remembered([item.text for item in understanding.facts])
        if understanding.kind not in TASK_KINDS:
            return texts.NO_ERRAND
        timezone = self._settings.owner_timezone
        due = None
        if understanding.due_at is not None:
            due = texts.format_due(
                understanding.due_at.astimezone(timezone), understanding.due_precision
            )
        nearest = next_fire_at(planned)
        remind_at = (
            None
            if nearest is None
            else texts.format_remind_at(nearest.astimezone(timezone), now.astimezone(timezone))
        )
        return texts.recorded_reply(
            kind=understanding.kind,
            title=understanding.title,
            due=due,
            review_reason=understanding.review_reason if understanding.needs_review else None,
            priority=understanding.priority,
            remind_at=remind_at,
        )
