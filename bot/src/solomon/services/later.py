"""Разобрать позже: очередь сообщений, которые модель не смогла разобрать сразу.

Источник правды — `techspec/33-retry-later.md`. Приём сообщения
(`services/tasks.py`) при временном отказе модели — сеть, 429, 5xx — пишет
не «как есть», а «⏳ Принял…» и откладывает сообщение в базе (§33.2, шаг 1).
Дальше всё здесь:

- тик (§6.2) запускает разбор очереди в фоне и его не ждёт;
- голова очереди — самое раннее ждущее сообщение — берётся атомарно
  (`take_deferred`); взятая больше 10 минут назад брошена и берётся снова;
- разбор — по одному, по порядку получения: голова снова не разобралась —
  она возвращается в очередь с растущей паузой, и остальные ждут за ней;
- удалось — ответ на исходное «Разобрал ваше сообщение от ЧЧ:ММ: …», шесть
  часов без успеха — «Записал как есть: … Разобрать так и не смог.»;
- порядок «записать → отправить → пометить», как у поисков (§24.3): ответ
  записан и не ушёл — следующий заход шлёт записанный, а не разбирает заново.

Сеть здесь не трогается: база — через `LaterStore`, разбор — через
`TaskService.retry`, Telegram — через `LaterReplier`; тесты подставляют свои.
Тексты сообщений в журнал не пишутся — только id, числа и типы ошибок.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import Protocol

from supabase import Client

from solomon.config import Settings
from solomon.db import later as db_later
from solomon.db.later import DeferredMessage
from solomon.db.rpc import DatabaseError
from solomon.services.tasks import LATER_LIMIT, Button, FileLoader, RecordOutcome
from solomon.services.understanding import Clock, next_retry, retry_pause

logger = logging.getLogger(__name__)

# Взятое раньше — брошено перезапуском или упавшим заходом (§33.2), как у поисков.
STALE_AFTER = timedelta(minutes=10)


class LaterStore(Protocol):
    """Очередь отложенных сообщений владельца в базе (§3.2)."""

    async def take(
        self, due_before: datetime, stale_before: datetime
    ) -> DeferredMessage | None: ...

    async def release(self, message_id: str, retry_at: datetime) -> bool: ...

    async def finish(self, message_id: str) -> bool: ...


class DatabaseLaterStore:
    """Очередь в настоящей базе: владелец — из настроек (инвариант 2)."""

    def __init__(self, settings: Settings, db: Client) -> None:
        self._owner = settings.owner_telegram_id
        self._db = db

    async def take(self, due_before: datetime, stale_before: datetime) -> DeferredMessage | None:
        return await db_later.take_deferred(
            self._db,
            owner_telegram_id=self._owner,
            due_before=due_before,
            stale_before=stale_before,
        )

    async def release(self, message_id: str, retry_at: datetime) -> bool:
        return await db_later.release_deferred(
            self._db, owner_telegram_id=self._owner, message_id=message_id, retry_at=retry_at
        )

    async def finish(self, message_id: str) -> bool:
        return await db_later.finish_deferred(
            self._db, owner_telegram_id=self._owner, message_id=message_id
        )


class Retrier(Protocol):
    """Разбор отложенного сообщения — `TaskService.retry`."""

    async def retry(
        self,
        row: DeferredMessage,
        *,
        give_up: bool = False,
        load_file: FileLoader | None = None,
    ) -> RecordOutcome: ...


class LaterReplier(Protocol):
    """Сообщение владельцу ответом на его исходное, с кнопками ответа.

    Возвращает id сообщения в Telegram. Приходит из сборки замыканием над
    ботом: сервис не знает про aiogram.
    """

    async def __call__(
        self, *, chat_id: int, reply_to: int, text: str, buttons: Sequence[Button] = ()
    ) -> int: ...


# Запустить поиск, о котором прозвучало «Ищу» в ответе на отложенное
# сообщение (`techspec/24-search.md` §24.3): ответ поиска не обгонит его.
SearchLauncher = Callable[[str], bool]


class LaterService:
    """Очередь отложенных сообщений: разбирать по одному в фоне, отвечать на
    исходное. Собирается один раз при запуске бота; работник один."""

    def __init__(
        self,
        settings: Settings,
        store: LaterStore,
        tasks: Retrier,
        reply: LaterReplier,
        load_file: FileLoader | None = None,
        launch_search: SearchLauncher | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._tasks = tasks
        self._reply = reply
        # Без него отложенный снимок не скачать — «Не смог открыть снимок».
        self._load_file = load_file
        # Без него поиск из отложенного сообщения подхватит тик поисков (§24.3).
        self._launch_search = launch_search
        self._clock = clock or self._now
        self._worker: asyncio.Task[None] | None = None

    def _now(self) -> datetime:
        return datetime.now(self._settings.owner_timezone)

    @classmethod
    def with_database(
        cls,
        settings: Settings,
        db: Client,
        tasks: Retrier,
        reply: LaterReplier,
        load_file: FileLoader | None = None,
        launch_search: SearchLauncher | None = None,
    ) -> LaterService:
        """Обычная сборка: настоящая база, разбор приёма и отправка в Telegram."""
        return cls(
            settings=settings,
            store=DatabaseLaterStore(settings, db),
            tasks=tasks,
            reply=reply,
            load_file=load_file,
            launch_search=launch_search,
        )

    async def tick(self, now: datetime | None = None) -> int:
        """Шаг тика (§6.2): запустить разбор очереди в фоне. Тик его не ждёт —
        вызов модели идёт десятки секунд, — поэтому ушедшего этим шагом нет."""
        self.launch()
        return 0

    def launch(self) -> bool:
        """Запустить работника, если он не идёт. `False` — уже идёт."""
        if self._worker is not None and not self._worker.done():
            return False
        self._worker = asyncio.create_task(self._work())
        return True

    async def wait(self) -> None:
        """Дождаться работника."""
        if self._worker is not None:
            await asyncio.gather(self._worker, return_exceptions=True)

    async def stop(self) -> None:
        """Оборвать работника при остановке бота: взятое сообщение остаётся
        взятым, и после запуска его возьмут снова через `STALE_AFTER`."""
        if self._worker is not None and not self._worker.done():
            self._worker.cancel()
        await self.wait()

    async def _work(self) -> None:
        try:
            await self.run_queue()
        except Exception:  # фоновая задача: исключение иначе потерялось бы молча
            logger.exception("Разбор отложенных сообщений упал")

    async def run_queue(self) -> int:
        """Разобрать очередь, пока голова разбирается (§33.2). Возвращает,
        сколько ответов ушло. Голова не созрела, в работе или снова не
        разобралась — заход кончается: остальные ждут за ней по порядку."""
        sent = 0
        while True:
            now = self._clock()
            try:
                row = await self._store.take(now, now - STALE_AFTER)
            except DatabaseError as error:
                logger.error("Очередь отложенных не прочитана: %s", error)
                return sent
            if row is None:
                return sent
            logger.info(
                "Отложенное сообщение %s взято: попытка %s, ответ записан %s",
                row.id,
                row.attempts,
                "да" if row.reply is not None else "нет",
            )
            if not await self._one(row, now):
                return sent
            sent += 1

    async def _one(self, row: DeferredMessage, now: datetime) -> bool:
        """Одна голова: разобрать или «как есть», отправить, пометить.

        `True` — сообщение больше не ждёт, можно браться за следующее.
        """
        if row.reply is not None:
            # Записано, а не ушло: отправить записанное, без нового разбора.
            return await self._deliver(row, row.reply, (), now)
        deadline = row.received_at + LATER_LIMIT
        outcome = await self._retry(row, give_up=False)
        if outcome.later and now >= deadline:
            logger.info("Отложенное сообщение %s: шесть часов без разбора — как есть", row.id)
            outcome = await self._retry(row, give_up=True)
        if outcome.later:
            retry_at = next_retry(now, row.attempts, deadline)
            if now >= deadline:
                # Даже «как есть» не записалось — база не ответила: снова через паузу.
                retry_at = now + retry_pause(row.attempts)
            await self._release(row, retry_at)
            return False
        if not outcome.message:
            await self._finish(row)
            return True
        if not await self._deliver(row, outcome.message, outcome.buttons, now):
            return False
        if outcome.search_id is not None and self._launch_search is not None:
            self._launch_search(outcome.search_id)
        return True

    async def _retry(self, row: DeferredMessage, *, give_up: bool) -> RecordOutcome:
        """Разбор у `TaskService`. Неожиданный сбой — как временный отказ:
        сообщение не теряется, и шесть часов кончатся «как есть»."""
        try:
            return await self._tasks.retry(row, give_up=give_up, load_file=self._load_file)
        except Exception:  # фоновая попытка не падает молча и не теряет сообщение
            logger.exception("Отложенное сообщение %s: разбор упал", row.id)
            return RecordOutcome(ok=False, message="", later=True)

    async def _deliver(
        self, row: DeferredMessage, text: str, buttons: Sequence[Button], now: datetime
    ) -> bool:
        """Ответом на исходное сообщение, потом — пометить (§33.2, шаг 3).

        Не ушло — сообщение возвращается в очередь с записанным ответом, и
        следующий заход шлёт его снова. В журнал — только тип ошибки: в тексте
        ошибок aiogram — адрес с токеном бота.
        """
        try:
            message_id = await self._reply(
                chat_id=row.chat_id, reply_to=row.telegram_message_id, text=text, buttons=buttons
            )
        except Exception as error:  # noqa: BLE001 - любой отказ Telegram не роняет очередь
            logger.warning("Ответ на отложенное %s не ушёл: %s", row.id, type(error).__name__)
            await self._release(row, now + retry_pause(1))
            return False
        logger.info("Ответ на отложенное %s ушёл: сообщение %s", row.id, message_id)
        await self._finish(row)
        return True

    async def _release(self, row: DeferredMessage, retry_at: datetime) -> None:
        """Вернуть в очередь; база не ответила — строка остаётся взятой, и её
        возьмут снова через `STALE_AFTER`."""
        try:
            await self._store.release(row.id, retry_at)
        except DatabaseError as error:
            logger.error("Отложенное %s не возвращено в очередь: %s", row.id, error)
            return
        logger.info(
            "Отложенное %s ждёт дальше: попыток %s, следующая %s",
            row.id,
            row.attempts,
            retry_at.isoformat(),
        )

    async def _finish(self, row: DeferredMessage) -> None:
        """Снять ожидание. База не ответила — через `STALE_AFTER` возьмут снова
        и отправят записанный ответ ещё раз: дубль возможен, потеря нет."""
        try:
            await self._store.finish(row.id)
        except DatabaseError as error:
            logger.error("Ответ на отложенное %s ушёл, но не помечен: %s", row.id, error)
