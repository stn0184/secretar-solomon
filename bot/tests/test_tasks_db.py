"""Слой данных: фильтр по владельцу, вызов RPC и отказ вместо трассировки.

Сети здесь нет: клиент Supabase подменён записной книжкой, которая
запоминает, что именно у неё спросили.
"""

from __future__ import annotations

import inspect
from datetime import datetime
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest
from supabase import Client

from solomon.db import reminders as db_reminders
from solomon.db import tasks as db_tasks
from solomon.db.rpc import DatabaseError
from solomon.db.tasks import SavedMessage, Task
from tests.conftest import OWNER_TIMEZONE

OWNER_ID = 777
TZ = ZoneInfo(OWNER_TIMEZONE)
ROW = {"id": "0e2f", "title": "купить лампочку", "status": "active"}
MESSAGE_ROW = {"id": "9a71", "text": "купить лампочку", "reply": None}
ANALYSIS = {"kind": "task", "title": "купить лампочку"}
TASK_FIELDS = {"title": "купить лампочку", "kind": "task", "needs_review": False}
REMINDER_ROWS = [{"stage": "before", "fire_at": "2026-09-25T09:00:00+05:00"}]
REMINDER_ROW = {
    "id": "b17c",
    "task_id": "0e2f",
    "stage": "due",
    "fire_at": "2026-09-25T18:00:00+05:00",
    "title": "купить лампочку",
    "due_at": "2026-09-25T18:00:00+05:00",
    "due_precision": "day",
}


class FakeResponse:
    def __init__(self, data: Any) -> None:
        self.data = data


class FakeQuery:
    """Цепочка postgrest: всё записывает и ничего не делает."""

    def __init__(self, client: FakeClient) -> None:
        self.client = client

    def select(self, *columns: str) -> FakeQuery:
        self.client.calls.append(("select", columns))
        return self

    def eq(self, column: str, value: Any) -> FakeQuery:
        self.client.calls.append(("eq", column, value))
        return self

    def order(self, column: str, *, desc: bool = False) -> FakeQuery:
        self.client.calls.append(("order", column, desc))
        return self

    def limit(self, size: int) -> FakeQuery:
        self.client.calls.append(("limit", size))
        return self

    def execute(self) -> FakeResponse:
        if self.client.error is not None:
            raise self.client.error
        return FakeResponse(self.client.data)


class FakeClient:
    def __init__(self, data: Any = None, error: Exception | None = None) -> None:
        self.data = data
        self.error = error
        self.calls: list[tuple[Any, ...]] = []

    def table(self, name: str) -> FakeQuery:
        self.calls.append(("table", name))
        return FakeQuery(self)

    def rpc(self, function: str, params: dict[str, Any]) -> FakeQuery:
        self.calls.append(("rpc", function, params))
        return FakeQuery(self)


def as_client(fake: FakeClient) -> Client:
    return cast(Client, fake)


async def test_record_message_calls_rpc_with_whole_text() -> None:
    fake = FakeClient(data=MESSAGE_ROW)

    saved = await db_tasks.record_message(
        as_client(fake),
        owner_telegram_id=OWNER_ID,
        chat_id=42,
        telegram_message_id=7,
        text="купить лампочку",
    )

    assert saved == SavedMessage(id="9a71", reply=None)
    assert fake.calls[0] == (
        "rpc",
        "record_message",
        {
            "owner_telegram_id": OWNER_ID,
            "chat_id": 42,
            "telegram_message_id": 7,
            "text": "купить лампочку",
        },
    )


async def test_record_message_returns_the_reply_already_given() -> None:
    """Повтор обновления: ответ бота лежит в строке, и звать модель незачем."""
    fake = FakeClient(data=[{**MESSAGE_ROW, "reply": "Записал: купить лампочку"}])

    saved = await db_tasks.record_message(
        as_client(fake),
        owner_telegram_id=OWNER_ID,
        chat_id=42,
        telegram_message_id=7,
        text="купить лампочку",
    )

    assert saved.reply == "Записал: купить лампочку"


async def test_record_message_without_row_is_a_failure() -> None:
    fake = FakeClient(data=None)

    with pytest.raises(DatabaseError):
        await db_tasks.record_message(
            as_client(fake),
            owner_telegram_id=OWNER_ID,
            chat_id=42,
            telegram_message_id=7,
            text="купить лампочку",
        )


async def test_record_understanding_sends_analysis_and_task() -> None:
    fake = FakeClient(data=ROW)

    task = await db_tasks.record_understanding(
        as_client(fake),
        message_id="9a71",
        owner_telegram_id=OWNER_ID,
        analysis=ANALYSIS,
        ai_model="claude-opus-5",
        ai_input_tokens=120,
        ai_output_tokens=45,
        reply="Записал: купить лампочку",
        task=TASK_FIELDS,
        reminders=REMINDER_ROWS,
    )

    assert task == Task(id="0e2f", title="купить лампочку", status="active")
    assert fake.calls[0] == (
        "rpc",
        "record_understanding",
        {
            "message_id": "9a71",
            "owner_telegram_id": OWNER_ID,
            "analysis": ANALYSIS,
            "ai_model": "claude-opus-5",
            "ai_input_tokens": 120,
            "ai_output_tokens": 45,
            "reply": "Записал: купить лампочку",
            "task": TASK_FIELDS,
            "reminders": REMINDER_ROWS,
        },
    )


async def test_record_understanding_without_task_returns_nothing() -> None:
    """Разговор: разбор записан, задачи нет — и это не отказ базы."""
    fake = FakeClient(data=None)

    task = await db_tasks.record_understanding(
        as_client(fake),
        message_id="9a71",
        owner_telegram_id=OWNER_ID,
        analysis=ANALYSIS,
        ai_model="claude-opus-5",
        ai_input_tokens=120,
        ai_output_tokens=45,
        reply="Это не похоже на поручение",
        task=None,
        reminders=[],
    )

    assert task is None


async def test_record_understanding_ignores_empty_composite_row() -> None:
    """PostgREST может отдать пустую строку составного типа вместо null."""
    fake = FakeClient(data={"id": None, "title": None, "status": None})

    task = await db_tasks.record_understanding(
        as_client(fake),
        message_id="9a71",
        owner_telegram_id=OWNER_ID,
        analysis=ANALYSIS,
        ai_model="claude-opus-5",
        ai_input_tokens=120,
        ai_output_tokens=45,
        reply="Это не похоже на поручение",
        task=None,
        reminders=[],
    )

    assert task is None


async def test_client_error_becomes_database_error() -> None:
    fake = FakeClient(error=ConnectionError("no route to host"))

    with pytest.raises(DatabaseError) as failure:
        await db_tasks.record_message(
            as_client(fake),
            owner_telegram_id=OWNER_ID,
            chat_id=42,
            telegram_message_id=7,
            text="купить лампочку",
        )

    assert "ConnectionError" in str(failure.value)


async def test_active_tasks_are_asked_for_this_owner_only() -> None:
    fake = FakeClient(data=[ROW])

    found = await db_tasks.list_active_tasks(as_client(fake), owner_telegram_id=OWNER_ID, limit=101)

    assert [task.title for task in found] == ["купить лампочку"]
    assert ("eq", "owner_telegram_id", OWNER_ID) in fake.calls
    assert ("eq", "status", "active") in fake.calls
    assert ("order", "created_at", True) in fake.calls
    assert ("limit", 101) in fake.calls


async def test_broken_row_is_a_failure_not_a_half_task() -> None:
    fake = FakeClient(data=[{"id": "0e2f"}])

    with pytest.raises(DatabaseError):
        await db_tasks.list_active_tasks(as_client(fake), owner_telegram_id=OWNER_ID, limit=101)


async def test_due_reminders_asks_about_the_owner_and_the_moment() -> None:
    """Отбор созревших: владелец и «сейчас» уходят в функцию явно (§6.2)."""
    fake = FakeClient(data=[REMINDER_ROW])
    now = datetime(2026, 9, 25, 18, 0, tzinfo=TZ)

    due = await db_reminders.due_reminders(as_client(fake), owner_telegram_id=OWNER_ID, now=now)

    assert fake.calls[0] == (
        "rpc",
        "due_reminders",
        {"owner_telegram_id": OWNER_ID, "now": now.isoformat()},
    )
    assert due[0].task_id == "0e2f"
    assert due[0].stage == "due"
    assert due[0].fire_at == datetime(2026, 9, 25, 18, 0, tzinfo=TZ)
    assert due[0].title == "купить лампочку"


async def test_due_reminders_without_rows_is_an_empty_tick() -> None:
    fake = FakeClient(data=None)

    assert (
        await db_reminders.due_reminders(
            as_client(fake), owner_telegram_id=OWNER_ID, now=datetime.now(TZ)
        )
        == []
    )


async def test_broken_reminder_row_is_a_failure() -> None:
    """Нет полей задачи — отказ: напоминание без сути отправлять нечего."""
    fake = FakeClient(data=[{"id": "b17c", "task_id": "0e2f"}])

    with pytest.raises(DatabaseError):
        await db_reminders.due_reminders(
            as_client(fake), owner_telegram_id=OWNER_ID, now=datetime.now(TZ)
        )


async def test_mark_sent_passes_ids_and_the_telegram_message() -> None:
    fake = FakeClient(data=None)

    await db_reminders.mark_sent(
        as_client(fake),
        owner_telegram_id=OWNER_ID,
        reminder_ids=["b17c"],
        telegram_message_id=51,
    )

    assert fake.calls[0] == (
        "rpc",
        "mark_reminders_sent",
        {"owner_telegram_id": OWNER_ID, "ids": ["b17c"], "telegram_message_id": 51},
    )


async def test_mark_task_done_returns_the_closed_task() -> None:
    fake = FakeClient(data={**ROW, "status": "done"})

    task = await db_reminders.mark_task_done(
        as_client(fake), owner_telegram_id=OWNER_ID, task_id="0e2f"
    )

    assert fake.calls[0] == (
        "rpc",
        "mark_task_done",
        {"owner_telegram_id": OWNER_ID, "task_id": "0e2f"},
    )
    assert task == Task(id="0e2f", title="купить лампочку", status="done")


async def test_mark_task_done_returns_nothing_for_a_foreign_task() -> None:
    """Чужой или выдуманный `task_id`: база не нашла, и закрывать нечего (§6.3)."""
    fake = FakeClient(data={"id": None, "title": None, "status": None})

    task = await db_reminders.mark_task_done(
        as_client(fake), owner_telegram_id=OWNER_ID, task_id="0e2f"
    )

    assert task is None


def test_owner_is_required_by_every_query() -> None:
    """Инвариант 2 держится сигнатурой: владельца не забыть и не подставить."""
    for query in (
        db_tasks.record_message,
        db_tasks.record_understanding,
        db_tasks.list_active_tasks,
        db_reminders.due_reminders,
        db_reminders.mark_sent,
        db_reminders.mark_task_done,
    ):
        parameter = inspect.signature(query).parameters["owner_telegram_id"]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is inspect.Parameter.empty
