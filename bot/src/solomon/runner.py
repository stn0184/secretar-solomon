"""Сборка и запуск бота: long polling, входящих соединений не нужно."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from aiogram import Bot, Dispatcher
from anthropic import AsyncAnthropic
from deepgram import AsyncDeepgramClient
from supabase import Client

from solomon import handlers
from solomon.config import Settings
from solomon.middlewares import OwnerOnlyMiddleware
from solomon.services.reminders import ReminderService, mirror_timezone
from solomon.services.tasks import TaskService
from solomon.services.transcription import DeepgramTranscriber, create_deepgram_client
from solomon.services.understanding import UnderstandingService, create_anthropic_client

logger = logging.getLogger(__name__)


def build_tasks(
    settings: Settings, db: Client, client: AsyncAnthropic, speech: AsyncDeepgramClient
) -> TaskService:
    """Приём поручений целиком: база, модель и распознавание на своих местах.

    Клиенты Claude и Deepgram приходят снаружи — они живут столько же,
    сколько бот, и создавать их на каждое сообщение значило бы поднимать
    соединение заново (`techspec/05-ai.md` §5.1, `techspec/09-voice.md` §9.2).
    """
    return TaskService.with_understanding(
        settings,
        db,
        UnderstandingService.with_client(settings, client, db),
        DeepgramTranscriber.with_client(speech),
    )


def build_reminders(settings: Settings, db: Client, bot: Bot) -> ReminderService:
    """Цикл напоминаний: база своя, отправка — через этого бота.

    Отправка приходит в сервис замыканием, а не объектом aiogram: сервис
    остаётся без знания о Telegram, а кнопка собирается там же, где разбирается
    её нажатие (`handlers.py`). Чат — личный чат владельца: его id совпадает
    с id пользователя, других чатов у помощника нет. Строка «Перенёс»
    (`techspec/11-edit.md` §11.4) уходит своим замыканием — без кнопки:
    это не напоминание.
    """

    async def notify(*, text: str, task_id: str) -> int:
        message = await bot.send_message(
            chat_id=settings.owner_telegram_id,
            text=text,
            reply_markup=handlers.done_keyboard(task_id),
        )
        return message.message_id

    async def announce(*, text: str) -> int:
        message = await bot.send_message(chat_id=settings.owner_telegram_id, text=text)
        return message.message_id

    return ReminderService.with_database(settings, db, notify, announce)


def build_dispatcher(
    settings: Settings,
    db: Client | None = None,
    tasks: TaskService | None = None,
    reminders: ReminderService | None = None,
) -> Dispatcher:
    """Собрать диспетчер: фильтр владельца снаружи, обработчики внутри.

    Операции над задачами уезжают в workflow data — обработчик получает
    готовый сервис по имени параметра и своих зависимостей не собирает.
    Готовый сервис можно передать снаружи: так его подменяет тест.
    """
    dispatcher = Dispatcher(settings=settings, db=db, tasks=tasks, reminders=reminders)
    dispatcher.update.outer_middleware(OwnerOnlyMiddleware(settings.owner_telegram_id))
    dispatcher.include_router(handlers.build_router())
    return dispatcher


async def run(settings: Settings, db: Client | None = None) -> None:
    """Запустить опрос Telegram и работать, пока не остановят."""
    bot = Bot(token=settings.telegram_bot_token)
    client = create_anthropic_client(settings)
    speech = create_deepgram_client(settings)
    tasks = build_tasks(settings, db, client, speech) if db is not None else None
    reminders = build_reminders(settings, db, bot) if db is not None else None
    dispatcher = build_dispatcher(settings, db=db, tasks=tasks, reminders=reminders)
    if db is not None:
        # Пояс владельца — в базу до первого сообщения: правка срока из
        # приложения берёт его оттуда (`techspec/11-edit.md` §11.3). Сбой —
        # строка в журнале, бот работает дальше.
        await mirror_timezone(settings, db)
    # Цикл напоминаний живёт рядом с polling, в том же процессе
    # (`techspec/06-reminders.md` §6.2): отдельного планировщика нет.
    ticking = asyncio.create_task(reminders.run()) if reminders is not None else None
    logger.info(
        "Соломон запущен: long polling, владелец %s, пояс %s, база %s",
        settings.owner_telegram_id,
        settings.owner_timezone.key,
        settings.supabase_url,
    )
    try:
        await dispatcher.start_polling(bot)
    finally:
        if ticking is not None:
            # Штатная остановка: цикл отменяется и доигрывает CancelledError.
            ticking.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ticking
        await client.close()
        await bot.session.close()
        logger.info("Соломон остановлен.")
