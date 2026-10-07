"""Личные чаты (`techspec/25-chats.md`): база, приём, согласие, разбор,
сообщение владельцу, «ждёт ответа» и шаг тика.

Сети нет: клиент Supabase, модель, Deepgram и Telegram подменены. Переписки
в примерах выдуманные — Игорь и Олег.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import SendMessage
from aiogram.types import (
    BusinessConnection,
    BusinessMessagesDeleted,
    Chat,
    InlineKeyboardMarkup,
    Message,
    PhotoSize,
    Sticker,
    Update,
    User,
    VideoNote,
    Voice,
)
from supabase import Client

from solomon import handlers, texts
from solomon.db import chats as db_chats
from solomon.db.chats import (
    ChatMessage,
    ChatReport,
    ChatSource,
    ChatToAnalyze,
    ChatTrace,
    ReportLine,
    Stored,
    WaitingChat,
)
from solomon.db.rpc import DatabaseError
from solomon.runner import build_dispatcher
from solomon.services import chats
from solomon.services.chats import ChatService, Connection, Incoming, OwnerSender
from solomon.services.tasks import Button
from solomon.services.transcription import NotTranscribed, Transcript
from tests.conftest import (
    OWNER_ID,
    OWNER_TIMEZONE,
    FakeTranscriber,
    RecordingSession,
    make_callback_update,
    make_settings,
)
from tests.test_reminders import FakeRpcClient
from tests.test_tasks_db import FakeClient, as_client

TZ = ZoneInfo(OWNER_TIMEZONE)
THREAD_ID = "7c2d9e10-4b5a-4f6e-8d7c-1a2b3c4d5e6f"
ANALYSIS_ID = "3e4f5a6b-7c8d-4e9f-a0b1-c2d3e4f5a6b7"
MESSAGE_ID = "9a71c2d4-1e5f-4a6b-8c7d-0e1f2a3b4c5d"
TASK_ID = "5b0c7a52-8f3e-4c1d-9a6b-2e4f1d3c8b90"
CONNECTION = "biz-777"
IGOR_CHAT = "1001"
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=TZ)


def rpc(answers: dict[str, object]) -> FakeRpcClient:
    return FakeRpcClient(answers)


def source_row(**changes: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": "s1",
        "owner_telegram_id": OWNER_ID,
        "platform": "telegram",
        "connection_id": CONNECTION,
        "is_enabled": True,
        "asked_at": None,
        "consented_at": None,
        "declined_at": None,
        "created_at": "2026-10-07T07:00:00+00:00",
    }
    row.update(changes)
    return row


def message_row(**changes: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": MESSAGE_ID,
        "direction": "in",
        "sender": "Игорь Петров",
        "sent_at": "2026-10-07T06:00:00+00:00",
        "kind": "text",
        "text": "Пришлёшь расчёт?",
        "erased_at": None,
    }
    row.update(changes)
    return row


def report_row(**changes: object) -> dict[str, object]:
    row: dict[str, object] = {
        "platform": "telegram",
        "chat_name": "Игорь Петров",
        "chat_with": "Игорем",
        "item": 1,
        "task_id": TASK_ID,
        "title": "прислать Игорю расчёт",
        "due_at": "2026-10-09T13:00:00+00:00",
        "due_precision": "day",
        "promise": "mine",
        "status": "active",
    }
    row.update(changes)
    return row


# --------------------------------------------------------- база (§3.11–3.15)


async def test_connect_goes_with_the_owner_and_reads_the_source() -> None:
    client = rpc({"connect_chat_source": source_row()})

    source = await db_chats.connect_chat_source(
        cast(Client, client),
        owner_telegram_id=OWNER_ID,
        platform="telegram",
        connection_id=CONNECTION,
        is_enabled=True,
    )

    assert source == ChatSource(
        platform="telegram",
        connection_id=CONNECTION,
        is_enabled=True,
        asked_at=None,
        consented_at=None,
        declined_at=None,
    )
    assert client.params == [
        {
            "owner_telegram_id": OWNER_ID,
            "platform": "telegram",
            "connection_id": CONNECTION,
            "is_enabled": True,
        }
    ]


async def test_connect_without_a_row_is_a_refusal() -> None:
    client = rpc({"connect_chat_source": {"platform": None}})

    with pytest.raises(DatabaseError):
        await db_chats.connect_chat_source(
            cast(Client, client),
            owner_telegram_id=OWNER_ID,
            platform="telegram",
            connection_id=CONNECTION,
            is_enabled=True,
        )


async def test_answer_consent_reads_the_decision_and_an_empty_row_is_none() -> None:
    agreed = rpc({"answer_consent": source_row(consented_at="2026-10-07T07:01:00+00:00")})
    missing = rpc({"answer_consent": source_row(platform=None)})

    source = await db_chats.answer_consent(
        cast(Client, agreed), owner_telegram_id=OWNER_ID, platform="telegram", agreed=True
    )
    none = await db_chats.answer_consent(
        cast(Client, missing), owner_telegram_id=OWNER_ID, platform="telegram", agreed=False
    )

    assert source is not None and source.consented_at is not None
    assert none is None
    assert agreed.params == [
        {"owner_telegram_id": OWNER_ID, "platform": "telegram", "agreed": True}
    ]


async def test_sources_to_ask_filter_the_owner_and_the_undecided() -> None:
    fake = FakeClient(tables={"chat_sources": [source_row()]})

    sources = await db_chats.sources_to_ask(as_client(fake), owner_telegram_id=OWNER_ID)

    assert [source.platform for source in sources] == ["telegram"]
    assert ("eq", "owner_telegram_id", OWNER_ID) in fake.calls
    assert ("eq", "is_enabled", True) in fake.calls
    for column in ("asked_at", "consented_at", "declined_at"):
        assert ("is", column, None) in fake.calls


async def test_store_sends_every_field_and_reads_the_outcome() -> None:
    client = rpc({"store_chat_message": [{"outcome": "stored", "message_id": MESSAGE_ID}]})

    stored = await db_chats.store_chat_message(
        cast(Client, client),
        owner_telegram_id=OWNER_ID,
        platform="telegram",
        connection_id=CONNECTION,
        chat_key=IGOR_CHAT,
        chat_name="Игорь Петров",
        external_id="42",
        direction="in",
        sender="Игорь Петров",
        sent_at=NOW,
        kind="text",
        text="Пришлёшь расчёт?",
    )

    assert stored == Stored(outcome="stored", message_id=MESSAGE_ID)
    assert client.params == [
        {
            "owner_telegram_id": OWNER_ID,
            "platform": "telegram",
            "connection_id": CONNECTION,
            "chat_key": IGOR_CHAT,
            "chat_name": "Игорь Петров",
            "external_id": "42",
            "direction": "in",
            "sender": "Игорь Петров",
            "sent_at": NOW.isoformat(),
            "kind": "text",
            "message_text": "Пришлёшь расчёт?",
            "tracks_waiting": True,
        }
    ]


@pytest.mark.parametrize(
    "answer",
    [
        None,
        [],
        [{"outcome": "lost", "message_id": None}],
        [{"outcome": "stored", "message_id": None}],
        "stored",
    ],
)
async def test_store_with_an_unclear_answer_is_a_refusal(answer: object) -> None:
    client = rpc({"store_chat_message": answer})

    with pytest.raises(DatabaseError):
        await db_chats.store_chat_message(
            cast(Client, client),
            owner_telegram_id=OWNER_ID,
            platform="telegram",
            connection_id=CONNECTION,
            chat_key=IGOR_CHAT,
            chat_name="Игорь",
            external_id="42",
            direction="in",
            sender="Игорь",
            sent_at=NOW,
            kind="text",
            text="т",
        )


async def test_store_without_consent_has_no_message() -> None:
    client = rpc({"store_chat_message": [{"outcome": "no_consent", "message_id": None}]})

    stored = await db_chats.store_chat_message(
        cast(Client, client),
        owner_telegram_id=OWNER_ID,
        platform="telegram",
        connection_id=CONNECTION,
        chat_key=IGOR_CHAT,
        chat_name="Игорь",
        external_id="42",
        direction="in",
        sender="Игорь",
        sent_at=NOW,
        kind="text",
        text="т",
    )

    assert stored == Stored(outcome="no_consent", message_id=None)


async def test_new_messages_read_the_unanalyzed_of_the_owner_in_order() -> None:
    fake = FakeClient(tables={"chat_messages": [message_row(), message_row(id="m2", kind="voice")]})

    found = await db_chats.new_chat_messages(
        as_client(fake), owner_telegram_id=OWNER_ID, thread_id=THREAD_ID, limit=50
    )

    assert found[0] == ChatMessage(
        id=MESSAGE_ID,
        direction="in",
        sender="Игорь Петров",
        sent_at=datetime(2026, 10, 7, 11, 0, tzinfo=TZ),
        kind="text",
        text="Пришлёшь расчёт?",
        erased=False,
    )
    assert found[1].kind == "voice"
    assert ("eq", "owner_telegram_id", OWNER_ID) in fake.calls
    assert ("eq", "thread_id", THREAD_ID) in fake.calls
    assert ("is", "analysis_id", None) in fake.calls
    assert ("limit", 50) in fake.calls


async def test_earlier_messages_are_analyzed_kept_and_chronological() -> None:
    fake = FakeClient(
        tables={
            "chat_messages": [
                message_row(id="late", sent_at="2026-10-07T06:00:00+00:00"),
                message_row(id="early", sent_at="2026-10-06T06:00:00+00:00"),
            ]
        }
    )

    found = await db_chats.earlier_chat_messages(
        as_client(fake), owner_telegram_id=OWNER_ID, thread_id=THREAD_ID, limit=20
    )

    assert [message.id for message in found] == ["early", "late"]
    assert ("not.is", "analysis_id", None) in fake.calls
    assert ("is", "erased_at", None) in fake.calls
    assert ("order", "sent_at", True) in fake.calls


@pytest.mark.parametrize(
    "row",
    [
        message_row(sent_at=None),
        message_row(direction="sideways"),
        message_row(kind="sticker"),
        {"id": MESSAGE_ID},
        "строка",
    ],
)
async def test_incomplete_chat_message_is_a_refusal(row: object) -> None:
    fake = FakeClient(tables={"chat_messages": [row]})

    with pytest.raises(DatabaseError):
        await db_chats.new_chat_messages(
            as_client(fake), owner_telegram_id=OWNER_ID, thread_id=THREAD_ID, limit=50
        )


async def test_record_analysis_goes_as_one_call_with_the_trace() -> None:
    client = rpc({"record_chat_analysis": ANALYSIS_ID})
    trace = ChatTrace(
        analysis={"deals": []},
        model="claude-opus-5",
        input_tokens=1800,
        output_tokens=240,
        duration_ms=9500,
    )
    task = {"item": 1, "task": {"title": "прислать расчёт"}, "reminders": []}
    waiting = {"about": "он спрашивал, во сколько созвон", "to": "Игорю", "since": NOW.isoformat()}

    saved = await db_chats.record_chat_analysis(
        cast(Client, client),
        owner_telegram_id=OWNER_ID,
        thread_id=THREAD_ID,
        message_ids=[MESSAGE_ID],
        trace=trace,
        chat_with="Игорем",
        waiting=waiting,
        tasks=[task],
    )

    assert saved == ANALYSIS_ID
    assert client.params == [
        {
            "owner_telegram_id": OWNER_ID,
            "thread_id": THREAD_ID,
            "message_ids": [MESSAGE_ID],
            "analysis": {"deals": []},
            "ai_model": "claude-opus-5",
            "input_tokens": 1800,
            "output_tokens": 240,
            "duration_ms": 9500,
            "chat_with": "Игорем",
            "waiting": waiting,
            "tasks": [task],
        }
    ]


async def test_record_analysis_repeat_is_none_and_garbage_is_a_refusal() -> None:
    trace = ChatTrace(analysis={}, model="m", input_tokens=1, output_tokens=1, duration_ms=1)
    repeat = rpc({"record_chat_analysis": None})
    garbage = rpc({"record_chat_analysis": 42})

    assert (
        await db_chats.record_chat_analysis(
            cast(Client, repeat),
            owner_telegram_id=OWNER_ID,
            thread_id=THREAD_ID,
            message_ids=[MESSAGE_ID],
            trace=trace,
            chat_with=None,
            waiting=None,
            tasks=[],
        )
        is None
    )
    with pytest.raises(DatabaseError):
        await db_chats.record_chat_analysis(
            cast(Client, garbage),
            owner_telegram_id=OWNER_ID,
            thread_id=THREAD_ID,
            message_ids=[MESSAGE_ID],
            trace=trace,
            chat_with=None,
            waiting=None,
            tasks=[],
        )


async def test_chats_to_analyze_read_the_threads() -> None:
    client = rpc(
        {
            "chats_to_analyze": [
                {
                    "thread_id": THREAD_ID,
                    "platform": "telegram",
                    "chat_key": IGOR_CHAT,
                    "name": "Игорь",
                }
            ]
        }
    )

    found = await db_chats.chats_to_analyze(
        cast(Client, client),
        owner_telegram_id=OWNER_ID,
        quiet_before=NOW - timedelta(minutes=20),
        stale_before=NOW - timedelta(hours=2),
    )

    assert found == [
        ChatToAnalyze(thread_id=THREAD_ID, platform="telegram", chat_key=IGOR_CHAT, name="Игорь")
    ]
    assert client.params[0]["owner_telegram_id"] == OWNER_ID


async def test_failures_count_and_a_missing_thread_is_none() -> None:
    counted = rpc({"chat_failed": 2})
    missing = rpc({"chat_failed": None})

    assert (
        await db_chats.chat_failed(
            cast(Client, counted), owner_telegram_id=OWNER_ID, thread_id=THREAD_ID
        )
        == 2
    )
    assert (
        await db_chats.chat_failed(
            cast(Client, missing), owner_telegram_id=OWNER_ID, thread_id=THREAD_ID
        )
        is None
    )


async def test_report_reads_the_lines_and_the_chat() -> None:
    client = rpc(
        {
            "chat_report": [
                report_row(),
                report_row(item=2, title="Игорь пришлёт договор", promise="to_me", due_at=None),
            ]
        }
    )

    found = await db_chats.chat_report(
        cast(Client, client), owner_telegram_id=OWNER_ID, analysis_id=ANALYSIS_ID
    )

    assert found == ChatReport(
        platform="telegram",
        chat_name="Игорь Петров",
        chat_with="Игорем",
        lines=(
            ReportLine(
                item=1,
                task_id=TASK_ID,
                title="прислать Игорю расчёт",
                due_at=datetime(2026, 10, 9, 18, 0, tzinfo=TZ),
                due_precision="day",
                promise="mine",
                status="active",
            ),
            ReportLine(
                item=2,
                task_id=TASK_ID,
                title="Игорь пришлёт договор",
                due_at=None,
                due_precision="day",
                promise="to_me",
                status="active",
            ),
        ),
    )


async def test_report_without_lines_is_none() -> None:
    client = rpc({"chat_report": []})

    assert (
        await db_chats.chat_report(
            cast(Client, client), owner_telegram_id=OWNER_ID, analysis_id=ANALYSIS_ID
        )
        is None
    )


async def test_reports_to_send_are_the_owner_unreported_with_deals() -> None:
    fake = FakeClient(tables={"chat_analyses": [{"id": ANALYSIS_ID}]})

    found = await db_chats.reports_to_send(as_client(fake), owner_telegram_id=OWNER_ID)

    assert found == [ANALYSIS_ID]
    assert ("eq", "owner_telegram_id", OWNER_ID) in fake.calls
    assert ("is", "reported_at", None) in fake.calls
    assert ("neq", "items", 0) in fake.calls


async def test_drop_reads_the_status_and_a_missing_task_is_none() -> None:
    dropped = rpc({"drop_chat_task": {"id": TASK_ID, "status": "cancelled"}})
    missing = rpc({"drop_chat_task": {"id": None, "status": None}})

    assert (
        await db_chats.drop_chat_task(
            cast(Client, dropped), owner_telegram_id=OWNER_ID, analysis_id=ANALYSIS_ID, item=1
        )
        == "cancelled"
    )
    assert (
        await db_chats.drop_chat_task(
            cast(Client, missing), owner_telegram_id=OWNER_ID, analysis_id=ANALYSIS_ID, item=1
        )
        is None
    )
    assert dropped.params == [
        {"owner_telegram_id": OWNER_ID, "analysis_id": ANALYSIS_ID, "item": 1}
    ]


async def test_waiting_chats_are_read_whole() -> None:
    client = rpc(
        {
            "chats_waiting": [
                {
                    "thread_id": THREAD_ID,
                    "platform": "telegram",
                    "name": "Игорь Петров",
                    "waiting_since": "2026-10-07T05:00:00+00:00",
                    "waiting_about": "он спрашивал, во сколько созвон",
                    "waiting_to": "Игорю",
                }
            ]
        }
    )

    found = await db_chats.chats_waiting(
        cast(Client, client), owner_telegram_id=OWNER_ID, asked_before=NOW
    )

    assert found == [
        WaitingChat(
            thread_id=THREAD_ID,
            platform="telegram",
            name="Игорь Петров",
            since=datetime(2026, 10, 7, 10, 0, tzinfo=TZ),
            about="он спрашивал, во сколько созвон",
            to="Игорю",
        )
    ]


@pytest.mark.parametrize(
    ("function", "call"),
    [
        (
            "mark_consent_asked",
            lambda client: db_chats.mark_consent_asked(
                client, owner_telegram_id=OWNER_ID, platform="telegram"
            ),
        ),
        (
            "set_chat_transcript",
            lambda client: db_chats.set_chat_transcript(
                client, owner_telegram_id=OWNER_ID, message_id=MESSAGE_ID, transcript="т"
            ),
        ),
        (
            "mark_chat_report_sent",
            lambda client: db_chats.mark_chat_report_sent(
                client, owner_telegram_id=OWNER_ID, analysis_id=ANALYSIS_ID, telegram_message_id=1
            ),
        ),
        (
            "mark_waiting_reminded",
            lambda client: db_chats.mark_waiting_reminded(
                client, owner_telegram_id=OWNER_ID, thread_id=THREAD_ID, since=NOW
            ),
        ),
    ],
)
async def test_yes_or_no_answers_must_be_booleans(function: str, call: Any) -> None:
    assert await call(cast(Client, rpc({function: True}))) is True
    with pytest.raises(DatabaseError):
        await call(cast(Client, rpc({function: "да"})))


async def test_erasing_counts_and_garbage_is_a_refusal() -> None:
    erased = rpc({"erase_old_chat_messages": 3})
    garbage = rpc({"erase_old_chat_messages": True})

    assert (
        await db_chats.erase_old_chat_messages(
            cast(Client, erased), owner_telegram_id=OWNER_ID, before=NOW
        )
        == 3
    )
    with pytest.raises(DatabaseError):
        await db_chats.erase_old_chat_messages(
            cast(Client, garbage), owner_telegram_id=OWNER_ID, before=NOW
        )


async def test_every_chat_function_takes_the_owner_by_name() -> None:
    """Инвариант 2: у каждой функции слоя владелец — именованный и без значения
    по умолчанию."""
    for name, function in inspect.getmembers(db_chats, inspect.iscoroutinefunction):
        if function.__module__ != db_chats.__name__:
            continue
        parameter = inspect.signature(function).parameters.get("owner_telegram_id")
        assert parameter is not None, name
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY, name
        assert parameter.default is inspect.Parameter.empty, name


# ---------------------------------------------- приём: сообщение Telegram (§25.2)

IGOR_ID = 1001
SENT = datetime(2026, 10, 7, 11, 0, tzinfo=TZ)


def business_message(
    text: str | None = "Пришлёшь расчёт в пятницу?",
    *,
    from_id: int = IGOR_ID,
    message_id: int = 42,
    connection: str | None = CONNECTION,
    voice: bool = False,
    video_note: bool = False,
    photo: bool = False,
    caption: str | None = None,
    sticker: bool = False,
    by_bot: bool = False,
    offline: bool | None = None,
) -> Message:
    """Сообщение личного чата владельца с Игорем — как его приносит Telegram."""
    author = (
        User(id=OWNER_ID, is_bot=False, first_name="Тим")
        if from_id == OWNER_ID
        else User(id=from_id, is_bot=False, first_name="Игорь", last_name="Петров")
    )
    return Message(
        message_id=message_id,
        date=SENT,
        chat=Chat(id=IGOR_ID, type="private", first_name="Игорь", last_name="Петров"),
        from_user=author,
        business_connection_id=connection,
        text=text,
        caption=caption,
        voice=Voice(file_id="voice-1", file_unique_id="v1", duration=5) if voice else None,
        video_note=(
            VideoNote(file_id="note-1", file_unique_id="n1", length=240, duration=5)
            if video_note
            else None
        ),
        photo=[PhotoSize(file_id="photo-1", file_unique_id="p1", width=90, height=90)]
        if photo
        else None,
        sticker=(
            Sticker(
                file_id="sticker-1",
                file_unique_id="s1",
                type="regular",
                width=512,
                height=512,
                is_animated=False,
                is_video=False,
            )
            if sticker
            else None
        ),
        sender_business_bot=User(id=5, is_bot=True, first_name="Автоответчик") if by_bot else None,
        is_from_offline=offline,
    )


def test_message_from_the_interlocutor_is_incoming() -> None:
    incoming = handlers.chat_message_of(business_message(), OWNER_ID)

    assert incoming == Incoming(
        platform="telegram",
        connection_id=CONNECTION,
        chat_key=str(IGOR_ID),
        chat_name="Игорь Петров",
        external_id="42",
        direction="in",
        sender="Игорь Петров",
        sent_at=SENT,
        kind="text",
        text="Пришлёшь расчёт в пятницу?",
    )


def test_message_of_the_owner_is_outgoing_in_the_same_chat() -> None:
    incoming = handlers.chat_message_of(
        business_message("Да, в пятницу пришлю", from_id=OWNER_ID), OWNER_ID
    )

    assert incoming is not None
    assert incoming.direction == "out"
    assert incoming.sender == "Тим"
    assert incoming.chat_key == str(IGOR_ID)
    assert incoming.chat_name == "Игорь Петров"


@pytest.mark.parametrize(
    ("message", "kind", "text", "file_id"),
    [
        (business_message(None, voice=True), "voice", "", "voice-1"),
        (business_message(None, video_note=True), "video_note", "", "note-1"),
        (
            business_message(None, photo=True, caption="вот такой нужен"),
            "photo",
            "вот такой нужен",
            None,
        ),
        (business_message(None, photo=True), "photo", "", None),
        (business_message(None, sticker=True), "other", "", None),
    ],
)
def test_voice_photo_and_the_rest_are_marked_by_kind(
    message: Message, kind: str, text: str, file_id: str | None
) -> None:
    incoming = handlers.chat_message_of(message, OWNER_ID)

    assert incoming is not None
    assert (incoming.kind, incoming.text, incoming.file_id) == (kind, text, file_id)


@pytest.mark.parametrize(
    "message",
    [
        business_message(by_bot=True),
        business_message(offline=True),
        business_message(connection=None),
    ],
)
def test_bot_written_offline_and_unconnected_messages_are_skipped(message: Message) -> None:
    assert handlers.chat_message_of(message, OWNER_ID) is None


# ------------------------------------------------------------- согласие (§25.5)


def test_consent_button_data_goes_both_ways() -> None:
    assert chats.consent_data("telegram", agreed=True) == "consent:telegram:yes"
    assert chats.consent_data("max", agreed=False) == "consent:max:no"
    assert chats.parse_consent("consent:telegram:yes") == ("telegram", True)
    assert chats.parse_consent("consent:instagram:no") == ("instagram", False)


@pytest.mark.parametrize(
    "data",
    [
        "consent:",
        "consent:vk:yes",
        "consent:telegram:maybe",
        "consent:telegram",
        "consent:telegram:yes:1",
    ],
)
def test_crooked_consent_data_is_none(data: str) -> None:
    assert chats.parse_consent(data) is None


def test_consent_question_says_what_is_read_where_it_goes_and_how_long() -> None:
    assert texts.consent_question("telegram") == (
        "Подключено чтение личных чатов Telegram. Я читаю новые сообщения в чатах, которые "
        "вы выбрали, и отправляю текст и голосовые на разбор (Claude через посредника, "
        "Deepgram). Храню переписку 7 дней, записанные дела — пока не уберёте. Писать в ваши "
        "чаты я не могу. Согласны?"
    )
    for platform in ("instagram", "max"):
        question = texts.consent_question(platform)
        assert "7 дней" in question and question.endswith("Согласны?"), platform


# --------------------------------------------------- подменённая база чатов


class FakeChatStore:
    """`ChatStore` в памяти — с теми же правилами, что функции базы (§3.15):
    без подключения владельца и согласия ничего не хранится, разобранное
    второй раз не пишется, «ждёт ответа» снимается сообщением владельца."""

    def __init__(self, now: datetime = NOW) -> None:
        self.now = now
        self.sources: dict[str, dict[str, Any]] = {}
        self.threads: dict[str, dict[str, Any]] = {}
        self.messages: list[dict[str, Any]] = []
        self.analyses: list[dict[str, Any]] = []
        self.tasks: list[dict[str, Any]] = []
        self.calls: list[str] = []
        self.broken: set[str] = set()
        self._ids = 0

    def _id(self, prefix: str) -> str:
        self._ids += 1
        return f"{prefix}{self._ids}"

    def _call(self, name: str) -> None:
        self.calls.append(name)
        if name in self.broken:
            raise DatabaseError(f"{name}: база не ответила")

    def _source(self, platform: str) -> ChatSource:
        row = self.sources[platform]
        return ChatSource(
            platform=cast(Any, platform),
            connection_id=row["connection_id"],
            is_enabled=row["is_enabled"],
            asked_at=row["asked_at"],
            consented_at=row["consented_at"],
            declined_at=row["declined_at"],
        )

    def consent(self, platform: str = "telegram", connection: str | None = CONNECTION) -> None:
        """Площадка подключена, вопрос ушёл, владелец согласился."""
        self.sources[platform] = {
            "connection_id": connection,
            "is_enabled": True,
            "asked_at": self.now,
            "consented_at": self.now,
            "declined_at": None,
        }

    async def connect(self, platform: str, connection_id: str | None, enabled: bool) -> ChatSource:
        self._call("connect")
        row = self.sources.get(platform)
        if row is None:
            row = {
                "connection_id": connection_id,
                "is_enabled": enabled,
                "asked_at": None,
                "consented_at": None,
                "declined_at": None,
            }
            self.sources[platform] = row
        else:
            if enabled and not row["is_enabled"] and row["declined_at"] is not None:
                row["asked_at"] = None
                row["declined_at"] = None
            row["connection_id"] = connection_id
            row["is_enabled"] = enabled
        return self._source(platform)

    async def sources_to_ask(self) -> list[ChatSource]:
        self._call("sources_to_ask")
        return [
            self._source(platform)
            for platform, row in self.sources.items()
            if row["is_enabled"]
            and row["asked_at"] is None
            and row["consented_at"] is None
            and row["declined_at"] is None
        ]

    async def mark_asked(self, platform: str) -> bool:
        self._call("mark_asked")
        row = self.sources.get(platform)
        if row is None or row["asked_at"] is not None:
            return False
        row["asked_at"] = self.now
        return True

    async def answer(self, platform: str, agreed: bool) -> ChatSource | None:
        self._call("answer")
        row = self.sources.get(platform)
        if row is None:
            return None
        row["consented_at"] = (row["consented_at"] or self.now) if agreed else None
        row["declined_at"] = None if agreed else (row["declined_at"] or self.now)
        row["asked_at"] = row["asked_at"] or self.now
        return self._source(platform)

    def thread(self, chat_key: str = IGOR_CHAT, platform: str = "telegram") -> dict[str, Any]:
        return self.threads[f"{platform}:{chat_key}"]

    def _thread_by_id(self, thread_id: str) -> dict[str, Any]:
        return next(row for row in self.threads.values() if row["id"] == thread_id)

    async def store(self, incoming: Incoming, tracks_waiting: bool = True) -> Stored:
        self._call("store")
        row = self.sources.get(incoming.platform)
        if row is None:
            return Stored("no_source", None)
        if row["connection_id"] != incoming.connection_id:
            return Stored("unknown_connection", None)
        if not row["is_enabled"]:
            return Stored("disabled", None)
        if row["consented_at"] is None:
            return Stored("no_consent", None)
        key = f"{incoming.platform}:{incoming.chat_key}"
        thread = self.threads.setdefault(
            key,
            {
                "id": self._id("t"),
                "platform": incoming.platform,
                "chat_key": incoming.chat_key,
                "name": incoming.chat_name,
                "tracks_waiting": tracks_waiting,
                "last_message_at": self.now,
                "last_out_at": None,
                "waiting_since": None,
                "waiting_about": None,
                "waiting_to": None,
                "waiting_reminded_at": None,
                "failures": 0,
            },
        )
        thread["name"] = incoming.chat_name or thread["name"]
        for message in self.messages:
            if (
                message["thread_id"] == thread["id"]
                and message["external_id"] == incoming.external_id
            ):
                return Stored("repeat", message["id"])
        saved = {
            "id": self._id("m"),
            "thread_id": thread["id"],
            "external_id": incoming.external_id,
            "direction": incoming.direction,
            "sender": incoming.sender,
            "sent_at": incoming.sent_at,
            "kind": incoming.kind,
            "text": incoming.text,
            "analysis_id": None,
            "erased": False,
            "created_at": self.now,
        }
        self.messages.append(saved)
        thread["last_message_at"] = self.now
        if incoming.direction == "out":
            last = thread["last_out_at"]
            thread["last_out_at"] = (
                incoming.sent_at if last is None else max(last, incoming.sent_at)
            )
            since = thread["waiting_since"]
            if since is not None and since <= incoming.sent_at:
                thread.update(
                    waiting_since=None,
                    waiting_about=None,
                    waiting_to=None,
                    waiting_reminded_at=None,
                )
        return Stored("stored", saved["id"])

    async def set_transcript(self, message_id: str, transcript: str) -> bool:
        self._call("set_transcript")
        for message in self.messages:
            if message["id"] == message_id and message["analysis_id"] is None:
                message["text"] = transcript
                return True
        return False

    async def edit(self, incoming: Incoming) -> bool:
        self._call("edit")
        source = self.sources.get(incoming.platform)
        thread = self.threads.get(f"{incoming.platform}:{incoming.chat_key}")
        if source is None or source["connection_id"] != incoming.connection_id or thread is None:
            return False
        for message in self.messages:
            if (
                message["thread_id"] == thread["id"]
                and message["external_id"] == incoming.external_id
                and message["analysis_id"] is None
                and not message["erased"]
            ):
                message["text"] = incoming.text
                return True
        return False

    async def erase(
        self, platform: str, connection_id: str | None, chat_key: str, external_ids: Sequence[str]
    ) -> int:
        self._call("erase")
        source = self.sources.get(platform)
        thread = self.threads.get(f"{platform}:{chat_key}")
        if source is None or source["connection_id"] != connection_id or thread is None:
            return 0
        erased = 0
        for message in self.messages:
            if (
                message["thread_id"] == thread["id"]
                and message["external_id"] in external_ids
                and not message["erased"]
            ):
                message.update(text="", erased=True)
                erased += 1
        return erased

    def _pending(self, thread_id: str) -> list[dict[str, Any]]:
        return [
            message
            for message in self.messages
            if message["thread_id"] == thread_id and message["analysis_id"] is None
        ]

    async def to_analyze(
        self, quiet_before: datetime, stale_before: datetime
    ) -> list[ChatToAnalyze]:
        self._call("to_analyze")
        found: list[tuple[datetime, ChatToAnalyze]] = []
        for thread in self.threads.values():
            consented = self.sources.get(thread["platform"], {}).get("consented_at") is not None
            pending = self._pending(thread["id"])
            if not consented or not pending:
                continue
            first = min(message["created_at"] for message in pending)
            if thread["last_message_at"] < quiet_before or first < stale_before:
                chat = ChatToAnalyze(
                    thread_id=thread["id"],
                    platform=thread["platform"],
                    chat_key=thread["chat_key"],
                    name=thread["name"],
                )
                found.append((first, chat))
        return [chat for _, chat in sorted(found, key=lambda pair: pair[0])]

    @staticmethod
    def _as_message(row: dict[str, Any]) -> ChatMessage:
        return ChatMessage(
            id=row["id"],
            direction=row["direction"],
            sender=row["sender"],
            sent_at=row["sent_at"],
            kind=row["kind"],
            text=row["text"],
            erased=row["erased"],
        )

    async def new_messages(self, thread_id: str, limit: int) -> list[ChatMessage]:
        self._call("new_messages")
        pending = sorted(
            self._pending(thread_id), key=lambda row: (row["sent_at"], row["created_at"])
        )
        return [self._as_message(row) for row in pending[:limit]]

    async def earlier_messages(self, thread_id: str, limit: int) -> list[ChatMessage]:
        self._call("earlier_messages")
        done = [
            row
            for row in self.messages
            if row["thread_id"] == thread_id
            and row["analysis_id"] is not None
            and not row["erased"]
        ]
        newest = sorted(done, key=lambda row: row["sent_at"], reverse=True)[:limit]
        return [self._as_message(row) for row in sorted(newest, key=lambda row: row["sent_at"])]

    def _cover(self, thread_id: str, message_ids: Sequence[str], analysis_id: str) -> int:
        covered = [row for row in self._pending(thread_id) if row["id"] in message_ids]
        for row in covered:
            row["analysis_id"] = analysis_id
        return len(covered)

    async def record(
        self,
        thread_id: str,
        message_ids: Sequence[str],
        trace: ChatTrace,
        chat_with: str | None,
        waiting: Mapping[str, str] | None,
        tasks: Sequence[Mapping[str, Any]],
    ) -> str | None:
        self._call("record")
        if not any(row["id"] in message_ids for row in self._pending(thread_id)):
            return None
        analysis_id = self._id("a")
        count = self._cover(thread_id, message_ids, analysis_id)
        self.analyses.append(
            {
                "id": analysis_id,
                "thread_id": thread_id,
                "status": "done",
                "messages_count": count,
                "items": len(tasks),
                "chat_with": chat_with,
                "waiting": waiting,
                "trace": trace,
                "reported_at": None,
                "report_message_id": None,
            }
        )
        for entry in tasks:
            self.tasks.append(
                {
                    "id": self._id("k"),
                    "analysis_id": analysis_id,
                    "item": entry["item"],
                    "task": entry["task"],
                    "reminders": entry["reminders"],
                    "status": "active",
                }
            )
        thread = self._thread_by_id(thread_id)
        thread["failures"] = 0
        if waiting is not None and thread["tracks_waiting"]:
            since = datetime.fromisoformat(waiting["since"])
            last_out = thread["last_out_at"]
            if last_out is None or last_out < since:
                if thread["waiting_since"] is None or thread["waiting_reminded_at"] is not None:
                    thread.update(waiting_since=since, waiting_reminded_at=None)
                thread.update(waiting_about=waiting["about"], waiting_to=waiting["to"])
        return analysis_id

    async def failed(self, thread_id: str) -> int | None:
        self._call("failed")
        thread = self._thread_by_id(thread_id)
        thread["failures"] += 1
        return int(thread["failures"])

    async def skip(self, thread_id: str, message_ids: Sequence[str]) -> str | None:
        self._call("skip")
        if not any(row["id"] in message_ids for row in self._pending(thread_id)):
            return None
        analysis_id = self._id("a")
        count = self._cover(thread_id, message_ids, analysis_id)
        self.analyses.append(
            {
                "id": analysis_id,
                "thread_id": thread_id,
                "status": "skipped",
                "messages_count": count,
                "items": 0,
                "reported_at": None,
                "report_message_id": None,
            }
        )
        self._thread_by_id(thread_id)["failures"] = 0
        return analysis_id

    async def reports_to_send(self) -> list[str]:
        self._call("reports_to_send")
        return [
            analysis["id"]
            for analysis in self.analyses
            if analysis["items"] > 0 and analysis["reported_at"] is None
        ]

    async def report(self, analysis_id: str) -> ChatReport | None:
        self._call("report")
        analysis = next(row for row in self.analyses if row["id"] == analysis_id)
        thread = self._thread_by_id(analysis["thread_id"])
        lines = [
            ReportLine(
                item=task["item"],
                task_id=task["id"],
                title=task["task"]["title"],
                due_at=(
                    None
                    if task["task"]["due_at"] is None
                    else datetime.fromisoformat(task["task"]["due_at"])
                ),
                due_precision=task["task"]["due_precision"],
                promise=task["task"]["promise"],
                status=task["status"],
            )
            for task in sorted(self.tasks, key=lambda row: row["item"])
            if task["analysis_id"] == analysis_id
        ]
        if not lines:
            return None
        return ChatReport(
            platform=thread["platform"],
            chat_name=thread["name"],
            chat_with=analysis["chat_with"],
            lines=tuple(lines),
        )

    async def report_sent(self, analysis_id: str, telegram_message_id: int | None) -> bool:
        self._call("report_sent")
        analysis = next(row for row in self.analyses if row["id"] == analysis_id)
        if analysis["reported_at"] is not None:
            return False
        analysis.update(reported_at=self.now, report_message_id=telegram_message_id)
        return True

    async def drop(self, analysis_id: str, item: int) -> str | None:
        self._call("drop")
        for task in self.tasks:
            if task["analysis_id"] == analysis_id and task["item"] == item:
                if task["status"] == "active":
                    task["status"] = "cancelled"
                return str(task["status"])
        return None

    async def waiting(self, asked_before: datetime) -> list[WaitingChat]:
        self._call("waiting")
        return [
            WaitingChat(
                thread_id=thread["id"],
                platform=thread["platform"],
                name=thread["name"],
                since=thread["waiting_since"],
                about=thread["waiting_about"],
                to=thread["waiting_to"],
            )
            for thread in self.threads.values()
            if thread["tracks_waiting"]
            and thread["waiting_since"] is not None
            and thread["waiting_since"] <= asked_before
            and thread["waiting_reminded_at"] is None
            and (thread["last_out_at"] is None or thread["last_out_at"] < thread["waiting_since"])
        ]

    async def reminded(self, thread_id: str, since: datetime) -> bool:
        self._call("reminded")
        thread = self._thread_by_id(thread_id)
        if thread["waiting_since"] != since or thread["waiting_reminded_at"] is not None:
            return False
        thread["waiting_reminded_at"] = self.now
        return True

    async def erase_old(self, before: datetime) -> int:
        self._call("erase_old")
        erased = 0
        for message in self.messages:
            if message["created_at"] < before and not message["erased"]:
                message.update(text="", erased=True)
                erased += 1
        return erased


def network_error() -> TelegramNetworkError:
    return TelegramNetworkError(method=cast(Any, None), message="timeout")


class FakeSender:
    """Сообщения владельцу: что ушло и с какими кнопками; `broken` — Telegram не принял."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, tuple[Button, ...]]] = []
        self.broken = False

    async def __call__(self, *, text: str, buttons: Sequence[Button] = ()) -> int:
        if self.broken:
            raise network_error()
        self.sent.append((text, tuple(buttons)))
        return 500 + len(self.sent)

    @property
    def texts(self) -> list[str]:
        return [text for text, _ in self.sent]


class FakeLookup:
    """`getBusinessConnection`: чьё подключение — по словарю; вызовы записываются."""

    def __init__(self, owners: Mapping[str, int] | None = None) -> None:
        self.owners = dict(owners or {})
        self.calls: list[str] = []

    async def __call__(self, connection_id: str) -> Connection:
        self.calls.append(connection_id)
        if connection_id not in self.owners:
            raise network_error()
        return Connection(user_id=self.owners[connection_id], is_enabled=True)


def chat_service(
    store: FakeChatStore | None = None,
    sender: OwnerSender | None = None,
    lookup: FakeLookup | None = None,
    transcriber: FakeTranscriber | None = None,
) -> ChatService:
    return ChatService(
        settings=make_settings(),
        store=store or FakeChatStore(),
        send=sender or FakeSender(),
        lookup=lookup,
        transcriber=transcriber,
    )


def incoming(
    text: str = "Пришлёшь расчёт в пятницу?",
    *,
    external_id: str = "42",
    direction: str = "in",
    sent_at: datetime = SENT,
    kind: str = "text",
    connection: str | None = CONNECTION,
    chat_key: str = IGOR_CHAT,
    name: str = "Игорь Петров",
) -> Incoming:
    return Incoming(
        platform="telegram",
        connection_id=connection,
        chat_key=chat_key,
        chat_name=name,
        external_id=external_id,
        direction=cast(Any, direction),
        sender="Тим" if direction == "out" else name,
        sent_at=sent_at,
        kind=cast(Any, kind),
        text=text,
    )


async def audio() -> bytes:
    return b"OggS"


async def no_audio() -> bytes:
    raise network_error()


# ---------------------------------------------------- приём и согласие: сервис


async def connect_owner(service: ChatService, enabled: bool = True) -> None:
    await service.connected(
        platform="telegram", connection_id=CONNECTION, user_id=OWNER_ID, enabled=enabled
    )


async def test_owner_connection_is_recorded_and_consent_is_asked_once() -> None:
    store = FakeChatStore()
    sender = FakeSender()
    service = chat_service(store, sender)

    await connect_owner(service)
    await connect_owner(service)

    assert sender.texts == [texts.consent_question("telegram")]
    _, buttons = sender.sent[0]
    assert [button.text for button in buttons] == [texts.CONSENT_YES, texts.CONSENT_NO]
    assert [button.data for button in buttons] == ["consent:telegram:yes", "consent:telegram:no"]
    assert store.sources["telegram"]["asked_at"] is not None


async def test_stranger_connection_stores_nothing_and_says_nothing() -> None:
    store = FakeChatStore()
    sender = FakeSender()

    await chat_service(store, sender).connected(
        platform="telegram", connection_id="biz-stranger", user_id=999, enabled=True
    )

    assert store.calls == []
    assert sender.sent == []


async def test_unsent_consent_question_is_asked_again_later() -> None:
    store = FakeChatStore()
    sender = FakeSender()
    sender.broken = True
    service = chat_service(store, sender)

    await connect_owner(service)
    assert store.sources["telegram"]["asked_at"] is None, "не ушло — не помечено"

    sender.broken = False
    assert await service.ask_consents() == 1
    assert sender.texts == [texts.consent_question("telegram")]


async def test_disabled_connection_asks_nothing() -> None:
    store = FakeChatStore()
    sender = FakeSender()

    await connect_owner(chat_service(store, sender), enabled=False)

    assert store.sources["telegram"]["is_enabled"] is False
    assert sender.sent == []


async def test_before_consent_messages_are_not_kept() -> None:
    store = FakeChatStore()
    service = chat_service(store)
    await connect_owner(service)

    stored = await service.receive(incoming())

    assert stored == Stored("no_consent", None)
    assert store.messages == []


async def test_consent_yes_stores_and_leaves_a_button_to_stop() -> None:
    store = FakeChatStore()
    service = chat_service(store)
    await connect_owner(service)

    outcome = await service.answer_consent("telegram", True)
    stored = await service.receive(incoming())

    assert outcome.replace is True
    assert outcome.message == texts.consent_answered("telegram", agreed=True)
    assert [(button.text, button.data) for button in outcome.buttons] == [
        (texts.CONSENT_STOP, "consent:telegram:no")
    ]
    assert stored is not None and stored.outcome == "stored"
    assert [message["text"] for message in store.messages] == ["Пришлёшь расчёт в пятницу?"]


async def test_consent_no_keeps_nothing_and_offers_to_agree() -> None:
    store = FakeChatStore()
    service = chat_service(store)
    await connect_owner(service)

    outcome = await service.answer_consent("telegram", False)
    await service.receive(incoming())

    assert outcome.message == texts.consent_answered("telegram", agreed=False)
    assert "Автоматизация чатов" in outcome.message
    assert [button.data for button in outcome.buttons] == ["consent:telegram:yes"]
    assert store.messages == []


async def test_consent_answer_without_a_source_or_database_is_a_popup() -> None:
    store = FakeChatStore()
    missing = await chat_service(store).answer_consent("telegram", True)
    store.consent()
    store.broken.add("answer")
    broken = await chat_service(store).answer_consent("telegram", True)

    assert (missing.message, missing.replace) == (texts.CONSENT_UNKNOWN, False)
    assert (broken.message, broken.replace) == (texts.CONSENT_NOT_SAVED, False)


async def test_unknown_connection_of_the_owner_is_adopted_and_asked_about() -> None:
    """Подключение до этапа или пропущенное событие: Telegram называет
    владельца — подключение пишется, вопрос о согласии уходит, а сообщение
    до «Согласен» не хранится."""
    store = FakeChatStore()
    sender = FakeSender()
    lookup = FakeLookup({CONNECTION: OWNER_ID})

    stored = await chat_service(store, sender, lookup).receive(incoming())

    assert lookup.calls == [CONNECTION]
    assert store.sources["telegram"]["connection_id"] == CONNECTION
    assert sender.texts == [texts.consent_question("telegram")]
    assert stored == Stored("no_consent", None)


async def test_new_connection_of_the_owner_after_consent_is_stored() -> None:
    store = FakeChatStore()
    store.consent(connection="biz-old")
    lookup = FakeLookup({CONNECTION: OWNER_ID})

    stored = await chat_service(store, FakeSender(), lookup).receive(incoming())

    assert stored is not None and stored.outcome == "stored"
    assert store.sources["telegram"]["connection_id"] == CONNECTION


async def test_stranger_connection_is_asked_once_and_never_stored() -> None:
    store = FakeChatStore()
    store.consent()
    sender = FakeSender()
    lookup = FakeLookup({"biz-stranger": 999})
    service = chat_service(store, sender, lookup)

    first = await service.receive(incoming(connection="biz-stranger"))
    second = await service.receive(incoming(connection="biz-stranger", external_id="43"))

    assert lookup.calls == ["biz-stranger"], "чужое подключение спрашивается один раз"
    assert first == second == Stored("unknown_connection", None)
    assert store.messages == []
    assert sender.sent == []
    assert store.sources["telegram"]["connection_id"] == CONNECTION


async def test_lookup_failure_keeps_nothing() -> None:
    store = FakeChatStore()

    stored = await chat_service(store, FakeSender(), FakeLookup()).receive(
        incoming(connection="biz-unknown")
    )

    assert stored == Stored("no_source", None)
    assert store.messages == []


async def test_database_refusal_on_receive_is_none() -> None:
    store = FakeChatStore()
    store.consent()
    store.broken.add("store")

    assert await chat_service(store).receive(incoming()) is None


async def test_voice_is_stored_first_and_transcribed_with_the_name_hint() -> None:
    store = FakeChatStore()
    store.consent()
    transcriber = FakeTranscriber(Transcript(text="Перезвони после обеда", confidence=0.9))

    await chat_service(store, transcriber=transcriber).receive(incoming("", kind="voice"), audio)

    assert [message["text"] for message in store.messages] == ["Перезвони после обеда"]
    assert store.calls == ["store", "set_transcript"]
    assert transcriber.names == [("Игорь Петров",)]


async def test_unheard_voice_stays_empty() -> None:
    store = FakeChatStore()
    store.consent()
    service = chat_service(store, transcriber=FakeTranscriber(NotTranscribed("тишина")))

    await service.receive(incoming("", kind="voice"), no_audio)
    await service.receive(incoming("", kind="voice", external_id="43"), audio)

    assert [message["text"] for message in store.messages] == ["", ""]
    assert "set_transcript" not in store.calls


async def test_edit_and_delete_go_with_the_connection() -> None:
    store = FakeChatStore()
    store.consent()
    service = chat_service(store)
    await service.receive(incoming())

    assert await service.edited(incoming("Пришлёшь расчёт в четверг?")) is True
    assert await service.edited(incoming("чужая правка", connection="biz-stranger")) is False
    assert store.messages[0]["text"] == "Пришлёшь расчёт в четверг?"
    erased = await service.deleted(
        platform="telegram", connection_id=CONNECTION, chat_key=IGOR_CHAT, external_ids=["42"]
    )
    assert erased == 1
    assert store.messages[0]["text"] == ""


# ------------------------------------------- приём и согласие: через Telegram


def business_update(message: Message, update_id: int = 1) -> Update:
    return Update(update_id=update_id, business_message=message)


def connection_update(user_id: int = OWNER_ID, enabled: bool = True, update_id: int = 1) -> Update:
    owner = user_id == OWNER_ID
    return Update(
        update_id=update_id,
        business_connection=BusinessConnection(
            id=CONNECTION if owner else "biz-stranger",
            user=User(id=user_id, is_bot=False, first_name="Тим" if owner else "Чужой"),
            user_chat_id=user_id,
            date=SENT,
            is_enabled=enabled,
        ),
    )


def telegram_sender(bot: Bot) -> OwnerSender:
    """Отправка владельцу через подменённую сессию — как в сборке
    (`runner.build_chats`)."""

    async def send(*, text: str, buttons: Sequence[Button] = ()) -> int:
        message = await bot.send_message(
            chat_id=OWNER_ID, text=text, reply_markup=handlers.keyboard(buttons)
        )
        return message.message_id

    return send


def nothing_to_business_chats(session: RecordingSession) -> None:
    """Инвариант этапа: в бизнес-чаты не уходит ничего — ни ответа, ни ошибки."""
    for method in session.sent:
        assert getattr(method, "business_connection_id", None) is None, method
        if isinstance(method, SendMessage):
            assert int(method.chat_id) == OWNER_ID, method


async def test_owner_connection_through_telegram_asks_consent_in_the_owner_chat(
    bot: Bot, session: RecordingSession
) -> None:
    service = chat_service(FakeChatStore(), sender=telegram_sender(bot))
    dispatcher = build_dispatcher(make_settings(), chats=service)

    await dispatcher.feed_update(bot, connection_update())

    assert session.texts == [texts.consent_question("telegram")]
    nothing_to_business_chats(session)


async def test_stranger_connection_through_telegram_gets_no_answer(
    bot: Bot, session: RecordingSession
) -> None:
    store = FakeChatStore()
    service = chat_service(store, sender=telegram_sender(bot))
    dispatcher = build_dispatcher(make_settings(), chats=service)

    await dispatcher.feed_update(bot, connection_update(user_id=999))

    assert session.sent == []
    assert store.calls == []


async def test_business_message_is_stored_silently(bot: Bot, session: RecordingSession) -> None:
    store = FakeChatStore()
    store.consent()
    dispatcher = build_dispatcher(
        make_settings(), chats=chat_service(store, sender=telegram_sender(bot))
    )

    await dispatcher.feed_update(bot, business_update(business_message()))
    owner = business_message("Да, пришлю", from_id=OWNER_ID, message_id=43)
    await dispatcher.feed_update(bot, business_update(owner, 2))

    assert [(message["direction"], message["text"]) for message in store.messages] == [
        ("in", "Пришлёшь расчёт в пятницу?"),
        ("out", "Да, пришлю"),
    ]
    assert session.sent == [], "ни ответа в чат, ни «чужим сюда нельзя»"


async def test_business_message_before_consent_is_lost_silently(
    bot: Bot, session: RecordingSession
) -> None:
    store = FakeChatStore()
    dispatcher = build_dispatcher(
        make_settings(), chats=chat_service(store, sender=telegram_sender(bot))
    )
    await dispatcher.feed_update(bot, connection_update())

    await dispatcher.feed_update(bot, business_update(business_message(), 2))

    assert store.messages == []
    assert session.texts == [texts.consent_question("telegram")]
    nothing_to_business_chats(session)


async def test_bot_written_business_message_is_skipped(bot: Bot, session: RecordingSession) -> None:
    store = FakeChatStore()
    store.consent()
    dispatcher = build_dispatcher(make_settings(), chats=chat_service(store))

    await dispatcher.feed_update(bot, business_update(business_message(by_bot=True)))

    assert store.calls == []
    assert session.sent == []


async def test_business_voice_is_downloaded_and_transcribed(
    bot: Bot, session: RecordingSession
) -> None:
    store = FakeChatStore()
    store.consent()
    transcriber = FakeTranscriber(Transcript(text="Во сколько созвон?", confidence=0.9))
    dispatcher = build_dispatcher(
        make_settings(), chats=chat_service(store, transcriber=transcriber)
    )

    await dispatcher.feed_update(bot, business_update(business_message(None, voice=True)))

    assert [message["text"] for message in store.messages] == ["Во сколько созвон?"]
    assert session.file_requests == ["voice-1"]
    assert session.texts == []
    nothing_to_business_chats(session)


async def test_edited_and_deleted_business_messages_reach_the_store(
    bot: Bot, session: RecordingSession
) -> None:
    store = FakeChatStore()
    store.consent()
    dispatcher = build_dispatcher(make_settings(), chats=chat_service(store))
    await dispatcher.feed_update(bot, business_update(business_message()))

    edited = business_message("Пришлёшь расчёт в четверг?")
    await dispatcher.feed_update(bot, Update(update_id=2, edited_business_message=edited))
    assert store.messages[0]["text"] == "Пришлёшь расчёт в четверг?"
    deleted = BusinessMessagesDeleted(
        business_connection_id=CONNECTION,
        chat=Chat(id=IGOR_ID, type="private", first_name="Игорь"),
        message_ids=[42],
    )
    await dispatcher.feed_update(bot, Update(update_id=3, deleted_business_messages=deleted))

    assert store.messages[0]["text"] == ""
    assert session.sent == []


async def test_business_updates_without_the_service_say_nothing(
    bot: Bot, session: RecordingSession
) -> None:
    dispatcher = build_dispatcher(make_settings())

    await dispatcher.feed_update(bot, connection_update(user_id=999))
    await dispatcher.feed_update(bot, business_update(business_message(), 2))

    assert session.sent == []


async def test_consent_button_edits_the_question(bot: Bot, session: RecordingSession) -> None:
    store = FakeChatStore()
    service = chat_service(store)
    await connect_owner(service)
    dispatcher = build_dispatcher(make_settings(), chats=service)

    await dispatcher.feed_update(
        bot, make_callback_update("consent:telegram:yes", text=texts.consent_question("telegram"))
    )

    assert store.sources["telegram"]["consented_at"] is not None
    assert [edit.text for edit in session.edits] == [
        texts.consent_answered("telegram", agreed=True)
    ]
    markup = session.edits[0].reply_markup
    assert isinstance(markup, InlineKeyboardMarkup)
    assert [row[0].text for row in markup.inline_keyboard] == [texts.CONSENT_STOP]


async def test_crooked_consent_button_is_a_popup(bot: Bot, session: RecordingSession) -> None:
    store = FakeChatStore()
    dispatcher = build_dispatcher(make_settings(), chats=chat_service(store))

    await dispatcher.feed_update(bot, make_callback_update("consent:vk:yes"))

    assert session.answers == [texts.DONE_UNKNOWN]
    assert store.calls == []


async def test_platform_without_a_connection_is_enabled_by_the_same_path() -> None:
    """Instagram и MAX (026, 027) включаются тем же входом: вопрос о согласии
    свой, хранение — после «Согласен»."""
    store = FakeChatStore()
    sender = FakeSender()
    service = chat_service(store, sender)

    await service.enable("max")
    await service.answer_consent("max", True)
    stored = await service.receive(
        Incoming(
            platform="max",
            connection_id=None,
            chat_key="notes",
            chat_name="MAX: заметки",
            external_id="m1",
            direction="out",
            sender="Тим",
            sent_at=SENT,
            kind="text",
            text="Олег вернёт книгу в среду",
        )
    )

    assert sender.texts == [texts.consent_question("max")]
    assert stored is not None and stored.outcome == "stored"
