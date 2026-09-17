"""Слой данных: фильтр по владельцу, вызов RPC и отказ вместо трассировки.

Сети здесь нет: клиент Supabase подменён записной книжкой, которая
запоминает, что именно у неё спросили.
"""

from __future__ import annotations

import inspect
from typing import Any, cast

import pytest
from supabase import Client

from solomon.db import tasks as db_tasks
from solomon.db.tasks import DatabaseError, Task

OWNER_ID = 777
ROW = {"id": "0e2f", "title": "купить лампочку", "status": "active"}


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


async def test_record_task_calls_rpc_with_whole_message() -> None:
    fake = FakeClient(data=ROW)

    task = await db_tasks.record_task(
        as_client(fake),
        owner_telegram_id=OWNER_ID,
        chat_id=42,
        telegram_message_id=7,
        text="купить лампочку",
    )

    assert task == Task(id="0e2f", title="купить лампочку", status="active")
    assert fake.calls[0] == (
        "rpc",
        "record_task",
        {
            "owner_telegram_id": OWNER_ID,
            "chat_id": 42,
            "telegram_message_id": 7,
            "text": "купить лампочку",
        },
    )


async def test_record_task_accepts_single_row_list() -> None:
    fake = FakeClient(data=[ROW])

    task = await db_tasks.record_task(
        as_client(fake),
        owner_telegram_id=OWNER_ID,
        chat_id=42,
        telegram_message_id=7,
        text="купить лампочку",
    )

    assert task.title == "купить лампочку"


async def test_record_task_without_row_is_a_failure() -> None:
    fake = FakeClient(data=None)

    with pytest.raises(DatabaseError):
        await db_tasks.record_task(
            as_client(fake),
            owner_telegram_id=OWNER_ID,
            chat_id=42,
            telegram_message_id=7,
            text="купить лампочку",
        )


async def test_client_error_becomes_database_error() -> None:
    fake = FakeClient(error=ConnectionError("no route to host"))

    with pytest.raises(DatabaseError) as failure:
        await db_tasks.record_task(
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


def test_owner_is_required_by_every_query() -> None:
    """Инвариант 2 держится сигнатурой: владельца не забыть и не подставить."""
    for query in (db_tasks.record_task, db_tasks.list_active_tasks):
        parameter = inspect.signature(query).parameters["owner_telegram_id"]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is inspect.Parameter.empty
