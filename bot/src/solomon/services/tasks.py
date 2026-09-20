"""Приём поручения: сообщение в базу, разбор моделью, задача и ответ словами.

Порядок шагов — `techspec/03-schema.md` §3.4: сначала `record_message`
(поручение в базе с первой секунды, инвариант 5), потом модель, потом
`record_understanding` — разбор, ответ бота и задача одной транзакцией.
Повтор того же обновления отсекается на первом шаге: у сообщения уже есть
ответ, и модель не зовётся.

Обработчик ничего не решает: он зовёт `record_from_message` и отправляет то,
что вернулось. Владелец берётся из настроек, а не из сообщения — чужие
обновления до этого слоя не доходят (`middlewares.py`), и подставить чужой
id из текста некому (`techspec/04-access.md` §4.3).

Отказ базы разбирается в слова здесь, как в `db/health.py`: наружу выходит
причина для человека, а не трассировка.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from supabase import Client

from solomon import texts
from solomon.config import Settings
from solomon.db import tasks as db_tasks
from solomon.db.tasks import DatabaseError, SavedMessage, Task
from solomon.services.understanding import (
    TASK_KINDS,
    Analysis,
    Understanding,
    UnderstandingService,
    Verdict,
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
    """Первый шаг: сообщение в базу до всякого разбора."""

    async def __call__(
        self, *, owner_telegram_id: int, chat_id: int, telegram_message_id: int, text: str
    ) -> SavedMessage: ...


class UnderstandingRecorder(Protocol):
    """Второй шаг: разбор, ответ бота и задача одной транзакцией."""

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
    ) -> Task | None: ...


class Analyst(Protocol):
    """Разбор сообщения моделью — то, что подменяет тест вместо сети."""

    async def analyze(self, text: str, *, forwarded_from: str | None = None) -> Verdict: ...


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
    ) -> None:
        self._settings = settings
        self._record_message = record_message
        self._record_understanding = record_understanding
        self._analyst = analyst

    @classmethod
    def with_database(cls, settings: Settings, db: Client, analyst: Analyst) -> TaskService:
        """Обычная сборка: пишет в настоящую базу."""

        async def record_message(
            *, owner_telegram_id: int, chat_id: int, telegram_message_id: int, text: str
        ) -> SavedMessage:
            return await db_tasks.record_message(
                db,
                owner_telegram_id=owner_telegram_id,
                chat_id=chat_id,
                telegram_message_id=telegram_message_id,
                text=text,
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
            )

        return cls(
            settings=settings,
            record_message=record_message,
            record_understanding=record_understanding,
            analyst=analyst,
        )

    @classmethod
    def with_understanding(
        cls, settings: Settings, db: Client, understanding: UnderstandingService
    ) -> TaskService:
        """Сборка бота целиком: настоящая база и настоящая модель."""
        return cls.with_database(settings, db, understanding)

    async def record_from_message(
        self,
        *,
        chat_id: int,
        telegram_message_id: int,
        text: str,
        forwarded_from: str | None = None,
    ) -> RecordOutcome:
        """Принять поручение и вернуть готовый ответ."""
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
            # Повтор того же обновления: ответ уже давали, модель не зовём.
            logger.info("Повтор сообщения %s: отвечаем сохранённым ответом", saved.id)
            return RecordOutcome(ok=True, message=saved.reply)

        verdict = await self._analyst.analyze(text, forwarded_from=forwarded_from)
        if isinstance(verdict, Analysis):
            understanding = verdict.understanding
            reply = self._reply_for(understanding)
            analysis: Mapping[str, Any] | None = understanding.model_dump(mode="json")
            task: Mapping[str, Any] | None = (
                task_fields(understanding) if understanding.kind in TASK_KINDS else None
            )
            ai_model: str | None = verdict.model
            input_tokens: int | None = verdict.input_tokens
            output_tokens: int | None = verdict.output_tokens
        else:
            # Разбора не случилось: записываем буквально и говорим об этом.
            reply = texts.RECORDED_AS_IS.format(text=summarize(text))
            analysis = None
            task = literal_fields(text)
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
            )
        except DatabaseError as error:
            logger.warning("Разбор не записан: %s", error)
            return RecordOutcome(ok=False, message=texts.NOT_SAVED)

        if recorded is not None:
            logger.info("Записана задача %s", recorded.id)
        else:
            logger.info("Задачи нет: сообщение %s сохранено с разбором", saved.id)
        return RecordOutcome(ok=True, message=reply)

    def _reply_for(self, understanding: Understanding) -> str:
        """Ответ человеку по видам. Дословно из модели — только причина."""
        if understanding.kind not in TASK_KINDS:
            return texts.NO_ERRAND
        due = None
        if understanding.due_at is not None:
            local = understanding.due_at.astimezone(self._settings.owner_timezone)
            due = texts.format_due(local, understanding.due_precision)
        return texts.recorded_reply(
            kind=understanding.kind,
            title=understanding.title,
            due=due,
            review_reason=understanding.review_reason if understanding.needs_review else None,
        )
