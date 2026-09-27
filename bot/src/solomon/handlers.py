"""Обработчики сообщений. Сюда приходит только владелец — см. middlewares."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from io import BytesIO

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MaybeInaccessibleMessage,
    Message,
    MessageOriginChannel,
    MessageOriginChat,
    MessageOriginHiddenUser,
    MessageOriginUser,
)
from aiogram.utils.chat_action import ChatActionSender

from solomon import texts
from solomon.db.tasks import SpeechKind
from solomon.services.reminders import ReminderService
from solomon.services.tasks import TaskService

logger = logging.getLogger(__name__)

# Кнопка под напоминанием (`techspec/06-reminders.md` §6.3): в callback
# уезжает только id задачи, и владельца из него не взять — он из настроек.
DONE_PREFIX = "done:"


def done_keyboard(task_id: str) -> InlineKeyboardMarkup:
    """Одна кнопка «Сделано» под напоминанием."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=texts.DONE_BUTTON, callback_data=f"{DONE_PREFIX}{task_id}")]
        ]
    )


def is_plain_text(message: Message) -> bool:
    """Текст, не похожий на команду: команды разбирают свои обработчики."""
    text = message.text
    return text is not None and not text.startswith("/")


@dataclass(frozen=True, slots=True)
class Speech:
    """Голосовое или кружок: что нужно сервису, чтобы записать и расслышать (§9.1)."""

    kind: SpeechKind
    file_id: str
    duration: int


def speech_of(message: Message) -> Speech | None:
    """Голосовое или видео-кружок — или ничего, если это не речь."""
    if message.voice is not None:
        return Speech("voice", message.voice.file_id, message.voice.duration)
    if message.video_note is not None:
        return Speech("video_note", message.video_note.file_id, message.video_note.duration)
    return None


def speech_in(message: Message) -> dict[str, Speech] | bool:
    """Фильтр речи: пропускает голос и кружок и отдаёт обработчику, что пришло.

    Пересланное голосовое — то же голосовое: у него есть файл и длительность,
    а имя отправителя разбирает `forwarded_sender`.
    """
    speech = speech_of(message)
    return {"speech": speech} if speech is not None else False


def is_not_text(message: Message) -> bool:
    """Фотография, стикер, аудиофайл — ни текст, ни речь."""
    return message.text is None and speech_of(message) is None


def forwarded_sender(message: Message) -> str | None:
    """Чьё это сообщение, если его переслали.

    Имя нужно разбору: «пришлю смету завтра» от Ани — обещание мне, а не моё
    (`spec.md` §3.3). Отправитель, скрывший себя, приходит одним именем;
    канал и группа — названием. Не переслано — `None`, и в промпте этой
    строки нет.
    """
    origin = message.forward_origin
    if isinstance(origin, MessageOriginUser):
        return origin.sender_user.full_name
    if isinstance(origin, MessageOriginHiddenUser):
        return origin.sender_user_name
    if isinstance(origin, MessageOriginChat):
        return origin.sender_chat.title
    if isinstance(origin, MessageOriginChannel):
        return origin.chat.title
    return None


async def handle_start(message: Message) -> None:
    """Приветствие владельцу."""
    logger.info("Команда /start")
    await message.answer(texts.START)


async def handle_help(message: Message) -> None:
    """Короткий список того, что уже работает."""
    logger.info("Команда /help")
    await message.answer(texts.HELP)


async def handle_text(message: Message, tasks: TaskService | None) -> None:
    """Текст владельца — поручение: записываем и подтверждаем своими словами.

    Пересланное сообщение с текстом — такой же текст: разбирается как
    поручение, а имя отправителя уходит в разбор отдельным полем. Решение
    принимает слой операций, обработчик только отправляет его ответ.
    """
    if tasks is None:
        # Бота запустили без клиента базы — записывать некуда, и молчать о
        # этом нельзя (инвариант 4).
        logger.error("Поручение некуда записать: бот собран без базы")
        await message.answer(texts.NOT_SAVED)
        return

    outcome = await tasks.record_from_message(
        chat_id=message.chat.id,
        telegram_message_id=message.message_id,
        text=message.text or "",
        forwarded_from=forwarded_sender(message),
    )
    await message.answer(outcome.message)


async def handle_speech(
    message: Message, bot: Bot, tasks: TaskService | None, speech: Speech
) -> None:
    """Голосовое или кружок владельца — поручение: скачать, расслышать, записать.

    Файл качается в память замыканием, которое уходит в сервис: сервис не
    знает про aiogram, а байты на диск не попадают (`techspec/09-voice.md`
    §9.2). Пока идёт распознавание и разбор, в чате висит «печатает…» — это
    дольше текста, и молчание пугает; статус живёт пять секунд, поэтому его
    повторяет `ChatActionSender`.
    """
    if tasks is None:
        logger.error("Голосовое некуда записать: бот собран без базы")
        await message.answer(texts.NOT_SAVED)
        return

    async def load_audio() -> bytes:
        buffer = BytesIO()
        await bot.download(speech.file_id, destination=buffer)
        return buffer.getvalue()

    async with ChatActionSender.typing(chat_id=message.chat.id, bot=bot):
        outcome = await tasks.record_from_voice(
            chat_id=message.chat.id,
            telegram_message_id=message.message_id,
            kind=speech.kind,
            file_id=speech.file_id,
            duration=speech.duration,
            load_audio=load_audio,
            forwarded_from=forwarded_sender(message),
        )
    await message.answer(outcome.message)


async def handle_done(callback: CallbackQuery, reminders: ReminderService | None) -> None:
    """Нажата кнопка «Сделано» под напоминанием (§6.3).

    Порядок: сначала база, потом сообщение и ответ на callback — иначе бот
    зачеркнул бы задачу, которую не закрыл (инвариант 4). Повторное нажатие
    безвредно: задача уже закрыта, а отметка в сообщении уже стоит, и
    редактировать нечего.
    """
    task_id = (callback.data or "").removeprefix(DONE_PREFIX)
    if reminders is None or not task_id:
        logger.error("Кнопку «Сделано» некому обработать: бот собран без базы")
        await callback.answer(texts.NOT_CLOSED)
        return

    completion = await reminders.complete(task_id)
    if completion.ok:
        await mark_done(callback.message)
    await callback.answer(completion.answer)


async def mark_done(message: MaybeInaccessibleMessage | None) -> None:
    """Убрать кнопку и дописать «✓ Сделано» под напоминанием.

    Старое сообщение Telegram отдаёт без текста (`InaccessibleMessage`), и
    редактировать там нечего — задача уже закрыта, а это только отметка.
    """
    if not isinstance(message, Message) or message.text is None:
        return
    if message.text.endswith(texts.DONE_MARK):
        return
    try:
        await message.edit_text(texts.done_message(message.text), reply_markup=None)
    except TelegramBadRequest as error:
        # Сообщение старое или уже отредактировано: задача закрыта, и это
        # важнее, чем вид напоминания.
        logger.warning("Напоминание не отредактировано: %s", error)


async def handle_not_text(message: Message) -> None:
    """Ни текст, ни речь — вежливый отказ, и ничего не сохраняется."""
    logger.info("Сообщение не текстом и не голосом: %s", message.content_type)
    await message.answer(texts.NOT_TEXT)


def build_router() -> Router:
    """Новый роутер обработчиков.

    Именно фабрика, а не общий объект модуля: роутер aiogram привязывается
    к одному диспетчеру навсегда, и второй сборке достался бы занятый.
    Порядок важен: команды разбираются раньше свободного текста, речь —
    раньше отказа на всё остальное.
    """
    router = Router(name="basic")
    router.message.register(handle_start, CommandStart())
    router.message.register(handle_help, Command("help"))
    router.message.register(handle_text, is_plain_text)
    router.message.register(handle_speech, speech_in)
    router.message.register(handle_not_text, is_not_text)
    router.callback_query.register(handle_done, F.data.startswith(DONE_PREFIX))
    return router
