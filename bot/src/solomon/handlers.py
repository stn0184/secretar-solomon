"""Обработчики сообщений. Сюда приходит только владелец — см. middlewares."""

from __future__ import annotations

import logging
from collections.abc import Sequence
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
from solomon.services import edits
from solomon.services.reminders import ReminderService
from solomon.services.tasks import Button, PressOutcome, Swipe, TaskService
from solomon.services.understanding import ImageType

logger = logging.getLogger(__name__)

# Кнопка под напоминанием (`techspec/06-reminders.md` §6.3): в callback
# уезжает только id задачи, и владельца из него не взять — он из настроек.
# У повторяющейся задачи — ещё и раз в секундах Unix: `done:<id>:<раз>`
# (`techspec/13-repeat.md` §13.3); кнопки до этапа 011 — `done:<id>`.
DONE_PREFIX = "done:"


def done_data(task_id: str, occurrence: int | None = None) -> str:
    """Callback «Сделано»: id задачи и, у повторяющейся, раз."""
    if occurrence is None:
        return f"{DONE_PREFIX}{task_id}"
    return f"{DONE_PREFIX}{task_id}:{occurrence}"


def parse_done(data: str) -> tuple[str, int | None] | None:
    """Разобрать callback «Сделано»: задача и раз (или `None`). Кривой — `None`."""
    task_id, _, occurrence = data.removeprefix(DONE_PREFIX).partition(":")
    if not task_id:
        return None
    if not occurrence:
        return task_id, None
    if not (occurrence.isascii() and occurrence.isdigit()):
        return None
    return task_id, int(occurrence)


def done_keyboard(task_id: str, occurrence: int | None = None) -> InlineKeyboardMarkup:
    """Одна кнопка «Сделано» под напоминанием."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.DONE_BUTTON, callback_data=done_data(task_id, occurrence)
                )
            ]
        ]
    )


def keyboard(buttons: Sequence[Button]) -> InlineKeyboardMarkup | None:
    """Кнопки ответа (`techspec/12-chat-edit.md` §12.6) — по одной в ряд.

    Кнопок нет — клавиатуры нет: при правке сообщения это убирает прежние
    кнопки. Что в кнопке и её callback, решает слой операций.
    """
    if not buttons:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=button.text, callback_data=button.data)]
            for button in buttons
        ]
    )


def swipe_of(message: Message) -> Swipe | None:
    """На что владелец ответил свайпом «ответить» (§12.2) — или ничего.

    Ответ боту — напоминание или другое его сообщение, ответ себе — своё
    прежнее сообщение: какая это задача, разбирает слой операций по базе.
    У голосового текста в Telegram нет — его расшифровку сервис возьмёт из
    базы. Пересланное задачу не правит, и его свайп не читается.
    """
    replied = message.reply_to_message
    if replied is None or message.forward_origin is not None:
        return None
    author = replied.from_user
    return Swipe(
        telegram_message_id=replied.message_id,
        from_bot=author is not None and author.is_bot,
        text=replied.text or replied.caption,
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


# Предел файла снимка (`techspec/14-photo.md` §14.1): 3,5 МБ. В base64 это
# меньше 5 МБ — самого строгого предела картинки у Claude.
PHOTO_LIMIT = 3_670_016

# Картинки файлом, которые модель принимает как есть (§14.1). Прочие `image/*`
# — HEIC, GIF, TIFF, SVG — отказ «не открою».
IMAGE_TYPES: dict[str, ImageType] = {
    "image/jpeg": "image/jpeg",
    "image/png": "image/png",
    "image/webp": "image/webp",
}


@dataclass(frozen=True, slots=True)
class Photo:
    """Снимок: что нужно сервису, чтобы записать его и показать модели (§14.1)."""

    file_id: str
    media_type: ImageType
    caption: str


def fits(file_size: int | None) -> bool:
    """Файл не больше предела; размер, который Telegram не назвал, подходит."""
    return file_size is None or file_size <= PHOTO_LIMIT


def photo_of(message: Message) -> Photo | None:
    """Фото или картинка файлом, которую можно показать модели, — или ничего.

    У фото Telegram присылает несколько размеров одного снимка: берётся самый
    крупный, что влезает в предел. GIF-анимация приходит с `animation` и
    `document` сразу — это не снимок. Подпись — как есть, нет её — пустая
    строка.
    """
    if message.animation is not None:
        return None
    caption = message.caption or ""
    if message.photo:
        sizes = [size for size in message.photo if fits(size.file_size)]
        if not sizes:
            return None
        largest = max(sizes, key=lambda size: size.width * size.height)
        return Photo(largest.file_id, "image/jpeg", caption)
    document = message.document
    if document is None or not fits(document.file_size):
        return None
    media_type = IMAGE_TYPES.get((document.mime_type or "").lower())
    return Photo(document.file_id, media_type, caption) if media_type is not None else None


def photo_in(message: Message) -> dict[str, Photo] | bool:
    """Фильтр снимка: пропускает фото и картинку файлом и отдаёт, что пришло."""
    photo = photo_of(message)
    return {"photo": photo} if photo is not None else False


def refused_image(message: Message) -> bool:
    """Картинка, которую не открыть: не того вида или больше предела (§14.1).

    Фото, у которого ни один размер не влез в предел, — тоже: так не бывает,
    мелкие размеры Telegram — килобайты, но молча терять снимок нельзя.
    """
    if message.animation is not None or photo_of(message) is not None:
        return False
    if message.photo:
        return True
    document = message.document
    return document is not None and (document.mime_type or "").lower().startswith("image/")


def is_not_text(message: Message) -> bool:
    """Стикер, видео, аудиофайл, документ — ни текст, ни речь, ни снимок."""
    return (
        message.text is None
        and speech_of(message) is None
        and photo_of(message) is None
        and not refused_image(message)
    )


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
        swipe=swipe_of(message),
    )
    await message.answer(outcome.message, reply_markup=keyboard(outcome.buttons))


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
            swipe=swipe_of(message),
        )
    await message.answer(outcome.message, reply_markup=keyboard(outcome.buttons))


async def handle_done(callback: CallbackQuery, reminders: ReminderService | None) -> None:
    """Нажата кнопка «Сделано» под напоминанием (§6.3).

    Порядок: сначала база, потом сообщение и ответ на callback — иначе бот
    зачеркнул бы задачу, которую не закрыл (инвариант 4). Повторное нажатие
    безвредно: задача уже закрыта, а отметка в сообщении уже стоит, и
    редактировать нечего. У повторяющейся задачи раз из кнопки не даёт
    перескочить через раз (§13.3), а отметка называет срок, какой вернула
    база.
    """
    parsed = parse_done(callback.data or "")
    if reminders is None:
        logger.error("Кнопку «Сделано» некому обработать: бот собран без базы")
        await callback.answer(texts.NOT_CLOSED)
        return
    if parsed is None:
        logger.warning("Кнопка «Сделано» с непонятными данными: %r", callback.data)
        await callback.answer(texts.DONE_UNKNOWN)
        return

    task_id, occurrence = parsed
    completion = await reminders.complete(task_id, occurrence)
    if completion.ok:
        await mark_done(callback.message, completion.mark)
    await callback.answer(completion.answer)


async def mark_done(message: MaybeInaccessibleMessage | None, mark: str = texts.DONE_MARK) -> None:
    """Убрать кнопку и дописать отметку «✓ Сделано…» под напоминанием.

    Старое сообщение Telegram отдаёт без текста (`InaccessibleMessage`), и
    редактировать там нечего — задача уже закрыта, а это только отметка.
    Отметка уже та же — редактировать тоже нечего.
    """
    if not isinstance(message, Message) or message.text is None:
        return
    text = texts.done_message(message.text, mark)
    if text == message.text:
        return
    try:
        await message.edit_text(text, reply_markup=None)
    except TelegramBadRequest as error:
        # Сообщение старое или уже отредактировано: задача закрыта, и это
        # важнее, чем вид напоминания.
        logger.warning("Напоминание не отредактировано: %s", error)


async def handle_pick(callback: CallbackQuery, tasks: TaskService | None) -> None:
    """Нажата кнопка задачи под вопросом «Какую задачу…?» (§12.6).

    Порядок тот же, что у «Сделано»: сначала база, потом сообщение — вопрос
    сменяется ответом, только когда правка записана (инвариант 4). Отказ —
    всплывающий ответ, а вопрос с кнопками остаётся: можно нажать ещё раз.
    """
    message = callback.message
    if tasks is None or message is None:
        logger.error("Кнопку задачи некому обработать: бот собран без базы")
        await callback.answer(texts.NOT_PICKED)
        return
    parsed = edits.parse_pick(callback.data or "")
    if parsed is None:
        logger.warning("Кнопка задачи с непонятными данными: %r", callback.data)
        await callback.answer(texts.DONE_UNKNOWN)
        return
    telegram_message_id, task_id = parsed
    outcome = await tasks.pick(
        chat_id=message.chat.id, telegram_message_id=telegram_message_id, task_id=task_id
    )
    await answer_press(callback, outcome)


async def handle_apart(callback: CallbackQuery, tasks: TaskService | None) -> None:
    """Нажата «Записать отдельно» под «Это уже записано» (§15.4).

    Порядок тот же, что у кнопки задачи: сначала база, потом сообщение —
    «Это уже записано» сменяется ответом записи, только когда задача легла
    (инвариант 4). Отказ — всплывающий ответ, кнопка остаётся.
    """
    message = callback.message
    if tasks is None or message is None:
        logger.error("Кнопку «Записать отдельно» некому обработать: бот собран без базы")
        await callback.answer(texts.NOT_SAVED)
        return
    telegram_message_id = edits.parse_apart(callback.data or "")
    if telegram_message_id is None:
        logger.warning("Кнопка «Записать отдельно» с непонятными данными: %r", callback.data)
        await callback.answer(texts.MESSAGE_UNKNOWN)
        return
    outcome = await tasks.apart(chat_id=message.chat.id, telegram_message_id=telegram_message_id)
    await answer_press(callback, outcome)


async def handle_reopen(callback: CallbackQuery, tasks: TaskService | None) -> None:
    """Нажата «Вернуть» под «Закрыл» или «Убрал из списка» (§12.6).

    Задача снова активна и с напоминаниями по сроку — и только после ответа
    базы сообщение меняется на «Вернул в работу». Уже активная задача —
    тот же ответ без записи: второе нажатие безвредно.
    """
    if tasks is None:
        logger.error("Кнопку «Вернуть» некому обработать: бот собран без базы")
        await callback.answer(texts.NOT_REOPENED)
        return
    task_id = edits.parse_reopen(callback.data or "")
    if task_id is None:
        logger.warning("Кнопка «Вернуть» с непонятными данными: %r", callback.data)
        await callback.answer(texts.DONE_UNKNOWN)
        return
    outcome = await tasks.reopen(task_id=task_id)
    await answer_press(callback, outcome)


async def handle_back(callback: CallbackQuery, tasks: TaskService | None) -> None:
    """Нажата «Вернуть» под «Отметил» или «Пропускаю» (§13.3).

    Задача возвращается на прежний раз — и только после ответа базы
    сообщение меняется на «Вернул в работу». Ушла дальше или удалена —
    подсказка, сообщение остаётся.
    """
    if tasks is None:
        logger.error("Кнопку «Вернуть» некому обработать: бот собран без базы")
        await callback.answer(texts.NOT_REOPENED)
        return
    parsed = edits.parse_back(callback.data or "")
    if parsed is None:
        logger.warning("Кнопка «Вернуть» с непонятными данными: %r", callback.data)
        await callback.answer(texts.DONE_UNKNOWN)
        return
    task_id, moved_from, moved_to = parsed
    outcome = await tasks.back(task_id=task_id, back_to=moved_from, moved_from=moved_to)
    await answer_press(callback, outcome)


async def answer_press(callback: CallbackQuery, outcome: PressOutcome) -> None:
    """Ответ на нажатие: сменить сообщение с кнопками или всплыть подсказкой.

    Сообщение не сменилось (старое, уже с этим текстом) — ответ всплывает
    подсказкой: база уже записала, и человек должен об этом узнать.
    """
    if outcome.replace and await replace_text(
        callback.message, outcome.message, keyboard(outcome.buttons)
    ):
        await callback.answer()
        return
    await callback.answer(outcome.message)


async def replace_text(
    message: MaybeInaccessibleMessage | None, text: str, markup: InlineKeyboardMarkup | None
) -> bool:
    """Заменить текст и кнопки сообщения бота; `False` — Telegram не дал."""
    if not isinstance(message, Message):
        return False
    try:
        await message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as error:
        logger.warning("Сообщение с кнопками не отредактировано: %s", error)
        return False
    return True


async def handle_photo(message: Message, bot: Bot, tasks: TaskService | None, photo: Photo) -> None:
    """Фото или картинка файлом — поручение: скачать, показать модели, записать.

    Как у голоса: снимок качается в память замыканием, которое уходит в
    сервис, и на диск не попадает (`techspec/14-photo.md` §14.2). Снимок
    разбирается дольше текста — пока идёт разбор, в чате висит «печатает…».
    Свайп у снимка не читается: правки у снимка нет (§14.3).
    """
    if tasks is None:
        logger.error("Снимок некуда записать: бот собран без базы")
        await message.answer(texts.NOT_SAVED)
        return

    async def load_image() -> bytes:
        buffer = BytesIO()
        await bot.download(photo.file_id, destination=buffer)
        return buffer.getvalue()

    async with ChatActionSender.typing(chat_id=message.chat.id, bot=bot):
        outcome = await tasks.record_from_photo(
            chat_id=message.chat.id,
            telegram_message_id=message.message_id,
            file_id=photo.file_id,
            media_type=photo.media_type,
            caption=photo.caption,
            load_image=load_image,
            forwarded_from=forwarded_sender(message),
        )
    await message.answer(outcome.message, reply_markup=keyboard(outcome.buttons))


async def handle_refused_image(message: Message) -> None:
    """Картинка не того вида или больше предела — отказ, и ничего не сохраняется."""
    document = message.document
    logger.info(
        "Картинка не открыта: %s, байт %s",
        document.mime_type if document is not None else "photo",
        document.file_size if document is not None else None,
    )
    await message.answer(texts.FILE_REFUSED)


async def handle_not_text(message: Message) -> None:
    """Ни текст, ни речь, ни снимок — вежливый отказ, и ничего не сохраняется."""
    logger.info("Сообщение не текстом, не голосом и не снимком: %s", message.content_type)
    await message.answer(texts.NOT_TEXT)


def build_router() -> Router:
    """Новый роутер обработчиков.

    Именно фабрика, а не общий объект модуля: роутер aiogram привязывается
    к одному диспетчеру навсегда, и второй сборке достался бы занятый.
    Порядок важен: команды разбираются раньше свободного текста, речь и
    снимок — раньше отказа на всё остальное.
    """
    router = Router(name="basic")
    router.message.register(handle_start, CommandStart())
    router.message.register(handle_help, Command("help"))
    router.message.register(handle_text, is_plain_text)
    router.message.register(handle_speech, speech_in)
    router.message.register(handle_photo, photo_in)
    router.message.register(handle_refused_image, refused_image)
    router.message.register(handle_not_text, is_not_text)
    router.callback_query.register(handle_done, F.data.startswith(DONE_PREFIX))
    router.callback_query.register(handle_pick, F.data.startswith(edits.PICK_PREFIX))
    router.callback_query.register(handle_reopen, F.data.startswith(edits.REOPEN_PREFIX))
    router.callback_query.register(handle_back, F.data.startswith(edits.BACK_PREFIX))
    router.callback_query.register(handle_apart, F.data.startswith(edits.APART_PREFIX))
    return router
