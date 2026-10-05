"""Слой данных: фильтр по владельцу, вызов RPC и отказ вместо трассировки.

Сети здесь нет: клиент Supabase подменён записной книжкой, которая
запоминает, что именно у неё спросили.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest
from supabase import Client

from solomon.db import facts as db_facts
from solomon.db import reminders as db_reminders
from solomon.db import tasks as db_tasks
from solomon.db.facts import Fact
from solomon.db.reminders import Planned
from solomon.db.rpc import DatabaseError
from solomon.db.tasks import (
    OpenQuestion,
    PickedMessage,
    RecentMessage,
    SavedMessage,
    StoredMessage,
    Task,
    TaskDetails,
    TaskEvent,
)
from tests.conftest import OWNER_TIMEZONE

OWNER_ID = 777
TZ = ZoneInfo(OWNER_TIMEZONE)
ROW = {"id": "0e2f", "title": "купить лампочку", "status": "active"}
MESSAGE_ROW = {"id": "9a71", "text": "купить лампочку", "reply": None}
ANALYSIS = {"kind": "task", "title": "купить лампочку"}
TASK_FIELDS = {"title": "купить лампочку", "kind": "task", "needs_review": False}
REMINDER_ROWS = [{"stage": "before", "fire_at": "2026-09-25T09:00:00+05:00"}]
FACT_ROWS = [{"category": "car", "text": "Машина — Toyota Camry", "status": "fact"}]
FACT_ROW = {"id": "f1", "category": "car", "text": "Машина — Toyota Camry", "status": "fact"}
AMEND = {
    "task_id": "0e2f",
    "fields": {
        "due_at": "2026-09-25T18:00:00+05:00",
        "due_precision": "day",
        "needs_review": False,
    },
    "reminders": REMINDER_ROWS,
}
ASKED_AT = datetime(2026, 9, 24, 12, 0, tzinfo=TZ)
QUESTION_ROW = {
    "id": "0e2f",
    "title": "отправить расчёт клиенту",
    "kind": "task",
    "due_at": None,
    "due_precision": None,
    "priority": "high",
    "promise": "mine",
    "people": ["клиент"],
    "open_question": "К какому сроку?",
    "question_asked_at": "2026-09-24T12:00:00+05:00",
}
MOVED_ROW = {
    "id": "0e2f",
    "title": "отправить расчёт клиенту",
    "due_at": "2026-10-02T13:00:00+00:00",
    "due_precision": "day",
    "due_moved_at": "2026-09-28T07:15:42.123456+00:00",
    "next_fire_at": "2026-10-02T04:00:00+00:00",
}
TASK_ID = "5b0c7a52-8f3e-4c1d-9a6b-2e4f1d3c8b90"
DETAIL_ROW = {
    "id": TASK_ID,
    "title": "встреча с Ренатой",
    "kind": "task",
    "status": "active",
    "due_at": "2026-10-02T17:00:00+05:00",
    "due_precision": "time",
    "priority": "high",
    "promise": None,
    "people": ["Рената"],
    "created_at": "2026-09-28T10:00:00+05:00",
}
DETAILS = TaskDetails(
    id=TASK_ID,
    title="встреча с Ренатой",
    kind="task",
    status="active",
    due_at=datetime(2026, 10, 2, 17, 0, tzinfo=TZ),
    due_precision="time",
    priority="high",
    promise=None,
    people=("Рената",),
    created_at=datetime(2026, 9, 28, 10, 0, tzinfo=TZ),
)
# «Каждый понедельник» — правило, как его отдаёт база (§13.2).
WEEKLY: dict[str, Any] = {
    "every": "week",
    "interval": 1,
    "weekdays": [1],
    "month_day": None,
    "month": None,
    "time": None,
}
REPEAT_ROW = {
    **DETAIL_ROW,
    "due_at": "2026-10-12T18:00:00+05:00",
    "due_precision": "day",
    "repeat": WEEKLY,
    "occurrence_at": "2026-10-12T18:00:00+05:00",
}
EDIT = {
    "task_id": TASK_ID,
    "action": "change",
    "changes": {"due_date": "2026-10-02"},
    "schedule": REMINDER_ROWS,
    "question": None,
}
STORED_ROW: dict[str, Any] = {
    "id": "9a71",
    "text": "перенеси встречу на пятницу",
    "task_id": None,
    "analysis": {"kind": "chat", "edit": {"action": "change", "candidates": [1, 2]}},
    "reply": "Какую задачу перенести на пятницу, 2 октября?",
}
SINCE = datetime(2026, 9, 29, 11, 0, tzinfo=TZ)
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

    def __init__(self, client: FakeClient, table: str | None = None) -> None:
        self.client = client
        self.table = table
        self.negate = False

    def select(self, *columns: str) -> FakeQuery:
        self.client.calls.append(("select", columns))
        return self

    def eq(self, column: str, value: Any) -> FakeQuery:
        self.client.calls.append(("eq", column, value))
        return self

    def order(
        self, column: str, *, desc: bool = False, nullsfirst: bool | None = None
    ) -> FakeQuery:
        if nullsfirst is None:
            self.client.calls.append(("order", column, desc))
        else:
            self.client.calls.append(("order", column, desc, nullsfirst))
        return self

    @property
    def not_(self) -> FakeQuery:
        self.negate = True
        return self

    def is_(self, column: str, value: Any) -> FakeQuery:
        self.client.calls.append(("not.is" if self.negate else "is", column, value))
        self.negate = False
        return self

    def gte(self, column: str, value: Any) -> FakeQuery:
        self.client.calls.append(("gte", column, value))
        return self

    def in_(self, column: str, values: Any) -> FakeQuery:
        self.client.calls.append(("in", column, tuple(values)))
        return self

    def lt(self, column: str, value: Any) -> FakeQuery:
        self.client.calls.append(("lt", column, value))
        return self

    def neq(self, column: str, value: Any) -> FakeQuery:
        self.client.calls.append(("neq", column, value))
        return self

    def limit(self, size: int) -> FakeQuery:
        self.client.calls.append(("limit", size))
        return self

    def execute(self) -> FakeResponse:
        if self.client.error is not None:
            raise self.client.error
        if self.table is not None and self.table in self.client.tables:
            return FakeResponse(self.client.tables[self.table])
        return FakeResponse(self.client.data)


class FakeClient:
    """Ответ один на все выборки; `tables` — свой ответ для выборок из таблицы."""

    def __init__(
        self,
        data: Any = None,
        error: Exception | None = None,
        tables: dict[str, Any] | None = None,
    ) -> None:
        self.data = data
        self.error = error
        self.tables = tables or {}
        self.calls: list[tuple[Any, ...]] = []

    def table(self, name: str) -> FakeQuery:
        self.calls.append(("table", name))
        return FakeQuery(self, name)

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
            "kind": "text",
            "telegram_file_id": None,
            "duration_seconds": None,
            "forwarded_from": None,
        },
    )


async def test_record_message_sends_kind_file_and_duration_for_voice() -> None:
    """Голос пишется до расшифровки: вид, файл и длительность, текст пустой (§9.3)."""
    fake = FakeClient(data={**MESSAGE_ROW, "text": "", "kind": "voice"})

    saved = await db_tasks.record_message(
        as_client(fake),
        owner_telegram_id=OWNER_ID,
        chat_id=42,
        telegram_message_id=7,
        text="",
        kind="voice",
        telegram_file_id="voice-1",
        duration_seconds=32,
    )

    assert saved == SavedMessage(id="9a71", reply=None)
    assert fake.calls[0] == (
        "rpc",
        "record_message",
        {
            "owner_telegram_id": OWNER_ID,
            "chat_id": 42,
            "telegram_message_id": 7,
            "text": "",
            "kind": "voice",
            "telegram_file_id": "voice-1",
            "duration_seconds": 32,
            "forwarded_from": None,
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


async def test_record_message_sends_the_sender_of_a_forwarded_message() -> None:
    """У пересланного в строку ложится отправитель (§17.5): блок 6 назовёт его."""
    fake = FakeClient(data={**MESSAGE_ROW, "forwarded_from": "Рената"})

    await db_tasks.record_message(
        as_client(fake),
        owner_telegram_id=OWNER_ID,
        chat_id=42,
        telegram_message_id=7,
        text="Во сколько?",
        forwarded_from="Рената",
    )

    assert fake.calls[0][2]["forwarded_from"] == "Рената"


async def test_record_message_reads_when_the_message_was_received() -> None:
    """Время строки — верхняя граница недавнего разговора (§17.3)."""
    fake = FakeClient(data={**MESSAGE_ROW, "received_at": "2026-09-29T11:40:00+05:00"})

    saved = await db_tasks.record_message(
        as_client(fake),
        owner_telegram_id=OWNER_ID,
        chat_id=42,
        telegram_message_id=7,
        text="купить лампочку",
    )

    assert saved == SavedMessage(
        id="9a71", reply=None, received_at=datetime(2026, 9, 29, 11, 40, tzinfo=TZ)
    )


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

    tasks = await db_tasks.record_understanding(
        as_client(fake),
        message_id="9a71",
        owner_telegram_id=OWNER_ID,
        analysis=ANALYSIS,
        ai_model="claude-opus-5",
        ai_input_tokens=120,
        ai_output_tokens=45,
        reply="Записал: купить лампочку",
        tasks=[{"item": 1, "task": TASK_FIELDS, "reminders": REMINDER_ROWS}],
        facts=FACT_ROWS,
    )

    assert tasks == [Task(id="0e2f", title="купить лампочку", status="active")]
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
            "tasks": [{"item": 1, "task": TASK_FIELDS, "reminders": REMINDER_ROWS}],
            "facts": FACT_ROWS,
            "transcript": None,
            "transcript_confidence": None,
            "amend": None,
            "edit": None,
        },
    )


async def test_record_understanding_sends_several_tasks_and_returns_them_all() -> None:
    """Несколько дел (`techspec/23-several-tasks.md` §23.6): массив дел с номерами
    — одним вызовом; база отдаёт записанные задачи списком."""
    second = {**TASK_FIELDS, "title": "забрать костюм из химчистки"}
    fake = FakeClient(data=[ROW, {**ROW, "id": "7c4d", "title": "забрать костюм из химчистки"}])
    listed = [
        {"item": 1, "task": TASK_FIELDS, "reminders": REMINDER_ROWS},
        {"item": 3, "task": second, "reminders": []},
    ]

    tasks = await db_tasks.record_understanding(
        as_client(fake),
        message_id="9a71",
        owner_telegram_id=OWNER_ID,
        analysis=ANALYSIS,
        ai_model="claude-opus-5",
        ai_input_tokens=120,
        ai_output_tokens=90,
        reply="Записал:\n1. Купить лампочку\n2. Забрать костюм из химчистки",
        tasks=listed,
        facts=[],
    )

    assert fake.calls[0][2]["tasks"] == listed
    assert [task.id for task in tasks] == ["0e2f", "7c4d"]


async def test_record_understanding_broken_task_row_is_a_failure() -> None:
    fake = FakeClient(data=[{"id": "0e2f"}])

    with pytest.raises(DatabaseError):
        await db_tasks.record_understanding(
            as_client(fake),
            message_id="9a71",
            owner_telegram_id=OWNER_ID,
            analysis=ANALYSIS,
            ai_model="claude-opus-5",
            ai_input_tokens=120,
            ai_output_tokens=45,
            reply="Записал: купить лампочку",
            tasks=[{"item": 1, "task": TASK_FIELDS, "reminders": []}],
            facts=[],
        )


async def test_record_understanding_sends_the_edit_instead_of_a_task() -> None:
    """Правка словом (§12.4): ни новой задачи, ни поправки по вопросу."""
    fake = FakeClient(data=ROW)

    tasks = await db_tasks.record_understanding(
        as_client(fake),
        message_id="9a72",
        owner_telegram_id=OWNER_ID,
        analysis=ANALYSIS,
        ai_model="claude-opus-5",
        ai_input_tokens=120,
        ai_output_tokens=45,
        reply="Перенёс: встреча с Ренатой",
        tasks=[],
        facts=[],
        edit=EDIT,
    )

    assert tasks == [Task(id="0e2f", title="купить лампочку", status="active")]
    params = fake.calls[0][2]
    assert params["tasks"] == []
    assert params["amend"] is None
    assert params["edit"] == EDIT


async def test_record_understanding_sends_the_amendment_instead_of_a_task() -> None:
    """Ответ на вопрос дополняет прежнюю задачу (§10.2): новой в запросе нет."""
    fake = FakeClient(data=ROW)

    tasks = await db_tasks.record_understanding(
        as_client(fake),
        message_id="9a72",
        owner_telegram_id=OWNER_ID,
        analysis=ANALYSIS,
        ai_model="claude-opus-5",
        ai_input_tokens=120,
        ai_output_tokens=45,
        reply="Понял: купить лампочку",
        tasks=[],
        facts=[],
        amend=AMEND,
    )

    assert tasks == [Task(id="0e2f", title="купить лампочку", status="active")]
    params = fake.calls[0][2]
    assert params["tasks"] == []
    assert params["amend"] == AMEND


async def test_record_understanding_sends_the_transcript_and_its_confidence() -> None:
    """Расшифровка ложится тем же вызовом, что разбор и задача (§9.3)."""
    fake = FakeClient(data=ROW)

    await db_tasks.record_understanding(
        as_client(fake),
        message_id="9a71",
        owner_telegram_id=OWNER_ID,
        analysis=ANALYSIS,
        ai_model="claude-opus-5",
        ai_input_tokens=120,
        ai_output_tokens=45,
        reply="Записал: купить лампочку",
        tasks=[{"item": 1, "task": TASK_FIELDS, "reminders": []}],
        facts=[],
        transcript="купить лампочку",
        transcript_confidence=0.93,
    )

    params = fake.calls[0][2]
    assert params["transcript"] == "купить лампочку"
    assert params["transcript_confidence"] == 0.93


async def test_record_message_sends_the_photo_with_its_caption() -> None:
    """Снимок пишется до разбора: вид, файл и подпись, длительности нет (§14.2)."""
    fake = FakeClient(data={**MESSAGE_ROW, "text": "купить такие же", "kind": "photo"})

    await db_tasks.record_message(
        as_client(fake),
        owner_telegram_id=OWNER_ID,
        chat_id=42,
        telegram_message_id=7,
        text="купить такие же",
        kind="photo",
        telegram_file_id="photo-1",
    )

    params = fake.calls[0][2]
    assert params["kind"] == "photo"
    assert params["telegram_file_id"] == "photo-1"
    assert params["text"] == "купить такие же"
    assert params["duration_seconds"] is None


async def test_record_understanding_sends_what_was_read_from_the_photo() -> None:
    """Прочитанное со снимка ложится тем же вызовом, что разбор и задача (§14.2)."""
    fake = FakeClient(data=ROW)

    await db_tasks.record_understanding(
        as_client(fake),
        message_id="9a71",
        owner_telegram_id=OWNER_ID,
        analysis=ANALYSIS,
        ai_model="claude-opus-5",
        ai_input_tokens=1900,
        ai_output_tokens=310,
        reply="Записал: купить лампочку",
        tasks=[{"item": 1, "task": TASK_FIELDS, "reminders": []}],
        facts=[],
        photo_text="Этикетка лампочки: цоколь E14, 7 Вт.",
    )

    params = fake.calls[0][2]
    assert params["photo_text"] == "Этикетка лампочки: цоколь E14, 7 Вт."


async def test_record_understanding_without_photo_text_calls_the_function_as_before() -> None:
    """Текст и голос зовут функцию без `photo_text`: так они пишутся и на старой базе."""
    fake = FakeClient(data=ROW)

    await db_tasks.record_understanding(
        as_client(fake),
        message_id="9a71",
        owner_telegram_id=OWNER_ID,
        analysis=ANALYSIS,
        ai_model="claude-opus-5",
        ai_input_tokens=120,
        ai_output_tokens=45,
        reply="Записал: купить лампочку",
        tasks=[{"item": 1, "task": TASK_FIELDS, "reminders": []}],
        facts=[],
    )

    assert "photo_text" not in fake.calls[0][2]


async def test_record_understanding_sends_the_found_task_of_a_duplicate() -> None:
    """Дубль (§15.3): задачи нет, сообщение ведёт на найденную — тем же вызовом."""
    fake = FakeClient(data=ROW)

    tasks = await db_tasks.record_understanding(
        as_client(fake),
        message_id="9a71",
        owner_telegram_id=OWNER_ID,
        analysis=ANALYSIS,
        ai_model="claude-opus-5",
        ai_input_tokens=120,
        ai_output_tokens=45,
        reply="Это уже записано: купить лампочку.",
        tasks=[],
        facts=[],
        same_task=TASK_ID,
    )

    params = fake.calls[0][2]
    assert params["same_task"] == TASK_ID
    assert params["tasks"] == []
    assert tasks == [Task(id="0e2f", title="купить лампочку", status="active")]


async def test_record_understanding_without_a_duplicate_calls_the_function_as_before() -> None:
    """Без дубля `same_task` в запрос не уходит: так вызов работает и до миграции 013."""
    fake = FakeClient(data=ROW)

    await db_tasks.record_understanding(
        as_client(fake),
        message_id="9a71",
        owner_telegram_id=OWNER_ID,
        analysis=ANALYSIS,
        ai_model="claude-opus-5",
        ai_input_tokens=120,
        ai_output_tokens=45,
        reply="Записал: купить лампочку",
        tasks=[{"item": 1, "task": TASK_FIELDS, "reminders": []}],
        facts=[],
    )

    assert "same_task" not in fake.calls[0][2]


async def test_record_understanding_without_task_returns_nothing() -> None:
    """Разговор: разбор записан, задачи нет — и это не отказ базы."""
    fake = FakeClient(data=[])

    tasks = await db_tasks.record_understanding(
        as_client(fake),
        message_id="9a71",
        owner_telegram_id=OWNER_ID,
        analysis=ANALYSIS,
        ai_model="claude-opus-5",
        ai_input_tokens=120,
        ai_output_tokens=45,
        reply="Это не похоже на поручение",
        tasks=[],
        facts=[],
    )

    assert tasks == []


async def test_record_understanding_ignores_empty_composite_row() -> None:
    """PostgREST может отдать пустую строку составного типа вместо null."""
    fake = FakeClient(data={"id": None, "title": None, "status": None})

    tasks = await db_tasks.record_understanding(
        as_client(fake),
        message_id="9a71",
        owner_telegram_id=OWNER_ID,
        analysis=ANALYSIS,
        ai_model="claude-opus-5",
        ai_input_tokens=120,
        ai_output_tokens=45,
        reply="Это не похоже на поручение",
        tasks=[],
        facts=[],
    )

    assert tasks == []


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


async def test_due_reminder_carries_the_rule_and_the_occurrence() -> None:
    """Раз уходит в кнопку «Сделано» (§13.3); у разовой оба поля пусты."""
    fake = FakeClient(
        data=[
            {**REMINDER_ROW, "repeat": WEEKLY, "occurrence_at": "2026-09-25T18:00:00+05:00"},
            {**REMINDER_ROW, "id": "c28d", "repeat": None, "occurrence_at": None},
        ]
    )

    due = await db_reminders.due_reminders(
        as_client(fake), owner_telegram_id=OWNER_ID, now=datetime.now(TZ)
    )

    assert due[0].repeat == WEEKLY
    assert due[0].occurrence_at == datetime(2026, 9, 25, 18, 0, tzinfo=TZ)
    assert due[1].repeat is None
    assert due[1].occurrence_at is None


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
    """Кнопка без раза (§6.3): раз в базу не уходит — старый вызов работает."""
    fake = FakeClient(data={**DETAIL_ROW, "status": "done"})

    task = await db_reminders.mark_task_done(
        as_client(fake), owner_telegram_id=OWNER_ID, task_id=TASK_ID
    )

    assert fake.calls[0] == (
        "rpc",
        "mark_task_done",
        {"owner_telegram_id": OWNER_ID, "task_id": TASK_ID},
    )
    assert task is not None
    assert task.status == "done"
    assert task.repeat is None


async def test_mark_task_done_sends_the_occurrence_and_reads_the_next_one() -> None:
    """Кнопка с разом (§13.3): раз — секунды Unix; ответ — задача на следующем разе."""
    fake = FakeClient(data=[REPEAT_ROW])

    task = await db_reminders.mark_task_done(
        as_client(fake), owner_telegram_id=OWNER_ID, task_id=TASK_ID, occurrence=1790002800
    )

    assert fake.calls[0] == (
        "rpc",
        "mark_task_done",
        {"owner_telegram_id": OWNER_ID, "task_id": TASK_ID, "occurrence": 1790002800},
    )
    assert task is not None
    assert task.status == "active"
    assert task.repeat == WEEKLY
    assert task.occurrence_at == datetime(2026, 10, 12, 18, 0, tzinfo=TZ)


async def test_mark_task_done_returns_nothing_for_a_foreign_task() -> None:
    """Чужой или выдуманный `task_id`: база не нашла, и закрывать нечего (§6.3)."""
    fake = FakeClient(data={"id": None, "title": None, "status": None})

    task = await db_reminders.mark_task_done(
        as_client(fake), owner_telegram_id=OWNER_ID, task_id="0e2f"
    )

    assert task is None


async def test_moved_tasks_are_asked_for_this_owner() -> None:
    """Строки «Перенёс»: владелец уходит в функцию явно (§11.4, инвариант 2)."""
    fake = FakeClient(data=[MOVED_ROW])

    moved = await db_reminders.moved_tasks(as_client(fake), owner_telegram_id=OWNER_ID)

    assert fake.calls[0] == ("rpc", "moved_tasks", {"owner_telegram_id": OWNER_ID})
    task = moved[0]
    assert task.id == "0e2f"
    assert task.title == "отправить расчёт клиенту"
    assert task.due_at == datetime(2026, 10, 2, 18, 0, tzinfo=TZ)
    assert task.due_precision == "day"
    assert task.due_moved_at == datetime.fromisoformat("2026-09-28T07:15:42.123456+00:00")
    assert task.next_fire_at == datetime(2026, 10, 2, 9, 0, tzinfo=TZ)


async def test_moved_task_carries_the_repeat() -> None:
    """У повторяющейся задачи строка «Перенёс» называет повтор (§13.6)."""
    fake = FakeClient(
        data=[{**MOVED_ROW, "repeat": WEEKLY, "occurrence_at": "2026-10-05T13:00:00+00:00"}]
    )

    moved = await db_reminders.moved_tasks(as_client(fake), owner_telegram_id=OWNER_ID)

    assert moved[0].repeat == WEEKLY
    assert moved[0].occurrence_at == datetime(2026, 10, 5, 18, 0, tzinfo=TZ)


async def test_moved_task_without_due_has_no_reminder() -> None:
    """Срок снят: ни срока, ни ближайшего напоминания — и это не отказ."""
    fake = FakeClient(
        data=[{**MOVED_ROW, "due_at": None, "due_precision": None, "next_fire_at": None}]
    )

    moved = await db_reminders.moved_tasks(as_client(fake), owner_telegram_id=OWNER_ID)

    assert moved[0].due_at is None
    assert moved[0].due_precision is None
    assert moved[0].next_fire_at is None


async def test_no_moved_tasks_is_an_empty_list() -> None:
    fake = FakeClient(data=None)

    assert await db_reminders.moved_tasks(as_client(fake), owner_telegram_id=OWNER_ID) == []


async def test_moved_row_without_the_mark_is_a_failure() -> None:
    """Без отметки снимать нечего — отказ, а не строка, которую не погасить."""
    row = {key: value for key, value in MOVED_ROW.items() if key != "due_moved_at"}
    fake = FakeClient(data=[row])

    with pytest.raises(DatabaseError):
        await db_reminders.moved_tasks(as_client(fake), owner_telegram_id=OWNER_ID)


async def test_repeat_next_asks_the_database_and_reads_the_moment() -> None:
    """Следующий раз считает база (§13.2): правило, раз, «после» и пояс уходят явно."""
    fake = FakeClient(data="2026-10-12T13:00:00+00:00")
    occurrence = datetime(2026, 10, 5, 18, 0, tzinfo=TZ)
    after = datetime(2026, 10, 5, 20, 0, tzinfo=TZ)

    found = await db_reminders.repeat_next(
        as_client(fake),
        repeat=WEEKLY,
        occurrence_at=occurrence,
        after=after,
        timezone="Asia/Yekaterinburg",
    )

    assert fake.calls[0] == (
        "rpc",
        "repeat_next",
        {
            "repeat": WEEKLY,
            "occurrence_at": occurrence.isoformat(),
            "after": after.isoformat(),
            "timezone": "Asia/Yekaterinburg",
        },
    )
    assert found == datetime(2026, 10, 12, 18, 0, tzinfo=TZ)


async def test_repeat_next_without_an_answer_is_a_failure() -> None:
    """Пусто — правило не посчиталось: «Следующий раз» назвать нечем."""
    fake = FakeClient(data=None)

    with pytest.raises(DatabaseError):
        await db_reminders.repeat_next(
            as_client(fake),
            repeat=WEEKLY,
            occurrence_at=datetime.now(TZ),
            after=datetime.now(TZ),
            timezone="Asia/Yekaterinburg",
        )


async def test_return_occurrence_sends_both_moments_and_the_plan() -> None:
    """«Вернуть» (§13.3): владелец явно, разы — секунды Unix, план — готовый."""
    fake = FakeClient(data=REPEAT_ROW)
    planned = [Planned(stage="due", fire_at=datetime(2026, 10, 12, 18, 0, tzinfo=TZ))]

    task = await db_reminders.return_occurrence(
        as_client(fake),
        owner_telegram_id=OWNER_ID,
        task_id=TASK_ID,
        back_to=1790002800,
        moved_from=1790607600,
        schedule=planned,
    )

    assert fake.calls[0] == (
        "rpc",
        "return_occurrence",
        {
            "owner_telegram_id": OWNER_ID,
            "task_id": TASK_ID,
            "back_to": 1790002800,
            "moved_from": 1790607600,
            "schedule": [item.as_row() for item in planned],
        },
    )
    assert task is not None
    assert task.occurrence_at == datetime(2026, 10, 12, 18, 0, tzinfo=TZ)


async def test_return_occurrence_of_a_gone_task_is_none() -> None:
    fake = FakeClient(data={"id": None})

    assert (
        await db_reminders.return_occurrence(
            as_client(fake),
            owner_telegram_id=OWNER_ID,
            task_id=TASK_ID,
            back_to=1,
            moved_from=2,
            schedule=[],
        )
        is None
    )


async def test_roll_repeats_asks_for_the_owner_and_counts_the_moved() -> None:
    """Перекатывание (§13.4): владелец и «сейчас» — явно; ответ — сколько перешло."""
    fake = FakeClient(data=[REPEAT_ROW, REPEAT_ROW])
    now = datetime(2026, 10, 13, 0, 1, tzinfo=TZ)

    rolled = await db_reminders.roll_repeats(as_client(fake), owner_telegram_id=OWNER_ID, now=now)

    assert fake.calls[0] == (
        "rpc",
        "roll_repeats",
        {"owner_telegram_id": OWNER_ID, "now": now.isoformat()},
    )
    assert rolled == 2


async def test_roll_repeats_without_rows_moved_nothing() -> None:
    fake = FakeClient(data=None)

    assert (
        await db_reminders.roll_repeats(
            as_client(fake), owner_telegram_id=OWNER_ID, now=datetime.now(TZ)
        )
        == 0
    )


async def test_clear_due_moved_sends_the_seen_mark_back() -> None:
    """Снимается ровно прочитанная отметка: момент уходит обратно без потерь."""
    fake = FakeClient(data=True)
    seen = datetime.fromisoformat(MOVED_ROW["due_moved_at"])

    cleared = await db_reminders.clear_due_moved(
        as_client(fake), owner_telegram_id=OWNER_ID, task_id="0e2f", seen=seen
    )

    assert cleared is True
    assert fake.calls[0] == (
        "rpc",
        "clear_due_moved",
        {"owner_telegram_id": OWNER_ID, "task_id": "0e2f", "seen": seen.isoformat()},
    )
    assert datetime.fromisoformat(seen.isoformat()) == seen


async def test_clear_due_moved_reports_a_changed_mark() -> None:
    fake = FakeClient(data=False)

    assert (
        await db_reminders.clear_due_moved(
            as_client(fake), owner_telegram_id=OWNER_ID, task_id="0e2f", seen=datetime.now(TZ)
        )
        is False
    )


async def test_clear_due_moved_without_an_answer_is_a_failure() -> None:
    fake = FakeClient(data=None)

    with pytest.raises(DatabaseError):
        await db_reminders.clear_due_moved(
            as_client(fake), owner_telegram_id=OWNER_ID, task_id="0e2f", seen=datetime.now(TZ)
        )


async def test_known_facts_are_asked_for_this_owner_and_status_only() -> None:
    """В промпт уходят только факты владельца (§8.2): предположений там нет."""
    fake = FakeClient(data=[FACT_ROW])

    found = await db_facts.list_facts(as_client(fake), owner_telegram_id=OWNER_ID)

    assert found == [Fact(id="f1", category="car", text="Машина — Toyota Camry", status="fact")]
    assert ("table", "facts") in fake.calls
    assert ("eq", "owner_telegram_id", OWNER_ID) in fake.calls
    assert ("eq", "status", "fact") in fake.calls
    assert ("order", "created_at", False) in fake.calls
    assert ("limit", 50) in fake.calls


async def test_list_facts_without_rows_is_empty() -> None:
    fake = FakeClient(data=[])

    assert await db_facts.list_facts(as_client(fake), owner_telegram_id=OWNER_ID) == []


async def test_broken_fact_row_is_a_failure() -> None:
    fake = FakeClient(data=[{"id": "f1", "category": "car"}])

    with pytest.raises(DatabaseError):
        await db_facts.list_facts(as_client(fake), owner_telegram_id=OWNER_ID)


async def test_memory_texts_for_hints_are_facts_and_guesses_of_this_owner() -> None:
    """Подсказкам годится и предположение (§9.5); чужой памяти в выборке нет."""
    fake = FakeClient(data=[{"text": "Сына зовут Юлай"}, {"text": "Есть дочь Рената"}])

    found = await db_facts.list_fact_texts(as_client(fake), owner_telegram_id=OWNER_ID, limit=200)

    assert found == ["Сына зовут Юлай", "Есть дочь Рената"]
    assert ("table", "facts") in fake.calls
    assert ("select", ("text",)) in fake.calls
    assert ("eq", "owner_telegram_id", OWNER_ID) in fake.calls
    assert ("in", "status", ("fact", "guess")) in fake.calls
    # Сначала сказанное прямо, внутри — свежие: так имена занимают место.
    orders = [call for call in fake.calls if call[0] == "order"]
    assert orders == [("order", "status", False), ("order", "created_at", True)]
    assert ("limit", 200) in fake.calls


@pytest.mark.parametrize("rows", [[{"id": "f1"}], [{"text": None}], ["не строка"], "не список"])
async def test_broken_memory_texts_are_a_failure(rows: Any) -> None:
    fake = FakeClient(data=rows)

    with pytest.raises(DatabaseError):
        await db_facts.list_fact_texts(as_client(fake), owner_telegram_id=OWNER_ID, limit=200)


async def test_task_people_for_hints_skip_cancelled_and_empty() -> None:
    """Люди задач для подсказок (§9.5): свои, активные и выполненные, свежие первыми.

    Убранная задача не в счёт — имя в ней могло быть расслышано неверно.
    """
    fake = FakeClient(data=[{"people": ["Юлай"]}, {"people": ["мама", "Анна Петровна"]}])

    found = await db_tasks.list_task_people(as_client(fake), owner_telegram_id=OWNER_ID, limit=200)

    assert found == [("Юлай",), ("мама", "Анна Петровна")]
    assert ("table", "tasks") in fake.calls
    assert ("select", ("people",)) in fake.calls
    assert ("eq", "owner_telegram_id", OWNER_ID) in fake.calls
    statuses = [call for call in fake.calls if call[:2] == ("in", "status")]
    assert statuses == [("in", "status", ("active", "done"))]
    assert "cancelled" not in statuses[0][2]
    assert ("neq", "people", "{}") in fake.calls
    orders = [call for call in fake.calls if call[0] == "order"]
    assert orders == [("order", "created_at", True)]
    assert ("limit", 200) in fake.calls


@pytest.mark.parametrize("rows", [[{"id": "0e2f"}], [{"people": "Юлай"}], ["не строка"], None])
async def test_broken_task_people_are_a_failure(rows: Any) -> None:
    fake = FakeClient(data=rows)

    with pytest.raises(DatabaseError):
        await db_tasks.list_task_people(as_client(fake), owner_telegram_id=OWNER_ID, limit=200)


async def read_question(fake: FakeClient) -> OpenQuestion | None:
    return await db_tasks.open_question(as_client(fake), owner_telegram_id=OWNER_ID, since=ASKED_AT)


async def test_open_question_is_asked_for_this_owner_and_the_last_day() -> None:
    """Открытый вопрос — свой, у активной задачи и не старше `since` (§10.3)."""
    fake = FakeClient(data=[QUESTION_ROW])
    since = ASKED_AT - timedelta(hours=1)

    found = await db_tasks.open_question(as_client(fake), owner_telegram_id=OWNER_ID, since=since)

    assert found == OpenQuestion(
        task_id="0e2f",
        question="К какому сроку?",
        title="отправить расчёт клиенту",
        kind="task",
        due_at=None,
        due_precision=None,
        priority="high",
        promise="mine",
        people=("клиент",),
        asked_at=ASKED_AT,
    )
    assert ("table", "tasks") in fake.calls
    assert ("eq", "owner_telegram_id", OWNER_ID) in fake.calls
    assert ("eq", "status", "active") in fake.calls
    assert ("gte", "question_asked_at", since.isoformat()) in fake.calls
    assert ("order", "question_asked_at", True) in fake.calls
    assert ("limit", 1) in fake.calls


async def test_open_question_reads_the_due_of_the_task() -> None:
    fake = FakeClient(
        data=[{**QUESTION_ROW, "due_at": "2026-09-25T18:00:00+05:00", "due_precision": "day"}]
    )

    found = await read_question(fake)

    assert found is not None
    assert found.due_at == datetime(2026, 9, 25, 18, 0, tzinfo=TZ)
    assert found.due_precision == "day"


async def test_open_question_reads_the_rule_of_the_task() -> None:
    """«Понял: … Повтор: …» называет правило, которое у задачи уже есть (§13.7)."""
    fake = FakeClient(data=[{**QUESTION_ROW, "repeat": WEEKLY}])

    found = await db_tasks.open_question(
        as_client(fake), owner_telegram_id=OWNER_ID, since=ASKED_AT
    )

    assert found is not None
    assert found.repeat == WEEKLY


async def test_no_open_question_is_none() -> None:
    fake = FakeClient(data=[])

    assert await read_question(fake) is None


async def test_task_without_question_text_is_no_question() -> None:
    fake = FakeClient(data=[{**QUESTION_ROW, "open_question": None}])

    assert await read_question(fake) is None


@pytest.mark.parametrize(
    "row",
    [
        {key: value for key, value in QUESTION_ROW.items() if key != "title"},
        {**QUESTION_ROW, "question_asked_at": "вчера"},
        {**QUESTION_ROW, "people": "клиент"},
        "не строка",
    ],
)
async def test_broken_question_row_is_a_failure(row: Any) -> None:
    fake = FakeClient(data=[row])

    with pytest.raises(DatabaseError):
        await read_question(fake)


async def test_open_tasks_are_asked_for_this_owner_in_prompt_order() -> None:
    """Список для промпта (§12.2): свои активные, со сроком раньше, новые выше."""
    fake = FakeClient(data=[DETAIL_ROW])

    found = await db_tasks.list_open_tasks(as_client(fake), owner_telegram_id=OWNER_ID, limit=50)

    assert found == [DETAILS]
    assert ("table", "tasks") in fake.calls
    assert ("eq", "owner_telegram_id", OWNER_ID) in fake.calls
    assert ("eq", "status", "active") in fake.calls
    orders = [call for call in fake.calls if call[0] == "order"]
    assert orders == [("order", "due_at", False, False), ("order", "created_at", True)]
    assert ("limit", 50) in fake.calls


async def test_open_task_carries_the_rule_and_the_occurrence() -> None:
    """Строка блока 5 называет повтор (§13.5) — правило приходит с задачей."""
    fake = FakeClient(data=[REPEAT_ROW])

    found = await db_tasks.list_open_tasks(as_client(fake), owner_telegram_id=OWNER_ID, limit=50)

    assert "repeat" in fake.calls[1][1][0]
    assert "occurrence_at" in fake.calls[1][1][0]
    assert found[0].repeat == WEEKLY
    assert found[0].occurrence_at == datetime(2026, 10, 12, 18, 0, tzinfo=TZ)


async def test_open_task_with_a_broken_rule_is_a_failure() -> None:
    """Правило строкой читать вслепую нельзя — отказ, а не разовая задача."""
    fake = FakeClient(data=[{**DETAIL_ROW, "repeat": "каждый понедельник"}])

    with pytest.raises(DatabaseError):
        await db_tasks.list_open_tasks(as_client(fake), owner_telegram_id=OWNER_ID, limit=50)


async def test_open_task_without_due_has_none() -> None:
    fake = FakeClient(data=[{**DETAIL_ROW, "due_at": None, "due_precision": None}])

    found = await db_tasks.list_open_tasks(as_client(fake), owner_telegram_id=OWNER_ID, limit=50)

    assert found[0].due_at is None
    assert found[0].due_precision is None


@pytest.mark.parametrize(
    "row",
    [
        {key: value for key, value in DETAIL_ROW.items() if key != "created_at"},
        {**DETAIL_ROW, "people": "Рената"},
        {**DETAIL_ROW, "due_at": "в пятницу"},
        "не строка",
    ],
)
async def test_broken_open_task_row_is_a_failure(row: Any) -> None:
    """Неполная строка — отказ: номер в промпте указал бы на задачу вслепую."""
    fake = FakeClient(data=[row])

    with pytest.raises(DatabaseError):
        await db_tasks.list_open_tasks(as_client(fake), owner_telegram_id=OWNER_ID, limit=50)


async def test_task_details_are_asked_by_owner_and_id_in_any_status() -> None:
    """Кнопки приносят id снаружи (§12.6): фильтр по владельцу обязателен."""
    fake = FakeClient(data=[{**DETAIL_ROW, "status": "cancelled"}])

    found = await db_tasks.task_details(
        as_client(fake), owner_telegram_id=OWNER_ID, task_id=TASK_ID
    )

    assert found is not None
    assert found.status == "cancelled"
    assert ("eq", "owner_telegram_id", OWNER_ID) in fake.calls
    assert ("eq", "id", TASK_ID) in fake.calls
    assert not any(call[:2] == ("eq", "status") for call in fake.calls)
    assert ("limit", 1) in fake.calls


async def test_missing_task_details_are_none() -> None:
    fake = FakeClient(data=[])

    assert (
        await db_tasks.task_details(as_client(fake), owner_telegram_id=OWNER_ID, task_id=TASK_ID)
        is None
    )


async def test_last_message_task_is_the_newest_message_about_a_task() -> None:
    """Событие разговора (§12.2): своё сообщение с задачей, не раньше `since`."""
    fake = FakeClient(
        tables={
            "messages": [
                {"id": "m2", "task_id": TASK_ID, "received_at": "2026-09-29T11:40:00+05:00"},
                {"id": "m1", "task_id": "0e2f", "received_at": "2026-09-29T11:20:00+05:00"},
            ],
            "tasks": [],
        }
    )

    found = await db_tasks.last_message_task(
        as_client(fake), owner_telegram_id=OWNER_ID, since=SINCE
    )

    assert found == TaskEvent(task_id=TASK_ID, at=datetime(2026, 9, 29, 11, 40, tzinfo=TZ))
    assert ("table", "messages") in fake.calls
    assert ("select", ("id, task_id, received_at",)) in fake.calls
    assert ("gte", "received_at", SINCE.isoformat()) in fake.calls
    assert ("order", "received_at", True) in fake.calls
    assert ("limit", db_tasks.LAST_MESSAGES_LIMIT) in fake.calls
    # Задачи сообщений — второй выборкой, тоже по владельцу (инвариант 2).
    assert ("table", "tasks") in fake.calls
    assert ("in", "source_message_id", ("m2", "m1")) in fake.calls
    assert ("order", "source_item", False) in fake.calls
    assert fake.calls.count(("eq", "owner_telegram_id", OWNER_ID)) == 2


async def test_message_about_several_tasks_gives_them_all_in_item_order() -> None:
    """Сообщение о нескольких делах (§23.6): `task_id` первым, дальше заведённые
    из него по номерам дел, без повторов."""
    fake = FakeClient(
        tables={
            "messages": [
                {"id": "m2", "task_id": TASK_ID, "received_at": "2026-09-29T11:40:00+05:00"},
            ],
            "tasks": [
                {"id": TASK_ID, "source_message_id": "m2", "source_item": 1},
                {"id": "0e2f", "source_message_id": "m2", "source_item": 2},
                {"id": "7c4d", "source_message_id": "m2", "source_item": 3},
            ],
        }
    )

    found = await db_tasks.last_message_task(
        as_client(fake), owner_telegram_id=OWNER_ID, since=SINCE
    )

    assert found == TaskEvent(
        task_id=TASK_ID, at=datetime(2026, 9, 29, 11, 40, tzinfo=TZ), more=("0e2f", "7c4d")
    )


async def test_message_without_tasks_is_skipped_for_an_older_one() -> None:
    """Сообщение без задач (болтовня, отказ) событием не считается: берётся
    более раннее — с задачами, заведёнными из него."""
    fake = FakeClient(
        tables={
            "messages": [
                {"id": "m3", "task_id": None, "received_at": "2026-09-29T11:50:00+05:00"},
                {"id": "m2", "task_id": None, "received_at": "2026-09-29T11:40:00+05:00"},
            ],
            "tasks": [
                {"id": "0e2f", "source_message_id": "m2", "source_item": 2},
                {"id": "7c4d", "source_message_id": "m2", "source_item": 3},
            ],
        }
    )

    found = await db_tasks.last_message_task(
        as_client(fake), owner_telegram_id=OWNER_ID, since=SINCE
    )

    assert found == TaskEvent(
        task_id="0e2f", at=datetime(2026, 9, 29, 11, 40, tzinfo=TZ), more=("7c4d",)
    )


async def test_window_without_task_messages_is_none() -> None:
    fake = FakeClient(
        tables={
            "messages": [
                {"id": "m3", "task_id": None, "received_at": "2026-09-29T11:50:00+05:00"},
            ],
            "tasks": [],
        }
    )

    assert (
        await db_tasks.last_message_task(as_client(fake), owner_telegram_id=OWNER_ID, since=SINCE)
        is None
    )


async def test_broken_source_task_row_is_a_failure() -> None:
    fake = FakeClient(
        tables={
            "messages": [
                {"id": "m2", "task_id": None, "received_at": "2026-09-29T11:40:00+05:00"},
            ],
            "tasks": [{"id": "0e2f"}],
        }
    )

    with pytest.raises(DatabaseError):
        await db_tasks.last_message_task(as_client(fake), owner_telegram_id=OWNER_ID, since=SINCE)


RECENT_ROWS = [
    {
        "received_at": "2026-09-29T11:50:00+05:00",
        "kind": "photo",
        "text": "",
        "forwarded_from": None,
        "reply": None,
    },
    {
        "received_at": "2026-09-29T11:45:00+05:00",
        "kind": "text",
        "text": "Во сколько?",
        "forwarded_from": "Рената",
        "reply": "Это не похоже на поручение — ничего не записал.",
    },
]


async def test_recent_messages_are_the_owners_last_messages_before_the_current() -> None:
    """Блок 6 (§17.3): сообщения владельца за окно, строго до текущего, последние N."""
    before = datetime(2026, 9, 29, 11, 55, tzinfo=TZ)
    fake = FakeClient(data=RECENT_ROWS)

    found = await db_tasks.recent_messages(
        as_client(fake), owner_telegram_id=OWNER_ID, since=SINCE, before=before, limit=10
    )

    assert ("table", "messages") in fake.calls
    assert ("select", ("received_at, kind, text, forwarded_from, reply",)) in fake.calls
    assert ("eq", "owner_telegram_id", OWNER_ID) in fake.calls
    assert ("gte", "received_at", SINCE.isoformat()) in fake.calls
    assert ("lt", "received_at", before.isoformat()) in fake.calls
    assert ("order", "received_at", True) in fake.calls
    assert ("limit", 10) in fake.calls
    # База отдаёт новые первыми, блоку нужны от старых к новым.
    assert found == [
        RecentMessage(
            received_at=datetime(2026, 9, 29, 11, 45, tzinfo=TZ),
            kind="text",
            text="Во сколько?",
            forwarded_from="Рената",
            reply="Это не похоже на поручение — ничего не записал.",
        ),
        RecentMessage(
            received_at=datetime(2026, 9, 29, 11, 50, tzinfo=TZ),
            kind="photo",
            text="",
            forwarded_from=None,
            reply=None,
        ),
    ]


async def test_broken_recent_message_is_a_failure_without_its_text() -> None:
    """Кривая строка — отказ; текст сообщения в отказ не попадает (журнал — без текстов)."""
    fake = FakeClient(data=[{"received_at": "2026-09-29T11:45:00+05:00", "text": "секрет"}])

    with pytest.raises(DatabaseError) as raised:
        await db_tasks.recent_messages(
            as_client(fake),
            owner_telegram_id=OWNER_ID,
            since=SINCE,
            before=SINCE + timedelta(hours=1),
            limit=10,
        )

    assert "секрет" not in str(raised.value)


async def test_recent_messages_not_a_list_is_a_failure() -> None:
    fake = FakeClient(data={"text": "Во сколько?"})

    with pytest.raises(DatabaseError):
        await db_tasks.recent_messages(
            as_client(fake),
            owner_telegram_id=OWNER_ID,
            since=SINCE,
            before=SINCE + timedelta(hours=1),
            limit=10,
        )


async def test_last_reminder_task_is_the_newest_sent_reminder() -> None:
    fake = FakeClient(data=[{"task_id": TASK_ID, "sent_at": "2026-09-29T11:50:00+05:00"}])

    found = await db_tasks.last_reminder_task(
        as_client(fake), owner_telegram_id=OWNER_ID, since=SINCE
    )

    assert found == TaskEvent(task_id=TASK_ID, at=datetime(2026, 9, 29, 11, 50, tzinfo=TZ))
    assert ("table", "reminders") in fake.calls
    assert ("eq", "owner_telegram_id", OWNER_ID) in fake.calls
    assert ("gte", "sent_at", SINCE.isoformat()) in fake.calls
    assert ("order", "sent_at", True) in fake.calls
    assert ("limit", 1) in fake.calls


async def test_no_events_in_the_window_are_none() -> None:
    fake = FakeClient(data=[])

    assert (
        await db_tasks.last_message_task(as_client(fake), owner_telegram_id=OWNER_ID, since=SINCE)
        is None
    )
    assert (
        await db_tasks.last_reminder_task(as_client(fake), owner_telegram_id=OWNER_ID, since=SINCE)
        is None
    )


async def test_broken_event_row_is_a_failure() -> None:
    fake = FakeClient(data=[{"task_id": TASK_ID}])

    with pytest.raises(DatabaseError):
        await db_tasks.last_reminder_task(as_client(fake), owner_telegram_id=OWNER_ID, since=SINCE)


async def test_reminder_task_is_found_by_the_telegram_message() -> None:
    """Свайп на напоминание (§12.2): его сообщение в Telegram — ключ к задаче."""
    fake = FakeClient(data=[{"task_id": TASK_ID}])

    found = await db_tasks.reminder_task_id(
        as_client(fake), owner_telegram_id=OWNER_ID, telegram_message_id=51
    )

    assert found == TASK_ID
    assert ("table", "reminders") in fake.calls
    assert ("eq", "owner_telegram_id", OWNER_ID) in fake.calls
    assert ("eq", "telegram_message_id", 51) in fake.calls


async def test_other_bot_message_is_no_reminder() -> None:
    fake = FakeClient(data=[])

    assert (
        await db_tasks.reminder_task_id(
            as_client(fake), owner_telegram_id=OWNER_ID, telegram_message_id=51
        )
        is None
    )


async def test_stored_message_is_found_by_owner_chat_and_telegram_id() -> None:
    fake = FakeClient(tables={"messages": [STORED_ROW], "tasks": []})

    found = await db_tasks.message_by_telegram_id(
        as_client(fake), owner_telegram_id=OWNER_ID, chat_id=OWNER_ID, telegram_message_id=7
    )

    assert found == StoredMessage(
        id="9a71",
        text="перенеси встречу на пятницу",
        task_id=None,
        analysis=STORED_ROW["analysis"],
        reply="Какую задачу перенести на пятницу, 2 октября?",
    )
    assert ("table", "messages") in fake.calls
    assert ("eq", "owner_telegram_id", OWNER_ID) in fake.calls
    assert ("eq", "chat_id", OWNER_ID) in fake.calls
    assert ("eq", "telegram_message_id", 7) in fake.calls


async def test_stored_message_knows_all_its_tasks() -> None:
    """Свайп на своё сообщение о нескольких делах (§23.6): задачи — по номерам дел."""
    fake = FakeClient(
        tables={
            "messages": [{**STORED_ROW, "task_id": TASK_ID}],
            "tasks": [
                {"id": TASK_ID, "source_message_id": "9a71", "source_item": 1},
                {"id": "0e2f", "source_message_id": "9a71", "source_item": 2},
            ],
        }
    )

    found = await db_tasks.message_by_telegram_id(
        as_client(fake), owner_telegram_id=OWNER_ID, chat_id=OWNER_ID, telegram_message_id=7
    )

    assert found is not None
    assert found.tasks == (TASK_ID, "0e2f")
    assert ("table", "tasks") in fake.calls
    assert ("in", "source_message_id", ("9a71",)) in fake.calls
    assert fake.calls.count(("eq", "owner_telegram_id", OWNER_ID)) == 2


async def test_unknown_stored_message_is_none() -> None:
    fake = FakeClient(data=[])

    assert (
        await db_tasks.message_by_telegram_id(
            as_client(fake), owner_telegram_id=OWNER_ID, chat_id=OWNER_ID, telegram_message_id=7
        )
        is None
    )


@pytest.mark.parametrize(
    "row",
    [
        {**STORED_ROW, "analysis": "перенос"},
        {key: value for key, value in STORED_ROW.items() if key != "task_id"},
    ],
)
async def test_broken_stored_message_is_a_failure(row: Any) -> None:
    fake = FakeClient(data=[row])

    with pytest.raises(DatabaseError):
        await db_tasks.message_by_telegram_id(
            as_client(fake), owner_telegram_id=OWNER_ID, chat_id=OWNER_ID, telegram_message_id=7
        )


async def test_pick_task_sends_the_edit_and_the_reply() -> None:
    """Кнопка кандидата (§12.6): правка, задача сообщения и ответ — одним вызовом."""
    fake = FakeClient(data={"id": "9a71", "task_id": TASK_ID, "reply": "Перенёс: встреча"})

    picked = await db_tasks.pick_task(
        as_client(fake),
        owner_telegram_id=OWNER_ID,
        message_id="9a71",
        edit=EDIT,
        reply="Перенёс: встреча",
    )

    assert picked == PickedMessage(id="9a71", task_id=TASK_ID, reply="Перенёс: встреча")
    assert fake.calls[0] == (
        "rpc",
        "pick_task",
        {
            "owner_telegram_id": OWNER_ID,
            "message_id": "9a71",
            "edit": EDIT,
            "reply": "Перенёс: встреча",
        },
    )


async def test_pick_task_reports_a_gone_task_as_empty() -> None:
    fake = FakeClient(data={"id": "9a71", "task_id": None, "reply": "Какую задачу закрыть?"})

    picked = await db_tasks.pick_task(
        as_client(fake),
        owner_telegram_id=OWNER_ID,
        message_id="9a71",
        edit=EDIT,
        reply="Закрыл: встреча.",
    )

    assert picked.task_id is None


async def test_pick_task_without_a_row_is_a_failure() -> None:
    fake = FakeClient(data={"id": None, "task_id": None, "reply": None})

    with pytest.raises(DatabaseError):
        await db_tasks.pick_task(
            as_client(fake),
            owner_telegram_id=OWNER_ID,
            message_id="9a71",
            edit=EDIT,
            reply="Закрыл: встреча.",
        )


async def test_record_separately_sends_the_task_the_plan_and_the_reply() -> None:
    """«Записать отдельно» (§15.4): задача, план и ответ — одним вызовом."""
    fake = FakeClient(data={"id": "9a71", "task_id": TASK_ID, "reply": "Записал: встреча"})
    plan = [{"stage": "due", "fire_at": "2026-10-02T17:00:00+05:00"}]

    saved = await db_tasks.record_separately(
        as_client(fake),
        owner_telegram_id=OWNER_ID,
        message_id="9a71",
        task=TASK_FIELDS,
        reminders=plan,
        reply="Записал: встреча",
    )

    assert saved == PickedMessage(id="9a71", task_id=TASK_ID, reply="Записал: встреча")
    assert fake.calls[0] == (
        "rpc",
        "record_separately",
        {
            "owner_telegram_id": OWNER_ID,
            "message_id": "9a71",
            "task": TASK_FIELDS,
            "reminders": plan,
            "reply": "Записал: встреча",
        },
    )


async def test_record_separately_without_a_row_is_a_failure() -> None:
    fake = FakeClient(data=None)

    with pytest.raises(DatabaseError):
        await db_tasks.record_separately(
            as_client(fake),
            owner_telegram_id=OWNER_ID,
            message_id="9a71",
            task=TASK_FIELDS,
            reminders=[],
            reply="Записал: встреча",
        )


async def test_same_minute_asks_for_the_owner_the_minute_and_the_order() -> None:
    """Накладка (§15.5): свои активные со сроком со временем в ту же минуту, старые первыми."""
    fake = FakeClient(data=[{"title": "созвон с Ренатой"}, {"title": "стрижка"}])
    due = datetime(2026, 10, 2, 21, 0, 30, tzinfo=TZ)

    titles = await db_tasks.same_minute_titles(
        as_client(fake), owner_telegram_id=OWNER_ID, due_at=due, exclude_task_id=TASK_ID
    )

    assert titles == ["созвон с Ренатой", "стрижка"]
    assert ("table", "tasks") in fake.calls
    assert ("eq", "owner_telegram_id", OWNER_ID) in fake.calls
    assert ("eq", "status", "active") in fake.calls
    assert ("eq", "due_precision", "time") in fake.calls
    assert ("gte", "due_at", "2026-10-02T21:00:00+05:00") in fake.calls
    assert ("lt", "due_at", "2026-10-02T21:01:00+05:00") in fake.calls
    assert ("neq", "id", TASK_ID) in fake.calls
    assert [call for call in fake.calls if call[0] == "order"] == [("order", "created_at", False)]


async def test_same_minute_of_a_new_task_excludes_nothing() -> None:
    fake = FakeClient(data=[])

    titles = await db_tasks.same_minute_titles(
        as_client(fake),
        owner_telegram_id=OWNER_ID,
        due_at=datetime(2026, 10, 2, 21, 0, tzinfo=TZ),
    )

    assert titles == []
    assert not [call for call in fake.calls if call[0] == "neq"]


@pytest.mark.parametrize("data", [None, [{"id": "x"}], ["строка"]])
async def test_broken_same_minute_answer_is_a_failure(data: Any) -> None:
    fake = FakeClient(data=data)

    with pytest.raises(DatabaseError):
        await db_tasks.same_minute_titles(
            as_client(fake),
            owner_telegram_id=OWNER_ID,
            due_at=datetime(2026, 10, 2, 21, 0, tzinfo=TZ),
        )


async def test_reopen_task_sends_the_plan_and_returns_the_task() -> None:
    """«Вернуть» (§12.6): план на момент нажатия уходит в базу тем же вызовом."""
    fake = FakeClient(data=DETAIL_ROW)
    plan = [Planned(stage="due", fire_at=datetime(2026, 10, 2, 17, 0, tzinfo=TZ))]

    task = await db_reminders.reopen_task(
        as_client(fake), owner_telegram_id=OWNER_ID, task_id=TASK_ID, schedule=plan
    )

    assert task == DETAILS
    assert fake.calls[0] == (
        "rpc",
        "reopen_task",
        {
            "owner_telegram_id": OWNER_ID,
            "task_id": TASK_ID,
            "schedule": [{"stage": "due", "fire_at": "2026-10-02T17:00:00+05:00"}],
        },
    )


async def test_reopen_task_of_a_deleted_task_is_none() -> None:
    fake = FakeClient(data={key: None for key in DETAIL_ROW})

    assert (
        await db_reminders.reopen_task(
            as_client(fake), owner_telegram_id=OWNER_ID, task_id=TASK_ID, schedule=[]
        )
        is None
    )


UNDATED_ROW = {
    "task_id": TASK_ID,
    "title": "купить фильтр для воды",
    "created_at": "2026-09-30T09:15:00+00:00",
    "asked_at": None,
}


def _ask_bounds() -> dict[str, datetime]:
    day_start = datetime(2026, 10, 3, 0, 0, tzinfo=TZ)
    now = datetime(2026, 10, 3, 10, 0, tzinfo=TZ)
    return {
        "day_start": day_start,
        "asked_before": day_start - timedelta(days=6),
        "question_since": now - timedelta(days=1),
        "quiet_since": now - timedelta(minutes=15),
    }


async def test_undated_to_ask_sends_the_owner_and_the_bounds() -> None:
    """Дело без срока (§19.4): владелец и четыре границы уходят явно (инвариант 2)."""
    fake = FakeClient(data=[UNDATED_ROW])
    bounds = _ask_bounds()

    found = await db_reminders.undated_to_ask(as_client(fake), owner_telegram_id=OWNER_ID, **bounds)

    expected = {key: value.isoformat() for key, value in bounds.items()}
    assert fake.calls[0] == (
        "rpc",
        "undated_to_ask",
        {"owner_telegram_id": OWNER_ID, **expected},
    )
    assert found == db_reminders.UndatedTask(
        task_id=TASK_ID,
        title="купить фильтр для воды",
        created_at=datetime(2026, 9, 30, 14, 15, tzinfo=TZ),
        asked_at=None,
    )


async def test_undated_to_ask_reads_when_it_asked_last() -> None:
    fake = FakeClient(data=[{**UNDATED_ROW, "asked_at": "2026-09-26T05:00:00+00:00"}])

    found = await db_reminders.undated_to_ask(
        as_client(fake), owner_telegram_id=OWNER_ID, **_ask_bounds()
    )

    assert found is not None
    assert found.asked_at == datetime(2026, 9, 26, 10, 0, tzinfo=TZ)


@pytest.mark.parametrize("data", [None, []])
async def test_undated_to_ask_without_rows_is_none(data: Any) -> None:
    fake = FakeClient(data=data)

    found = await db_reminders.undated_to_ask(
        as_client(fake), owner_telegram_id=OWNER_ID, **_ask_bounds()
    )

    assert found is None


@pytest.mark.parametrize(
    "data",
    [
        [UNDATED_ROW, UNDATED_ROW],
        [{key: value for key, value in UNDATED_ROW.items() if key != "created_at"}],
        {"task_id": TASK_ID},
    ],
)
async def test_undated_to_ask_odd_answer_is_a_failure(data: Any) -> None:
    """Две строки, неполная строка или не список — отказ: вопрос в день один."""
    fake = FakeClient(data=data)

    with pytest.raises(DatabaseError):
        await db_reminders.undated_to_ask(
            as_client(fake), owner_telegram_id=OWNER_ID, **_ask_bounds()
        )


async def test_record_ask_sends_the_owner_question_and_message() -> None:
    """Ушедший вопрос (§19.4): владелец, задача, текст и id сообщения — явно."""
    fake = FakeClient(data={**DETAIL_ROW, "due_at": None, "due_precision": None})

    task = await db_reminders.record_ask(
        as_client(fake),
        owner_telegram_id=OWNER_ID,
        task_id=TASK_ID,
        question="Когда займётесь?",
        telegram_message_id=4242,
    )

    assert fake.calls[0] == (
        "rpc",
        "record_ask",
        {
            "owner_telegram_id": OWNER_ID,
            "task_id": TASK_ID,
            "question": "Когда займётесь?",
            "telegram_message_id": 4242,
        },
    )
    assert task is not None
    assert task.id == TASK_ID


async def test_record_ask_of_a_gone_task_is_none() -> None:
    """База не записала: задача получила срок, закрыта или чужая."""
    fake = FakeClient(data={"id": None, "title": None})

    task = await db_reminders.record_ask(
        as_client(fake),
        owner_telegram_id=OWNER_ID,
        task_id=TASK_ID,
        question="Когда займётесь?",
        telegram_message_id=4242,
    )

    assert task is None


OVERDUE_ROW = {
    "task_id": TASK_ID,
    "title": "позвонить в сервис",
    "due_at": "2026-10-02T13:00:00+00:00",
    "due_precision": "time",
    "asked_at": None,
}


def _overdue_bounds(quiet: bool = True) -> dict[str, Any]:
    day_start = datetime(2026, 10, 3, 0, 0, tzinfo=TZ)
    now = datetime(2026, 10, 3, 13, 0, tzinfo=TZ)
    return {
        "day_start": day_start,
        "asked_before": day_start - timedelta(days=6),
        "question_since": now - timedelta(days=1),
        "quiet_since": now - timedelta(minutes=15) if quiet else None,
    }


async def test_overdue_to_ask_sends_the_owner_and_the_bounds() -> None:
    """Прошедшее дело (§22.4): владелец и четыре границы уходят явно (инвариант 2)."""
    fake = FakeClient(data=[OVERDUE_ROW])
    bounds = _overdue_bounds()

    found = await db_reminders.overdue_to_ask(as_client(fake), owner_telegram_id=OWNER_ID, **bounds)

    expected = {key: value.isoformat() for key, value in bounds.items() if value is not None}
    assert fake.calls[0] == ("rpc", "overdue_to_ask", {"owner_telegram_id": OWNER_ID, **expected})
    assert found == db_reminders.OverdueTask(
        task_id=TASK_ID,
        title="позвонить в сервис",
        due_at=datetime(2026, 10, 2, 18, 0, tzinfo=TZ),
        due_precision="time",
        asked_at=None,
    )


async def test_overdue_to_ask_for_the_plan_skips_the_quiet() -> None:
    """У плана `quiet_since` нет — в базу уходит `null`, тишина не проверяется."""
    fake = FakeClient(data=[{**OVERDUE_ROW, "due_precision": None}])

    found = await db_reminders.overdue_to_ask(
        as_client(fake), owner_telegram_id=OWNER_ID, **_overdue_bounds(quiet=False)
    )

    assert fake.calls[0][2]["quiet_since"] is None
    assert found is not None
    assert found.due_precision is None


async def test_overdue_to_ask_reads_when_it_asked_last() -> None:
    fake = FakeClient(data=[{**OVERDUE_ROW, "asked_at": "2026-09-26T05:00:00+00:00"}])

    found = await db_reminders.overdue_to_ask(
        as_client(fake), owner_telegram_id=OWNER_ID, **_overdue_bounds()
    )

    assert found is not None
    assert found.asked_at == datetime(2026, 9, 26, 10, 0, tzinfo=TZ)


@pytest.mark.parametrize("data", [None, []])
async def test_overdue_to_ask_without_rows_is_none(data: Any) -> None:
    fake = FakeClient(data=data)

    found = await db_reminders.overdue_to_ask(
        as_client(fake), owner_telegram_id=OWNER_ID, **_overdue_bounds()
    )

    assert found is None


@pytest.mark.parametrize(
    "data",
    [
        [OVERDUE_ROW, OVERDUE_ROW],
        [{key: value for key, value in OVERDUE_ROW.items() if key != "due_at"}],
        [{**OVERDUE_ROW, "due_at": None}],
        {"task_id": TASK_ID},
    ],
)
async def test_overdue_to_ask_odd_answer_is_a_failure(data: Any) -> None:
    """Две строки, строка без срока или не список — отказ: вопрос один."""
    fake = FakeClient(data=data)

    with pytest.raises(DatabaseError):
        await db_reminders.overdue_to_ask(
            as_client(fake), owner_telegram_id=OWNER_ID, **_overdue_bounds()
        )


@pytest.mark.parametrize("message_id", [4242, None])
async def test_record_overdue_ask_sends_the_owner_question_and_message(
    message_id: int | None,
) -> None:
    """Ушедший вопрос (§22.4): владелец, задача, текст и id сообщения; у плана id нет."""
    fake = FakeClient(data=DETAIL_ROW)

    task = await db_reminders.record_overdue_ask(
        as_client(fake),
        owner_telegram_id=OWNER_ID,
        task_id=TASK_ID,
        question="Получилось?",
        telegram_message_id=message_id,
    )

    assert fake.calls[0] == (
        "rpc",
        "record_overdue_ask",
        {
            "owner_telegram_id": OWNER_ID,
            "task_id": TASK_ID,
            "question": "Получилось?",
            "telegram_message_id": message_id,
        },
    )
    assert task is not None
    assert task.id == TASK_ID


async def test_record_overdue_ask_of_a_gone_task_is_none() -> None:
    """База не записала: задачу закрыли, перенесли вперёд или она чужая."""
    fake = FakeClient(data={"id": None, "title": None})

    task = await db_reminders.record_overdue_ask(
        as_client(fake),
        owner_telegram_id=OWNER_ID,
        task_id=TASK_ID,
        question="Получилось?",
        telegram_message_id=None,
    )

    assert task is None


def test_owner_is_required_by_every_query() -> None:
    """Инвариант 2 держится сигнатурой: владельца не забыть и не подставить."""
    for query in (
        db_tasks.record_message,
        db_tasks.record_understanding,
        db_tasks.list_active_tasks,
        db_tasks.open_question,
        db_tasks.list_open_tasks,
        db_tasks.task_details,
        db_tasks.last_message_task,
        db_tasks.last_reminder_task,
        db_tasks.recent_messages,
        db_tasks.reminder_task_id,
        db_tasks.message_by_telegram_id,
        db_tasks.pick_task,
        db_tasks.record_separately,
        db_tasks.same_minute_titles,
        db_tasks.list_task_people,
        db_reminders.reopen_task,
        db_reminders.due_reminders,
        db_reminders.mark_sent,
        db_reminders.mark_task_done,
        db_reminders.save_owner_timezone,
        db_reminders.moved_tasks,
        db_reminders.clear_due_moved,
        db_reminders.undated_to_ask,
        db_reminders.record_ask,
        db_reminders.overdue_to_ask,
        db_reminders.record_overdue_ask,
        db_facts.list_facts,
        db_facts.list_fact_texts,
    ):
        parameter = inspect.signature(query).parameters["owner_telegram_id"]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is inspect.Parameter.empty
