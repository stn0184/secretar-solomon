"""Бот отвечает владельцу, записывает поручения и не отвечает по существу чужим."""

from __future__ import annotations

from aiogram import Bot

from solomon import texts
from solomon.config import Settings
from solomon.runner import build_dispatcher
from solomon.services.tasks import TaskService
from solomon.services.transcription import NotTranscribed
from tests.conftest import (
    AUDIO,
    OWNER_ID,
    SPOKEN,
    STRANGER_ID,
    FakeAnalyst,
    FakeMessages,
    FakePlanner,
    FakeTranscriber,
    FakeUnderstandings,
    RecordingSession,
    make_forwarded_update,
    make_photo_update,
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
) -> tuple[TaskService, FakeMessages, FakeAnalyst]:
    """Приём поручений на подменённых базе, модели и распознавании."""
    record_message = messages or FakeMessages()
    analyst = FakeAnalyst(make_understanding(title=title))
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


async def test_photo_is_refused_and_nothing_is_saved(
    bot: Bot, session: RecordingSession, settings: Settings
) -> None:
    service, messages, analyst = build_tasks(settings)
    dispatcher = build_dispatcher(settings, tasks=service)

    await dispatcher.feed_update(bot, make_photo_update(update_id=11))

    assert session.texts == [texts.NOT_TEXT]
    assert "текст и голос" in session.texts[0]
    assert messages.calls == []
    assert analyst.calls == []


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
