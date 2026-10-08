"""Обработчики сообщений. Сюда приходит только владелец — см. middlewares.

Исключение — бизнес-обновления личных чатов владельца (`techspec/25-chats.md`
§25.2): их пишут собеседники, и отбирает их не отправитель, а подключение —
в `services/chats.py`. Их обработчики собраны отдельным роутером
(`build_business_router`) и **не отвечают никогда**: ни `answer`, ни
`reply`, ни статуса «печатает» — в бизнес-чат владельца бот не пишет.
"""

from __future__ import annotations

import logging
from asyncio import sleep
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramEntityTooLarge, TelegramNetworkError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BusinessConnection,
    BusinessMessagesDeleted,
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
from aiohttp import ClientConnectionError, ClientPayloadError

from solomon import texts
from solomon.config import Settings
from solomon.db.chats import ChatKind, Platform
from solomon.db.tasks import SpeechKind
from solomon.services import chats as chats_module
from solomon.services import edits
from solomon.services.chats import ChatService, Incoming
from solomon.services.relay import partner_link
from solomon.services.reminders import ReminderService
from solomon.services.search import SearchService
from solomon.services.tasks import Button, PressOutcome, RecordOutcome, Swipe, TaskService
from solomon.services.understanding import ImageType
from solomon.telegram import typing_status

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
    """Одна кнопка «Сделано» под напоминанием о деле без часа и под вопросом о деле.

    Под напоминанием о встрече с часом её нет — решает сервис (§6.3).
    """
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


def forwarded_from_owner(message: Message) -> bool:
    """Переслал ли владелец своё же сообщение (`techspec/18-forwarded.md` §18.2).

    В переписке такие строки идут под именем «Владелец»: его обещание —
    `mine`, а не обещание собеседника. Отправитель — сам владелец, а если
    скрыл себя настройками — совпадает имя в Telegram. До обработчика
    доходит только владелец, поэтому сверка — с автором сообщения.
    """
    owner = message.from_user
    origin = message.forward_origin
    if owner is None:
        return False
    if isinstance(origin, MessageOriginUser):
        return origin.sender_user.id == owner.id
    if isinstance(origin, MessageOriginHiddenUser):
        return origin.sender_user_name == owner.full_name
    return False


def written_at(message: Message) -> datetime:
    """Когда сообщение написано: у пересланного — время оригинала (§18.2).

    Без него «завтра» из вчерашней переписки встало бы на послезавтра.
    """
    if message.forward_origin is not None:
        return message.forward_origin.date
    return message.date


# Скачивание файла из Telegram (`techspec/09-voice.md` §9.3,
# `techspec/14-photo.md` §14.2). Связь сервера с Telegram временами рвётся
# (журнал 2026-10-01): запрос повисает до таймаута или обрывается
# (`Connection reset by peer`), а такой же через минуту проходит за секунды.
# Поэтому вместо одной попытки с долгим сроком — три с короткими. Худший
# случай — 3 × (5 + 13) + 2 × 1 = 56 с; aiohttp округляет срок длиннее 5 с
# вверх до целой секунды — выходит до 59 с. Это не дольше прежнего отказа:
# один `get_file` ждал таймаута сессии, 60 с. Удачная попытка идёт как
# раньше — те же два запроса.
DOWNLOAD_ATTEMPTS = 3
# Путь к файлу — маленький запрос к API, обычно доли секунды: 5 с хватает и
# на новое соединение, а повисший запрос не держит минуту.
GET_FILE_TIMEOUT = 5
# Сам файл — остаток попытки: голосовое в десятки килобайт приходит за
# секунду, а снимок до 3,5 МБ (`PHOTO_LIMIT`) успевает при скорости от 280 КБ/с.
DOWNLOAD_TIMEOUT = 13
# Пауза между попытками — как первая пауза опроса Telegram в aiogram.
DOWNLOAD_PAUSE = 1

# Сбой сети по дороге к Telegram — его стоит повторить. Запрос к API
# (`get_file`) aiogram заворачивает в `TelegramNetworkError` — и таймаут, и
# обрыв; скачивание самого файла (`download_file`) не заворачивает: оттуда
# таймаут и ошибки соединения aiohttp приходят как есть.
NETWORK_FAILURES = (TelegramNetworkError, TimeoutError, ClientConnectionError, ClientPayloadError)


def network_failure(error: Exception) -> bool:
    """Сбой сети — повторить; ответ Telegram по существу — нет.

    `TelegramEntityTooLarge` наследует `TelegramNetworkError`, но это ответ
    413: файл не пройдёт и со второго раза. `TelegramBadRequest` («file is too
    big») и ответ сервера файлов с кодом ошибки (`ClientResponseError`) — тоже
    не сеть.
    """
    return isinstance(error, NETWORK_FAILURES) and not isinstance(error, TelegramEntityTooLarge)


async def load_file_once(bot: Bot, file_id: str) -> bytes:
    """Одна попытка: путь к файлу у Telegram, затем сам файл — в память.

    Это `bot.download`, только со сроком на оба запроса: у него срок есть лишь
    у скачивания, а `get_file` ждёт таймаута сессии — минуту.
    """
    found = await bot.get_file(file_id, request_timeout=GET_FILE_TIMEOUT)
    if found.file_path is None:
        # Без пути файл не скачать, и повтор не поможет: это не сеть.
        raise ValueError("Telegram returned no file_path")
    buffer = BytesIO()
    await bot.download_file(found.file_path, destination=buffer, timeout=DOWNLOAD_TIMEOUT)
    return buffer.getvalue()


async def load_file(bot: Bot, file_id: str) -> bytes:
    """Файл из Telegram в память; сбой сети — ещё попытка, всего `DOWNLOAD_ATTEMPTS`.

    Каждая неудачная попытка — строка в журнал: номер и тип ошибки. Текста
    ошибки там нет: в адресе файла у Telegram — токен бота. Не сеть или
    попытки кончились — исключение уходит в сервис как есть, и он отвечает
    так же, как без повтора.
    """
    attempt = 1
    while True:
        try:
            return await load_file_once(bot, file_id)
        except Exception as error:
            name = type(error).__name__
            if attempt == DOWNLOAD_ATTEMPTS or not network_failure(error):
                logger.warning(
                    "Файл из Telegram не скачан, попытка %s из %s: %s, без повтора",
                    attempt,
                    DOWNLOAD_ATTEMPTS,
                    name,
                )
                raise
            logger.warning(
                "Файл из Telegram не скачан, попытка %s из %s: %s, повтор через %s с",
                attempt,
                DOWNLOAD_ATTEMPTS,
                name,
                DOWNLOAD_PAUSE,
            )
        await sleep(DOWNLOAD_PAUSE)
        attempt += 1


async def handle_start(message: Message) -> None:
    """Приветствие владельцу."""
    logger.info("Команда /start")
    await message.answer(texts.START)


async def handle_help(message: Message) -> None:
    """Короткий список того, что уже работает."""
    logger.info("Команда /help")
    await message.answer(texts.HELP)


async def handle_chats(message: Message, settings: Settings) -> None:
    """Как подключить чтение чатов Telegram (`techspec/28-relay.md` §28.1):
    напрямую — в «Автоматизации чатов», или через Partner Assistant —
    кнопкой-ссылкой на него, если приём от него включён."""
    logger.info("Команда /chats")
    username = settings.partner_bot_username if settings.relay_enabled else None
    markup = None
    if username is not None:
        markup = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=texts.PARTNER_BUTTON, url=partner_link(username))]
            ]
        )
    await message.answer(texts.chats_help(relay=markup is not None), reply_markup=markup)


def launch_search(searches: SearchService | None, outcome: RecordOutcome) -> None:
    """Запустить поиск, заведённый сообщением, — когда «Ищу» уже ушло
    (`techspec/24-search.md` §24.3): ответ поиска его не обгонит. Обработчик
    поиска не ждёт. Не запустился — его подхватит тик."""
    if outcome.search_id is None:
        return
    if searches is None:
        logger.error("Поиск %s некому запустить: бот собран без поиска", outcome.search_id)
        return
    searches.launch(outcome.search_id)


async def handle_text(
    message: Message, tasks: TaskService | None, searches: SearchService | None = None
) -> None:
    """Текст владельца — поручение: записываем и подтверждаем своими словами.

    Пересланное сообщение с текстом — такой же текст: разбирается как
    поручение, а имя отправителя уходит в разбор отдельным полем. Решение
    принимает слой операций, обработчик только отправляет его ответ.
    Пустой ответ — сообщение пересланной переписки, за которую отвечает
    другое (`techspec/18-forwarded.md` §18.1): отправлять нечего. Сообщение
    завело поиск — он запускается после ответа «Ищу» (§24.3).
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
        sent_at=written_at(message),
        from_owner=forwarded_from_owner(message),
    )
    if outcome.message:
        await message.answer(outcome.message, reply_markup=keyboard(outcome.buttons))
    launch_search(searches, outcome)


async def handle_speech(
    message: Message,
    bot: Bot,
    tasks: TaskService | None,
    speech: Speech,
    searches: SearchService | None = None,
) -> None:
    """Голосовое или кружок владельца — поручение: скачать, расслышать, записать.

    Файл качается в память замыканием над `load_file` (сбой сети — ещё
    попытка), которое уходит в сервис: сервис не знает про aiogram, а байты
    на диск не попадают (`techspec/09-voice.md` §9.2, §9.3). Пока идёт
    распознавание и разбор, в чате висит «печатает…» — это дольше текста, и
    молчание пугает; статус живёт пять секунд, поэтому его повторяет
    `typing_status`, и повисший статус ответ не держит. Пустой ответ —
    голосовое пересланной переписки, за которую отвечает другое сообщение
    (`techspec/18-forwarded.md` §18.1).
    """
    if tasks is None:
        logger.error("Голосовое некуда записать: бот собран без базы")
        await message.answer(texts.NOT_SAVED)
        return

    async def load_audio() -> bytes:
        return await load_file(bot, speech.file_id)

    async with typing_status(bot, message.chat.id):
        outcome = await tasks.record_from_voice(
            chat_id=message.chat.id,
            telegram_message_id=message.message_id,
            kind=speech.kind,
            file_id=speech.file_id,
            duration=speech.duration,
            load_audio=load_audio,
            forwarded_from=forwarded_sender(message),
            swipe=swipe_of(message),
            sent_at=written_at(message),
            from_owner=forwarded_from_owner(message),
        )
    if outcome.message:
        await message.answer(outcome.message, reply_markup=keyboard(outcome.buttons))
    launch_search(searches, outcome)


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
    (инвариант 4). Отказ — всплывающий ответ, кнопка остаётся. Номер дела в
    callback (`techspec/23-several-tasks.md` §23.5) — какое из дел сообщения
    записать; без номера — дело номер 1.
    """
    message = callback.message
    if tasks is None or message is None:
        logger.error("Кнопку «Записать отдельно» некому обработать: бот собран без базы")
        await callback.answer(texts.NOT_SAVED)
        return
    parsed = edits.parse_apart(callback.data or "")
    if parsed is None:
        logger.warning("Кнопка «Записать отдельно» с непонятными данными: %r", callback.data)
        await callback.answer(texts.MESSAGE_UNKNOWN)
        return
    telegram_message_id, item = parsed
    outcome = await tasks.apart(
        chat_id=message.chat.id, telegram_message_id=telegram_message_id, item=item
    )
    await answer_press(callback, outcome)


async def handle_reopen(callback: CallbackQuery, tasks: TaskService | None) -> None:
    """Нажата «Вернуть» под «Закрыл» или «Убрал из списка» (§12.6).

    Задача снова активна и с напоминаниями по сроку — и только после ответа
    базы сообщение меняется на «Вернул в работу». Уже активная задача —
    тот же ответ без записи: второе нажатие безвредно.

    Под ответом о нескольких делах кнопка несёт сообщение владельца, а не
    задачу (`techspec/23-several-tasks.md` §23.5): итог приходит новым
    сообщением.
    """
    if tasks is None:
        logger.error("Кнопку «Вернуть» некому обработать: бот собран без базы")
        await callback.answer(texts.NOT_REOPENED)
        return
    data = callback.data or ""
    task_id = edits.parse_reopen(data)
    if task_id is not None:
        await answer_press(callback, await tasks.reopen(task_id=task_id))
        return
    telegram_message_id = edits.parse_reopen_message(data)
    if telegram_message_id is None or callback.message is None:
        logger.warning("Кнопка «Вернуть» с непонятными данными: %r", callback.data)
        await callback.answer(texts.DONE_UNKNOWN)
        return
    outcome = await tasks.reopen_in_message(
        chat_id=callback.message.chat.id, telegram_message_id=telegram_message_id
    )
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
    data = callback.data or ""
    parsed = edits.parse_back(data)
    if parsed is not None:
        task_id, moved_from, moved_to = parsed
        outcome = await tasks.back(task_id=task_id, back_to=moved_from, moved_from=moved_to)
        await answer_press(callback, outcome)
        return
    # Под ответом о нескольких делах (§23.5) — сообщение владельца вместо задачи.
    in_message = edits.parse_back_message(data)
    if in_message is None or callback.message is None:
        logger.warning("Кнопка «Вернуть» с непонятными данными: %r", callback.data)
        await callback.answer(texts.DONE_UNKNOWN)
        return
    telegram_message_id, moved_from, moved_to = in_message
    outcome = await tasks.back_in_message(
        chat_id=callback.message.chat.id,
        telegram_message_id=telegram_message_id,
        back_to=moved_from,
        moved_from=moved_to,
    )
    await answer_press(callback, outcome)


async def answer_press(callback: CallbackQuery, outcome: PressOutcome) -> None:
    """Ответ на нажатие: сменить сообщение с кнопками или всплыть подсказкой.

    Сообщение не сменилось (старое, уже с этим текстом) — ответ всплывает
    подсказкой: база уже записала, и человек должен об этом узнать.

    Нажатие под ответом о нескольких делах (`follow_up`, §23.5) текст не
    меняет: итог — новым сообщением, с ответа снимаются кнопки нажатого
    вопроса.
    """
    if outcome.follow_up and isinstance(callback.message, Message):
        await callback.message.answer(outcome.message, reply_markup=keyboard(outcome.buttons))
        await drop_pressed(callback.message, callback.data or "")
        await callback.answer()
        return
    if outcome.replace and await replace_text(
        callback.message, outcome.message, keyboard(outcome.buttons)
    ):
        await callback.answer()
        return
    await callback.answer(outcome.message)


async def drop_pressed(message: Message, pressed: str) -> None:
    """Снять с ответа о нескольких делах кнопки нажатого вопроса (§23.5).

    Telegram не дал — кнопки остаются: второе нажатие база не запишет, а
    итог владелец уже получил.
    """
    markup = message.reply_markup
    if markup is None:
        return
    rows = [
        kept
        for row in markup.inline_keyboard
        if (
            kept := [
                button for button in row if edits.keeps_button(button.callback_data or "", pressed)
            ]
        )
    ]
    try:
        await message.edit_reply_markup(
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
        )
    except TelegramBadRequest as error:
        logger.warning("Кнопки нажатого вопроса не сняты: %s", error)


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

    Как у голоса: снимок качается в память тем же `load_file` с повтором —
    замыканием, которое уходит в сервис, — и на диск не попадает
    (`techspec/14-photo.md` §14.2). Снимок разбирается дольше текста — пока
    идёт разбор, в чате висит «печатает…». Свайп у снимка не читается:
    правки у снимка нет (§14.3).
    """
    if tasks is None:
        logger.error("Снимок некуда записать: бот собран без базы")
        await message.answer(texts.NOT_SAVED)
        return

    async def load_image() -> bytes:
        return await load_file(bot, photo.file_id)

    async with typing_status(bot, message.chat.id):
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


# --- Личные чаты (`techspec/25-chats.md`) ------------------------------------

TELEGRAM: Platform = "telegram"


def _chat_content(message: Message) -> tuple[ChatKind, str, str | None]:
    """Вид, текст и файл сообщения чата (§25.2): у голосового и кружка текст
    пуст до расшифровки, у снимка и прочего — подпись, если есть."""
    if message.text is not None:
        return "text", message.text, None
    if message.voice is not None:
        return "voice", "", message.voice.file_id
    if message.video_note is not None:
        return "video_note", "", message.video_note.file_id
    caption = message.caption or ""
    if message.photo:
        return "photo", caption, None
    return "other", caption, None


def chat_message_of(message: Message, owner_telegram_id: int) -> Incoming | None:
    """Сообщение личного чата владельца в поля приёма — или ничего (§25.2).

    Чат — собеседник: в личном бизнес-чате `chat` всегда он, и у сообщения
    владельца тоже. `from.id` владельца — `out`, иначе `in`. Написанное
    другим ботом от имени владельца (`sender_business_bot`) и автоматическое
    (`is_from_offline`: автоответ, приветствие, отложенное) — пропуск: это
    не владелец ответил. Без подключения — не бизнес-сообщение.
    """
    connection = message.business_connection_id
    if connection is None or message.sender_business_bot is not None or message.is_from_offline:
        return None
    author = message.from_user
    out = author is not None and author.id == owner_telegram_id
    kind, text, file_id = _chat_content(message)
    chat_name = message.chat.full_name
    return Incoming(
        platform=TELEGRAM,
        connection_id=connection,
        chat_key=str(message.chat.id),
        chat_name=chat_name,
        external_id=str(message.message_id),
        direction="out" if out else "in",
        sender=author.full_name if author is not None else chat_name,
        sent_at=message.date,
        kind=kind,
        text=text,
        file_id=file_id,
    )


async def handle_business_connection(
    connection: BusinessConnection, chats: ChatService | None = None
) -> None:
    """Бота подключили к аккаунту или отключили (§25.2): владельца — в базу и
    вопрос о согласии, чужое — в журнал. Ответа нет никому."""
    if chats is None:
        logger.error("Подключение некому принять: бот собран без базы")
        return
    await chats.connected(
        platform=TELEGRAM,
        connection_id=connection.id,
        user_id=connection.user.id,
        enabled=connection.is_enabled,
    )


async def handle_business_message(
    message: Message, bot: Bot, settings: Settings, chats: ChatService | None = None
) -> None:
    """Сообщение личного чата владельца (§25.2): в базу, если подключение —
    владельца и есть согласие; голосовое — расшифровать. В чат — ничего."""
    if chats is None:
        logger.error("Сообщение чата некуда записать: бот собран без базы")
        return
    incoming = chat_message_of(message, settings.owner_telegram_id)
    if incoming is None:
        return
    file_id = incoming.file_id

    async def load_audio() -> bytes:
        if file_id is None:
            raise ValueError("no file to load")
        return await load_file(bot, file_id)

    await chats.receive(incoming, load_audio if file_id is not None else None)


async def handle_edited_business_message(
    message: Message, settings: Settings, chats: ChatService | None = None
) -> None:
    """Правка в личном чате (§25.1): до разбора меняет текст."""
    if chats is None:
        return
    incoming = chat_message_of(message, settings.owner_telegram_id)
    if incoming is None:
        return
    await chats.edited(incoming)


async def handle_deleted_business_messages(
    deleted: BusinessMessagesDeleted, chats: ChatService | None = None
) -> None:
    """Удаление в личном чате (§25.1) стирает текст."""
    if chats is None:
        return
    await chats.deleted(
        platform=TELEGRAM,
        connection_id=deleted.business_connection_id,
        chat_key=str(deleted.chat.id),
        external_ids=[str(message_id) for message_id in deleted.message_ids],
    )


async def handle_consent(callback: CallbackQuery, chats: ChatService | None = None) -> None:
    """Нажата «Согласен» или «Не надо» под вопросом о согласии (§25.5).

    Сначала база, потом сообщение: под ним — ответ и кнопка поменять решение.
    """
    parsed = chats_module.parse_consent(callback.data or "")
    if chats is None:
        logger.error("Кнопку согласия некому обработать: бот собран без базы")
        await callback.answer(texts.CONSENT_NOT_SAVED)
        return
    if parsed is None:
        logger.warning("Кнопка согласия с непонятными данными: %r", callback.data)
        await callback.answer(texts.DONE_UNKNOWN)
        return
    platform, agreed = parsed
    await answer_press(callback, await chats.answer_consent(platform, agreed))


async def handle_drop(callback: CallbackQuery, chats: ChatService | None = None) -> None:
    """Нажата «Убрать» под сообщением о разборе переписки (§25.4).

    Сначала база, потом сообщение: у дела пометка «убрано», кнопки остаются
    у остальных дел. Отказ — всплывающий ответ, кнопка остаётся.
    """
    parsed = chats_module.parse_drop(callback.data or "")
    if chats is None:
        logger.error("Кнопку «Убрать» некому обработать: бот собран без базы")
        await callback.answer(texts.NOT_DROPPED)
        return
    if parsed is None:
        logger.warning("Кнопка «Убрать» с непонятными данными: %r", callback.data)
        await callback.answer(texts.DROP_UNKNOWN)
        return
    analysis_id, item = parsed
    await answer_press(callback, await chats.drop(analysis_id, item))


def build_business_router() -> Router:
    """Роутер бизнес-обновлений (§25.2) — новая фабрика на каждую сборку.

    Обновления сюда приходят мимо проверки отправителя (`middlewares.py`):
    собеседник — не владелец, а отбирает их подключение. Обработчики не
    отвечают ничем.
    """
    router = Router(name="business")
    router.business_connection.register(handle_business_connection)
    router.business_message.register(handle_business_message)
    router.edited_business_message.register(handle_edited_business_message)
    router.deleted_business_messages.register(handle_deleted_business_messages)
    return router


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
    router.message.register(handle_chats, Command("chats"))
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
    router.callback_query.register(handle_consent, F.data.startswith(chats_module.CONSENT_PREFIX))
    router.callback_query.register(handle_drop, F.data.startswith(chats_module.DROP_PREFIX))
    return router
