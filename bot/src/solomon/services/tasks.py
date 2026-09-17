"""Приём поручения: сообщение и задача в базу, ответ — словами.

Обработчик ничего не решает: он зовёт `record_from_message` и отправляет то,
что вернулось. Владелец берётся из настроек, а не из сообщения — чужие
обновления до этого слоя не доходят (`middlewares.py`), и подставить чужой
id из текста некому (`techspec/04-access.md` §4.3).

Отказ базы разбирается в слова здесь, как в `db/health.py`: наружу выходит
причина для человека, а не трассировка.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from supabase import Client

from solomon import texts
from solomon.config import Settings
from solomon.db import tasks as db_tasks
from solomon.db.tasks import DatabaseError, Task

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


class Recorder(Protocol):
    """Запись поручения в базу — ровно то, что подменяет тест."""

    async def __call__(
        self, *, owner_telegram_id: int, chat_id: int, telegram_message_id: int, text: str
    ) -> Task: ...


class TaskService:
    """Операции над задачами. Собирается один раз при запуске бота."""

    def __init__(self, settings: Settings, recorder: Recorder) -> None:
        self._settings = settings
        self._record = recorder

    @classmethod
    def with_database(cls, settings: Settings, db: Client) -> TaskService:
        """Обычная сборка: пишет в настоящую базу."""

        async def record(
            *, owner_telegram_id: int, chat_id: int, telegram_message_id: int, text: str
        ) -> Task:
            return await db_tasks.record_task(
                db,
                owner_telegram_id=owner_telegram_id,
                chat_id=chat_id,
                telegram_message_id=telegram_message_id,
                text=text,
            )

        return cls(settings=settings, recorder=record)

    async def record_from_message(
        self, *, chat_id: int, telegram_message_id: int, text: str
    ) -> RecordOutcome:
        """Записать поручение и вернуть готовый ответ.

        Пока без разбора смысла: задача — это буквально текст сообщения
        (`spec.md` §3.2 — следующий этап). Сообщение и задача пишутся одной
        транзакцией, повтор того же обновления второй задачи не заводит.
        """
        try:
            task = await self._record(
                owner_telegram_id=self._settings.owner_telegram_id,
                chat_id=chat_id,
                telegram_message_id=telegram_message_id,
                text=text,
            )
        except DatabaseError as error:
            # Инвариант 4: не отвечаем «Записал», пока база не подтвердила.
            logger.warning("Поручение не записано: %s", error)
            return RecordOutcome(ok=False, message=texts.NOT_SAVED)

        logger.info("Записана задача %s", task.id)
        return RecordOutcome(ok=True, message=texts.RECORDED.format(text=summarize(text)))
