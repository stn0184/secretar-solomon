"""Сборка и запуск бота: long polling, входящих соединений не нужно."""

from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher
from anthropic import AsyncAnthropic
from supabase import Client

from solomon import handlers
from solomon.config import Settings
from solomon.middlewares import OwnerOnlyMiddleware
from solomon.services.tasks import TaskService
from solomon.services.understanding import UnderstandingService, create_anthropic_client

logger = logging.getLogger(__name__)


def build_tasks(settings: Settings, db: Client, client: AsyncAnthropic) -> TaskService:
    """Приём поручений целиком: база и модель на своих местах.

    Клиент Claude приходит снаружи — он живёт столько же, сколько бот, и
    создавать его на каждое сообщение значило бы поднимать соединение заново
    (`techspec/05-ai.md` §5.1).
    """
    return TaskService.with_understanding(
        settings, db, UnderstandingService.with_client(settings, client)
    )


def build_dispatcher(
    settings: Settings,
    db: Client | None = None,
    tasks: TaskService | None = None,
) -> Dispatcher:
    """Собрать диспетчер: фильтр владельца снаружи, обработчики внутри.

    Операции над задачами уезжают в workflow data — обработчик получает
    готовый сервис по имени параметра и своих зависимостей не собирает.
    Готовый сервис можно передать снаружи: так его подменяет тест.
    """
    dispatcher = Dispatcher(settings=settings, db=db, tasks=tasks)
    dispatcher.update.outer_middleware(OwnerOnlyMiddleware(settings.owner_telegram_id))
    dispatcher.include_router(handlers.build_router())
    return dispatcher


async def run(settings: Settings, db: Client | None = None) -> None:
    """Запустить опрос Telegram и работать, пока не остановят."""
    bot = Bot(token=settings.telegram_bot_token)
    client = create_anthropic_client(settings)
    tasks = build_tasks(settings, db, client) if db is not None else None
    dispatcher = build_dispatcher(settings, db=db, tasks=tasks)
    logger.info(
        "Соломон запущен: long polling, владелец %s, пояс %s, база %s",
        settings.owner_telegram_id,
        settings.owner_timezone.key,
        settings.supabase_url,
    )
    try:
        await dispatcher.start_polling(bot)
    finally:
        await client.close()
        await bot.session.close()
        logger.info("Соломон остановлен.")
