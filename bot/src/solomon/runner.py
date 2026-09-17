"""Сборка и запуск бота: long polling, входящих соединений не нужно."""

from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher
from supabase import Client

from solomon import handlers
from solomon.config import Settings
from solomon.middlewares import OwnerOnlyMiddleware
from solomon.services.tasks import TaskService

logger = logging.getLogger(__name__)


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
    service = tasks
    if service is None and db is not None:
        service = TaskService.with_database(settings, db)
    dispatcher = Dispatcher(settings=settings, db=db, tasks=service)
    dispatcher.update.outer_middleware(OwnerOnlyMiddleware(settings.owner_telegram_id))
    dispatcher.include_router(handlers.build_router())
    return dispatcher


async def run(settings: Settings, db: Client | None = None) -> None:
    """Запустить опрос Telegram и работать, пока не остановят."""
    bot = Bot(token=settings.telegram_bot_token)
    dispatcher = build_dispatcher(settings, db=db)
    logger.info(
        "Соломон запущен: long polling, владелец %s, база %s",
        settings.owner_telegram_id,
        settings.supabase_url,
    )
    try:
        await dispatcher.start_polling(bot)
    finally:
        await bot.session.close()
        logger.info("Соломон остановлен.")
