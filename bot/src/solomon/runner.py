"""Сборка и запуск бота: long polling, входящих соединений не нужно."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Sequence

from aiogram import Bot, Dispatcher
from aiogram.types import LinkPreviewOptions, ReplyParameters
from anthropic import AsyncAnthropic
from deepgram import AsyncDeepgramClient
from supabase import Client

from solomon import handlers
from solomon.config import Settings
from solomon.middlewares import OwnerOnlyMiddleware
from solomon.services.chats import ChatService, Connection, OwnerSender
from solomon.services.instagram import InstagramService
from solomon.services.reminders import ReminderService, mirror_timezone
from solomon.services.search import SearchService
from solomon.services.tasks import Button, TaskService
from solomon.services.transcription import DeepgramTranscriber, create_deepgram_client
from solomon.services.understanding import UnderstandingService, create_anthropic_client
from solomon.telegram import TelegramSession

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


def build_searches(
    settings: Settings, db: Client, bot: Bot, client: AsyncAnthropic
) -> SearchService:
    """Поиски по поручению (`techspec/24-search.md` §24.3): база, тот же клиент
    Claude и отправка ответом на просьбу владельца.

    Ответ уходит ответом на сообщение с просьбой — и тогда, когда его уже
    удалили (`allow_sending_without_reply`), — а превью ссылки выключено: из
    пяти ссылок картинка первой — шум.
    """

    async def reply(*, chat_id: int, reply_to: int, text: str) -> int:
        message = await bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_parameters=ReplyParameters(message_id=reply_to, allow_sending_without_reply=True),
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        return message.message_id

    return SearchService.with_database(settings, db, client, reply)


def owner_sender(settings: Settings, bot: Bot) -> OwnerSender:
    """Сообщение о чатах — только владельцу в чат с Соломоном (`chat_id` — его
    id) и без `business_connection_id`: в бизнес-чаты и в Direct бот не пишет
    никогда (§25.2, §26.4)."""

    async def send(*, text: str, buttons: Sequence[Button] = ()) -> int:
        message = await bot.send_message(
            chat_id=settings.owner_telegram_id,
            text=text,
            reply_markup=handlers.keyboard(buttons),
        )
        return message.message_id

    return send


def build_chats(
    settings: Settings,
    db: Client,
    bot: Bot,
    client: AsyncAnthropic,
    speech: AsyncDeepgramClient,
) -> ChatService:
    """Личные чаты (`techspec/25-chats.md`): база, тот же клиент Claude, Deepgram
    и две связи с Telegram — отправка владельцу (`owner_sender`) и чьё
    подключение (`getBusinessConnection`).
    """

    async def lookup(connection_id: str) -> Connection:
        connection = await bot.get_business_connection(business_connection_id=connection_id)
        return Connection(user_id=connection.user.id, is_enabled=connection.is_enabled)

    return ChatService.with_database(
        settings,
        db,
        client,
        owner_sender(settings, bot),
        lookup,
        DeepgramTranscriber.with_client(speech),
    )


def build_instagram(settings: Settings, bot: Bot, chats: ChatService) -> InstagramService | None:
    """Direct в Instagram (`techspec/26-instagram.md`): опрос своим ключом в
    общий путь чатов. Ключа нет — `None`, бот работает как без него."""
    return InstagramService.with_client(settings, chats, owner_sender(settings, bot))


def build_reminders(
    settings: Settings,
    db: Client,
    bot: Bot,
    searches: SearchService | None = None,
    chats: ChatService | None = None,
    instagram: InstagramService | None = None,
) -> ReminderService:
    """Цикл напоминаний: база своя, отправка — через этого бота.

    Отправка приходит в сервис замыканием, а не объектом aiogram: сервис
    остаётся без знания о Telegram, а кнопка собирается там же, где разбирается
    её нажатие (`handlers.py`). Чат — личный чат владельца: его id совпадает
    с id пользователя, других чатов у помощника нет. Строка «Перенёс»
    (`techspec/11-edit.md` §11.4) уходит своим замыканием — без кнопки:
    это не напоминание. Шаг тика о поисках (§24.3) — `searches`, о личных
    чатах (§25) — `chats`, опрос Direct (§26) — `instagram`.
    """

    async def notify(*, text: str, task_id: str, occurrence: int | None = None) -> int:
        message = await bot.send_message(
            chat_id=settings.owner_telegram_id,
            text=text,
            reply_markup=handlers.done_keyboard(task_id, occurrence),
        )
        return message.message_id

    async def announce(*, text: str) -> int:
        message = await bot.send_message(chat_id=settings.owner_telegram_id, text=text)
        return message.message_id

    return ReminderService.with_database(
        settings, db, notify, announce, searches=searches, chats=chats, instagram=instagram
    )


def build_dispatcher(
    settings: Settings,
    db: Client | None = None,
    tasks: TaskService | None = None,
    reminders: ReminderService | None = None,
    searches: SearchService | None = None,
    chats: ChatService | None = None,
) -> Dispatcher:
    """Собрать диспетчер: фильтр владельца снаружи, обработчики внутри.

    Операции над задачами уезжают в workflow data — обработчик получает
    готовый сервис по имени параметра и своих зависимостей не собирает.
    Готовый сервис можно передать снаружи: так его подменяет тест. `searches`
    обработчик зовёт, чтобы запустить поиск после ответа «Ищу» (§24.3).
    `chats` — личные чаты: бизнес-обновления идут своим роутером (§25.2), и
    long polling просит их у Telegram сам — по зарегистрированным
    обработчикам.
    """
    dispatcher = Dispatcher(
        settings=settings,
        db=db,
        tasks=tasks,
        reminders=reminders,
        searches=searches,
        chats=chats,
    )
    dispatcher.update.outer_middleware(OwnerOnlyMiddleware(settings.owner_telegram_id))
    dispatcher.include_router(handlers.build_router())
    dispatcher.include_router(handlers.build_business_router())
    return dispatcher


async def run(settings: Settings, db: Client | None = None) -> None:
    """Запустить опрос Telegram и работать, пока не остановят."""
    # Своя сессия: соединение с Telegram ждёт 5 с, а не минуту (`telegram.py`).
    bot = Bot(token=settings.telegram_bot_token, session=TelegramSession())
    client = create_anthropic_client(settings)
    speech = create_deepgram_client(settings)
    tasks = build_tasks(settings, db, client, speech) if db is not None else None
    searches = build_searches(settings, db, bot, client) if db is not None else None
    chats = build_chats(settings, db, bot, client, speech) if db is not None else None
    instagram = build_instagram(settings, bot, chats) if chats is not None else None
    reminders = (
        build_reminders(settings, db, bot, searches, chats, instagram) if db is not None else None
    )
    dispatcher = build_dispatcher(
        settings, db=db, tasks=tasks, reminders=reminders, searches=searches, chats=chats
    )
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
        if searches is not None:
            # Поиски в работе обрываются: строка остаётся начатой, и после
            # запуска тик возьмёт её через десять минут (§24.3).
            await searches.stop()
        if instagram is not None:
            # Опрос в работе обрывается: курсор не сдвинут, и после запуска
            # опрос прочтёт то же (§26.3).
            await instagram.stop()
        if chats is not None:
            # Разбор в работе обрывается: сообщения остаются неразобранными,
            # и после запуска тик разберёт их снова (§25.3).
            await chats.stop()
        await client.close()
        await bot.session.close()
        logger.info("Соломон остановлен.")
