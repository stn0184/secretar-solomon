"""Личные чаты (`techspec/25-chats.md`): база, приём, согласие, разбор,
сообщение владельцу, «ждёт ответа» и шаг тика.

Сети нет: клиент Supabase, модель, Deepgram и Telegram подменены. Переписки
в примерах выдуманные — Игорь и Олег.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest
from supabase import Client

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
from tests.conftest import OWNER_ID, OWNER_TIMEZONE
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
