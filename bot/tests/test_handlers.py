"""Бот отвечает владельцу, записывает поручения и не отвечает по существу чужим."""

from __future__ import annotations

import logging
from collections.abc import Callable

import pytest
from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramEntityTooLarge,
    TelegramNetworkError,
    TelegramServerError,
)
from aiogram.methods import GetFile
from aiogram.types import Message, Update
from aiohttp import (
    ClientOSError,
    ClientPayloadError,
    ClientResponseError,
    RequestInfo,
    ServerDisconnectedError,
)
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

from solomon import handlers, texts
from solomon.config import Settings
from solomon.handlers import (
    DOWNLOAD_ATTEMPTS,
    DOWNLOAD_PAUSE,
    DOWNLOAD_TIMEOUT,
    GET_FILE_TIMEOUT,
    PHOTO_LIMIT,
    Photo,
    is_not_text,
    network_failure,
    photo_of,
    refused_image,
)
from solomon.runner import build_dispatcher
from solomon.services.tasks import TaskService
from solomon.services.transcription import NotTranscribed
from solomon.services.understanding import ImageType, PhotoUnderstanding
from tests.conftest import (
    AUDIO,
    IMAGE,
    OWNER_ID,
    SPOKEN,
    STRANGER_ID,
    TEST_TOKEN,
    FakeAnalyst,
    FakeMessages,
    FakePlanner,
    FakeTranscriber,
    FakeUnderstandings,
    RecordingSession,
    make_document_update,
    make_forwarded_update,
    make_photo_understanding,
    make_photo_update,
    make_sticker_update,
    make_understanding,
    make_update,
    make_voice_update,
)

RECORDED = "Записал: купить лампочку в коридор"


def build_tasks(
    settings: Settings,
    title: str = "купить лампочку в коридор",
    messages: FakeMessages | None = None,
    transcriber: FakeTranscriber | None = None,
    photo: PhotoUnderstanding | None = None,
) -> tuple[TaskService, FakeMessages, FakeAnalyst]:
    """Приём поручений на подменённых базе, модели и распознавании."""
    record_message = messages or FakeMessages()
    analyst = FakeAnalyst(make_understanding(title=title), photo=photo)
    service = TaskService(
        settings=settings,
        record_message=record_message,
        record_understanding=FakeUnderstandings(),
        analyst=analyst,
        transcriber=transcriber or FakeTranscriber(),
        planner=FakePlanner(),
    )
    return service, record_message, analyst


async def test_start_answers_in_russian(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    dispatcher = build_dispatcher(settings)

    await dispatcher.feed_update(bot, make_update("/start"))

    assert session.texts == [texts.START]
    assert "Здравствуйте" in session.texts[0]


async def test_help_lists_working_commands(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    dispatcher = build_dispatcher(settings)

    await dispatcher.feed_update(bot, make_update("/help"))

    assert session.texts == [texts.HELP]
    assert "/start" in session.texts[0]
    # Правка из приложения (`techspec/11-edit.md`): /help о ней знает.
    assert "задачу можно закрыть, изменить или удалить" in session.texts[0]


async def test_stranger_is_turned_away(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    dispatcher = build_dispatcher(settings)

    await dispatcher.feed_update(bot, make_update("/start", from_id=STRANGER_ID))

    assert session.texts == [texts.STRANGER]


async def test_text_is_recorded_and_confirmed(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    service, messages, _ = build_tasks(settings)
    dispatcher = build_dispatcher(settings, tasks=service)

    await dispatcher.feed_update(bot, make_update("купить лампочку в коридор", update_id=5))

    assert session.texts == [RECORDED]
    assert messages.calls == [
        {
            "owner_telegram_id": OWNER_ID,
            "chat_id": OWNER_ID,
            "telegram_message_id": 5,
            "text": "купить лампочку в коридор",
            "kind": "text",
            "telegram_file_id": None,
            "duration_seconds": None,
        }
    ]


async def test_help_mentions_voice_and_the_about_me_tab(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    dispatcher = build_dispatcher(settings)

    await dispatcher.feed_update(bot, make_update("/help"))

    assert "олосов" in session.texts[0]
    assert "О себе" in session.texts[0]


async def test_voice_is_heard_and_recorded_as_a_task(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    transcriber = FakeTranscriber()
    service, messages, analyst = build_tasks(
        settings, title="отправить расчёт клиенту", transcriber=transcriber
    )
    dispatcher = build_dispatcher(settings, tasks=service)

    await dispatcher.feed_update(bot, make_voice_update(update_id=6, duration=32))

    assert session.texts == ["Записал: отправить расчёт клиенту"]
    saved = messages.calls[0]
    assert saved["kind"] == "voice"
    assert saved["telegram_file_id"] == "voice-1"
    assert saved["duration_seconds"] == 32
    assert saved["text"] == ""
    # Файл скачан в память и ушёл в распознавание как есть.
    assert transcriber.calls == [AUDIO]
    assert analyst.calls == [(SPOKEN, None, "fine")]
    # Пока бот слушал, в чате висело «печатает…».
    assert "typing" in session.actions


async def test_video_note_is_heard_the_same_way(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    service, messages, _ = build_tasks(settings, title="отправить расчёт клиенту")
    dispatcher = build_dispatcher(settings, tasks=service)

    await dispatcher.feed_update(
        bot, make_voice_update(update_id=7, kind="video_note", duration=15)
    )

    assert session.texts == ["Записал: отправить расчёт клиенту"]
    assert messages.calls[0]["kind"] == "video_note"
    assert messages.calls[0]["telegram_file_id"] == "note-1"
    assert messages.calls[0]["duration_seconds"] == 15


async def test_forwarded_voice_names_the_sender(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    service, _, analyst = build_tasks(settings, title="принять смету от Ани")
    dispatcher = build_dispatcher(settings, tasks=service)

    await dispatcher.feed_update(bot, make_voice_update(update_id=8, sender="Аня"))

    assert session.texts == ["Записал: принять смету от Ани"]
    assert analyst.calls == [(SPOKEN, "Аня", "fine")]


async def test_not_heard_voice_answers_honestly(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    transcriber = FakeTranscriber(NotTranscribed(reason="пустая расшифровка"))
    service, messages, analyst = build_tasks(settings, transcriber=transcriber)
    dispatcher = build_dispatcher(settings, tasks=service)

    await dispatcher.feed_update(bot, make_voice_update(update_id=9))

    assert session.texts == [texts.NOT_HEARD]
    assert messages.calls[0]["telegram_file_id"] == "voice-1"
    assert analyst.calls == []


async def test_voice_without_database_says_nothing_was_saved(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    dispatcher = build_dispatcher(settings)

    await dispatcher.feed_update(bot, make_voice_update(update_id=10))

    assert session.texts == [texts.NOT_SAVED]


# --- Снимки (`techspec/14-photo.md` §14.1) -----------------------------------

MB = 1024 * 1024


def message_of(update: Update) -> Message:
    assert update.message is not None
    return update.message


def test_photo_limit_is_three_and_a_half_megabytes() -> None:
    assert PHOTO_LIMIT == 3_670_016


def test_photo_takes_the_largest_size_within_the_limit() -> None:
    """Telegram шлёт размеры по возрастанию; самый крупный больше предела."""
    sizes = ((90, 68, 1_500), (1280, 960, 1 * MB), (2560, 1920, 4 * MB))
    message = message_of(make_photo_update(sizes=sizes, caption="купить такие же"))

    assert photo_of(message) == Photo("photo-1280", "image/jpeg", "купить такие же")
    assert not refused_image(message)
    assert not is_not_text(message)


def test_photo_size_at_the_limit_fits() -> None:
    message = message_of(make_photo_update(sizes=((90, 68, 1_500), (2560, 1920, PHOTO_LIMIT))))

    photo = photo_of(message)

    assert photo is not None
    assert photo.file_id == "photo-2560"


def test_photo_size_without_file_size_fits() -> None:
    message = message_of(make_photo_update(sizes=((90, 68, 1_500), (1280, 960, None))))

    photo = photo_of(message)

    assert photo == Photo("photo-1280", "image/jpeg", "")


def test_photo_with_no_size_within_the_limit_is_refused() -> None:
    """Решение (а) плана: ни один размер не влез — отказ «не открою», без записи."""
    message = message_of(make_photo_update(sizes=((2560, 1920, 4 * MB),)))

    assert photo_of(message) is None
    assert refused_image(message)
    assert not is_not_text(message)


@pytest.mark.parametrize("mime_type", ["image/jpeg", "image/png", "image/webp"])
def test_image_file_of_a_known_type_is_a_photo(mime_type: ImageType) -> None:
    message = message_of(make_document_update(mime_type, caption="это счёт"))

    assert photo_of(message) == Photo("doc-1", mime_type, "это счёт")
    assert not refused_image(message)


def test_image_file_without_a_size_is_a_photo() -> None:
    message = message_of(make_document_update("image/png", file_size=None))

    assert photo_of(message) == Photo("doc-1", "image/png", "")


@pytest.mark.parametrize(
    ("mime_type", "file_size"),
    [
        ("image/heic", 900_000),
        ("image/gif", 900_000),
        ("image/tiff", 900_000),
        ("image/svg+xml", 9_000),
        ("image/jpeg", PHOTO_LIMIT + 1),
    ],
)
def test_image_file_of_another_type_or_too_big_is_refused(mime_type: str, file_size: int) -> None:
    message = message_of(make_document_update(mime_type, file_size=file_size))

    assert photo_of(message) is None
    assert refused_image(message)
    assert not is_not_text(message)


@pytest.mark.parametrize(
    "update",
    [
        make_document_update("image/gif", animation=True),
        make_document_update("video/mp4", animation=True),
        make_document_update("application/pdf"),
        make_document_update(None),
        make_sticker_update(),
    ],
    ids=["gif-animation", "mp4-animation", "pdf", "no-mime", "sticker"],
)
def test_animation_sticker_and_other_files_are_not_photos(update: Update) -> None:
    message = message_of(update)

    assert photo_of(message) is None
    assert not refused_image(message)
    assert is_not_text(message)


async def test_photo_is_saved_downloaded_and_recorded(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    session.file_bytes = IMAGE
    photo = make_photo_understanding(title="сходить на родительское собрание")
    service, messages, analyst = build_tasks(settings, photo=photo)
    dispatcher = build_dispatcher(settings, tasks=service)

    sizes = ((90, 68, 1_500), (1280, 960, 180_000))
    await dispatcher.feed_update(
        bot, make_photo_update(update_id=11, sizes=sizes, caption="не забыть")
    )

    assert session.texts == ["Записал: сходить на родительское собрание"]
    saved = messages.calls[0]
    assert saved["kind"] == "photo"
    assert saved["telegram_file_id"] == "photo-1280"
    assert saved["text"] == "не забыть"
    assert saved["duration_seconds"] is None
    # Снимок скачан в память и ушёл модели как есть, вместе с подписью.
    assert analyst.photos == [(IMAGE, "image/jpeg", "не забыть", None)]
    assert analyst.calls == []
    # Пока модель смотрела, в чате висело «печатает…».
    assert "typing" in session.actions


async def test_image_file_goes_with_its_own_type(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    service, messages, analyst = build_tasks(settings, photo=make_photo_understanding())
    dispatcher = build_dispatcher(settings, tasks=service)

    await dispatcher.feed_update(bot, make_document_update("image/png", update_id=12))

    assert messages.calls[0]["kind"] == "photo"
    assert messages.calls[0]["telegram_file_id"] == "doc-1"
    assert messages.calls[0]["text"] == ""
    assert [media_type for _, media_type, _, _ in analyst.photos] == ["image/png"]


async def test_forwarded_photo_names_the_sender(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    service, _, analyst = build_tasks(settings, photo=make_photo_understanding())
    dispatcher = build_dispatcher(settings, tasks=service)

    await dispatcher.feed_update(bot, make_photo_update(update_id=13, sender="Аня"))

    assert analyst.photos[0][3] == "Аня"


async def test_refused_image_says_so_and_nothing_is_saved(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    service, messages, analyst = build_tasks(settings, photo=make_photo_understanding())
    dispatcher = build_dispatcher(settings, tasks=service)

    await dispatcher.feed_update(bot, make_document_update("image/heic", update_id=14))

    assert session.texts == ["Этот файл не открою — пришлите снимок как фото, а не файлом."]
    assert session.texts == [texts.FILE_REFUSED]
    assert messages.calls == []
    assert analyst.photos == []


@pytest.mark.parametrize(
    "update",
    [make_document_update("image/gif", update_id=15, animation=True), make_sticker_update(16)],
    ids=["gif-animation", "sticker"],
)
async def test_not_a_photo_is_refused_and_nothing_is_saved(
    bot: Bot, session: RecordingSession, settings: Settings, update: Update
) -> None:
    service, messages, analyst = build_tasks(settings, photo=make_photo_understanding())
    dispatcher = build_dispatcher(settings, tasks=service)

    await dispatcher.feed_update(bot, update)

    assert session.texts == [
        "Понимаю текст, голос и фото. Файлы, стикеры и видео пока не разбираю — "
        "напишите или надиктуйте."
    ]
    assert session.texts == [texts.NOT_TEXT]
    assert messages.calls == []
    assert analyst.calls == []
    assert analyst.photos == []


async def test_photo_without_database_says_nothing_was_saved(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    dispatcher = build_dispatcher(settings)

    await dispatcher.feed_update(bot, make_photo_update(update_id=17))

    assert session.texts == [texts.NOT_SAVED]


async def test_help_tells_about_photos(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    dispatcher = build_dispatcher(settings)

    await dispatcher.feed_update(bot, make_update("/help"))

    assert "Фотографии и файлы пока не понимаю" not in session.texts[0]
    assert "Фото и скриншоты" in session.texts[0]


# --- Скачивание с повтором (`techspec/09-voice.md` §9.3) ---------------------

# Адрес файла у Telegram: в нём токен бота, и в журнал он попадать не должен.
FILE_URL = f"https://api.telegram.org/file/bot{TEST_TOKEN}/voice/voice-1.oga"


def timed_out(file_id: str = "voice-1") -> TelegramNetworkError:
    """Повисший `get_file`: так aiogram заворачивает таймаут запроса к API."""
    return TelegramNetworkError(method=GetFile(file_id=file_id), message="Request timeout error")


def refused(message: str) -> TelegramBadRequest:
    """Отказ Telegram по существу: ответ 400 на `get_file`."""
    return TelegramBadRequest(method=GetFile(file_id="voice-1"), message=message)


def attempt_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Строки журнала о неудачных попытках — от обработчиков, по порядку."""
    return [record.getMessage() for record in caplog.records if record.name == "solomon.handlers"]


@pytest.fixture
def pauses(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Паузы между попытками: записываются, а не выжидаются."""
    taken: list[float] = []

    async def pause(seconds: float) -> None:
        taken.append(seconds)

    monkeypatch.setattr(handlers, "sleep", pause)
    return taken


async def test_voice_is_heard_after_a_network_failure(
    bot: Bot,
    session: RecordingSession,
    settings: Settings,
    pauses: list[float],
    caplog: pytest.LogCaptureFixture,
) -> None:
    session.get_file_failures = [timed_out()]
    transcriber = FakeTranscriber()
    service, messages, analyst = build_tasks(
        settings, title="отправить расчёт клиенту", transcriber=transcriber
    )
    dispatcher = build_dispatcher(settings, tasks=service)

    with caplog.at_level(logging.WARNING, logger="solomon.handlers"):
        await dispatcher.feed_update(bot, make_voice_update(update_id=21))

    # Вторая попытка прошла — дальше всё как обычно.
    assert session.texts == ["Записал: отправить расчёт клиенту"]
    assert messages.calls[0]["telegram_file_id"] == "voice-1"
    assert transcriber.calls == [AUDIO]
    assert analyst.calls == [(SPOKEN, None, "fine")]
    assert session.file_requests == ["voice-1", "voice-1"]
    assert pauses == [DOWNLOAD_PAUSE]
    assert attempt_lines(caplog) == [
        "Файл из Telegram не скачан, попытка 1 из 3: TelegramNetworkError, повтор через 1 с",
    ]


async def test_voice_is_not_heard_when_every_attempt_fails(
    bot: Bot,
    session: RecordingSession,
    settings: Settings,
    pauses: list[float],
    caplog: pytest.LogCaptureFixture,
) -> None:
    session.get_file_failures = [timed_out() for _ in range(DOWNLOAD_ATTEMPTS)]
    transcriber = FakeTranscriber()
    service, messages, analyst = build_tasks(settings, transcriber=transcriber)
    dispatcher = build_dispatcher(settings, tasks=service)

    with caplog.at_level(logging.WARNING, logger="solomon.handlers"):
        await dispatcher.feed_update(bot, make_voice_update(update_id=22))

    # Как без повтора: сообщение с файлом в базе, честный ответ, без распознавания.
    assert session.texts == [texts.NOT_HEARD]
    assert messages.calls[0]["telegram_file_id"] == "voice-1"
    assert transcriber.calls == []
    assert analyst.calls == []
    assert session.file_requests == ["voice-1"] * DOWNLOAD_ATTEMPTS
    assert pauses == [DOWNLOAD_PAUSE] * (DOWNLOAD_ATTEMPTS - 1)
    assert attempt_lines(caplog) == [
        "Файл из Telegram не скачан, попытка 1 из 3: TelegramNetworkError, повтор через 1 с",
        "Файл из Telegram не скачан, попытка 2 из 3: TelegramNetworkError, повтор через 1 с",
        "Файл из Telegram не скачан, попытка 3 из 3: TelegramNetworkError, без повтора",
    ]


async def test_refusal_of_telegram_is_not_retried(
    bot: Bot,
    session: RecordingSession,
    settings: Settings,
    pauses: list[float],
    caplog: pytest.LogCaptureFixture,
) -> None:
    session.get_file_failures = [refused("Bad Request: file is too big")]
    transcriber = FakeTranscriber()
    service, _, _ = build_tasks(settings, transcriber=transcriber)
    dispatcher = build_dispatcher(settings, tasks=service)

    with caplog.at_level(logging.WARNING, logger="solomon.handlers"):
        await dispatcher.feed_update(bot, make_voice_update(update_id=23))

    assert session.texts == [texts.NOT_HEARD]
    assert transcriber.calls == []
    assert session.file_requests == ["voice-1"]
    assert pauses == []
    assert attempt_lines(caplog) == [
        "Файл из Telegram не скачан, попытка 1 из 3: TelegramBadRequest, без повтора",
    ]


async def test_each_attempt_has_its_own_short_timeouts(
    bot: Bot, session: RecordingSession, settings: Settings, pauses: list[float]
) -> None:
    session.content_failures = [TimeoutError()]
    service, _, _ = build_tasks(settings)
    dispatcher = build_dispatcher(settings, tasks=service)

    await dispatcher.feed_update(bot, make_voice_update(update_id=24))

    # Срок есть и у пути к файлу, и у самого файла — в каждой попытке; таймаут
    # сессии в минуту больше не ждётся.
    assert session.get_file_timeouts == [GET_FILE_TIMEOUT, GET_FILE_TIMEOUT]
    assert session.content_timeouts == [DOWNLOAD_TIMEOUT, DOWNLOAD_TIMEOUT]


def test_worst_case_is_not_longer_than_the_old_minute() -> None:
    # Прежний отказ — 60 с: столько ждал один `get_file`. Срок длиннее 5 с
    # aiohttp округляет вверх до целой секунды — отсюда секунда запаса.
    attempt = GET_FILE_TIMEOUT + DOWNLOAD_TIMEOUT + 1
    assert DOWNLOAD_ATTEMPTS * attempt + (DOWNLOAD_ATTEMPTS - 1) * DOWNLOAD_PAUSE <= 60


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError(),
        ClientOSError(104, "Connection reset by peer"),
        ServerDisconnectedError(),
        ClientPayloadError("Response payload is not completed"),
    ],
    ids=["timeout", "reset", "disconnected", "cut-short"],
)
async def test_failure_while_downloading_the_file_is_retried(
    failure: Exception,
    bot: Bot,
    session: RecordingSession,
    settings: Settings,
    pauses: list[float],
) -> None:
    # Скачивание самого файла aiogram в `TelegramNetworkError` не заворачивает.
    session.content_failures = [failure]
    transcriber = FakeTranscriber()
    service, _, _ = build_tasks(settings, title="отправить расчёт клиенту", transcriber=transcriber)
    dispatcher = build_dispatcher(settings, tasks=service)

    await dispatcher.feed_update(bot, make_voice_update(update_id=25))

    assert session.texts == ["Записал: отправить расчёт клиенту"]
    assert transcriber.calls == [AUDIO]
    # Новая попытка начинается сначала — с пути к файлу.
    assert session.file_requests == ["voice-1", "voice-1"]
    assert pauses == [DOWNLOAD_PAUSE]


async def test_attempt_line_has_no_error_text(
    bot: Bot,
    session: RecordingSession,
    settings: Settings,
    pauses: list[float],
    caplog: pytest.LogCaptureFixture,
) -> None:
    session.content_failures = [
        ClientPayloadError(f"Response payload is not completed: {FILE_URL}")
    ]
    service, _, _ = build_tasks(settings)
    dispatcher = build_dispatcher(settings, tasks=service)

    with caplog.at_level(logging.WARNING, logger="solomon.handlers"):
        await dispatcher.feed_update(bot, make_voice_update(update_id=26))

    assert attempt_lines(caplog) == [
        "Файл из Telegram не скачан, попытка 1 из 3: ClientPayloadError, повтор через 1 с",
    ]
    assert TEST_TOKEN not in caplog.text


async def test_photo_is_downloaded_with_the_same_retry(
    bot: Bot, session: RecordingSession, settings: Settings, pauses: list[float]
) -> None:
    session.file_bytes = IMAGE
    session.get_file_failures = [timed_out("photo-90")]
    photo = make_photo_understanding(title="сходить на родительское собрание")
    service, _, analyst = build_tasks(settings, photo=photo)
    dispatcher = build_dispatcher(settings, tasks=service)

    await dispatcher.feed_update(bot, make_photo_update(update_id=27))

    assert session.texts == ["Записал: сходить на родительское собрание"]
    assert analyst.photos == [(IMAGE, "image/jpeg", "", None)]
    assert session.file_requests == ["photo-90", "photo-90"]
    assert pauses == [DOWNLOAD_PAUSE]


async def test_photo_is_not_opened_when_every_attempt_fails(
    bot: Bot, session: RecordingSession, settings: Settings, pauses: list[float]
) -> None:
    session.get_file_failures = [timed_out("photo-90") for _ in range(DOWNLOAD_ATTEMPTS)]
    service, messages, analyst = build_tasks(settings, photo=make_photo_understanding())
    dispatcher = build_dispatcher(settings, tasks=service)

    await dispatcher.feed_update(bot, make_photo_update(update_id=28))

    assert session.texts == [texts.PHOTO_NOT_OPENED]
    assert messages.calls[0]["telegram_file_id"] == "photo-90"
    assert analyst.photos == []
    assert session.file_requests == ["photo-90"] * DOWNLOAD_ATTEMPTS


def http_error(status: int) -> ClientResponseError:
    """Сервер файлов ответил кодом ошибки: так aiohttp сообщает о нём при скачивании."""
    info = RequestInfo(URL(FILE_URL), "GET", CIMultiDictProxy(CIMultiDict[str]()))
    return ClientResponseError(info, (), status=status, message="Not Found")


@pytest.mark.parametrize(
    ("error", "retried"),
    [
        (timed_out(), True),
        (TimeoutError(), True),
        (ClientOSError(104, "Connection reset by peer"), True),
        (ServerDisconnectedError(), True),
        (ClientPayloadError("Response payload is not completed"), True),
        (TelegramEntityTooLarge(method=GetFile(file_id="voice-1"), message="Too Large"), False),
        (refused("Bad Request: file is too big"), False),
        (TelegramServerError(method=GetFile(file_id="voice-1"), message="Bad Gateway"), False),
        (http_error(404), False),
        (ValueError("Telegram returned no file_path"), False),
    ],
)
def test_only_network_failures_are_retried(error: Exception, retried: bool) -> None:
    assert network_failure(error) is retried


@pytest.mark.parametrize(
    ("make", "reply"),
    [(make_voice_update, texts.NOT_HEARD), (make_photo_update, texts.PHOTO_NOT_OPENED)],
)
async def test_download_refusal_leaves_no_token_in_the_log(
    make: Callable[..., Update],
    reply: str,
    bot: Bot,
    session: RecordingSession,
    settings: Settings,
    pauses: list[float],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Сервер файлов ответил 404: в тексте ошибки aiohttp — адрес файла с токеном.

    Ни строка обработчика, ни строка сервиса этот текст в журнал не пишут —
    только тип ошибки.
    """
    session.content_failures = [http_error(404)]
    service, _, _ = build_tasks(settings, photo=make_photo_understanding())
    dispatcher = build_dispatcher(settings, tasks=service)

    with caplog.at_level(logging.INFO):
        await dispatcher.feed_update(bot, make(update_id=29))

    assert session.texts == [reply]
    assert "ClientResponseError" in caplog.text
    assert TEST_TOKEN not in caplog.text


async def test_broken_database_is_not_called_recorded(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    service, _, _ = build_tasks(settings, messages=FakeMessages(broken=True))
    dispatcher = build_dispatcher(settings, tasks=service)

    await dispatcher.feed_update(bot, make_update("купить лампочку", update_id=7))

    # Инвариант 4: о записи сообщается только после ответа базы.
    assert session.texts == [texts.NOT_SAVED]
    assert "Записал" not in session.texts[0]


async def test_repeated_update_is_confirmed_twice(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    service, _, analyst = build_tasks(settings)
    dispatcher = build_dispatcher(settings, tasks=service)

    await dispatcher.feed_update(bot, make_update("купить лампочку в коридор", update_id=8))
    await dispatcher.feed_update(bot, make_update("купить лампочку в коридор", update_id=8))

    # Человеку отвечаем оба раза: первый ответ он мог не увидеть.
    assert session.texts == [RECORDED, RECORDED]
    assert len(analyst.calls) == 2


async def test_owner_id_is_the_only_gate(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    service, _, _ = build_tasks(settings, title="привет")
    dispatcher = build_dispatcher(settings, tasks=service)

    await dispatcher.feed_update(bot, make_update("привет", from_id=OWNER_ID, update_id=2))
    await dispatcher.feed_update(bot, make_update("привет", from_id=STRANGER_ID, update_id=3))

    # Владельцу текст записывается, чужому уходит короткий отказ, и дальше
    # обновление не идёт — до слоя данных оно не доходит.
    assert session.texts == ["Записал: привет", texts.STRANGER]


async def test_bot_without_database_says_nothing_was_saved(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    dispatcher = build_dispatcher(settings)

    await dispatcher.feed_update(bot, make_update("купить лампочку", update_id=9))

    assert session.texts == [texts.NOT_SAVED]


async def test_forwarded_message_is_an_errand_with_a_named_sender(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    service, messages, analyst = build_tasks(settings, title="принять смету от Ани")
    dispatcher = build_dispatcher(settings, tasks=service)

    await dispatcher.feed_update(
        bot, make_forwarded_update("пришлю смету завтра", sender="Аня", update_id=10)
    )

    # Пересланное с текстом — обычное поручение, а имя отправителя уходит
    # в разбор отдельно: чьё это обещание (`spec.md` §3.3).
    assert session.texts == ["Записал: принять смету от Ани"]
    assert messages.calls[0]["text"] == "пришлю смету завтра"
    assert analyst.calls == [("пришлю смету завтра", "Аня", None)]
