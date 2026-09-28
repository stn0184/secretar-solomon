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
from solomon.db.rpc import DatabaseError
from solomon.db.tasks import OpenQuestion, SavedMessage, Task
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

    def gte(self, column: str, value: Any) -> FakeQuery:
        self.client.calls.append(("gte", column, value))
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
            "kind": "text",
            "telegram_file_id": None,
            "duration_seconds": None,
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
        facts=FACT_ROWS,
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
            "facts": FACT_ROWS,
            "transcript": None,
            "transcript_confidence": None,
            "amend": None,
        },
    )


async def test_record_understanding_sends_the_amendment_instead_of_a_task() -> None:
    """Ответ на вопрос дополняет прежнюю задачу (§10.2): новой в запросе нет."""
    fake = FakeClient(data=ROW)

    task = await db_tasks.record_understanding(
        as_client(fake),
        message_id="9a72",
        owner_telegram_id=OWNER_ID,
        analysis=ANALYSIS,
        ai_model="claude-opus-5",
        ai_input_tokens=120,
        ai_output_tokens=45,
        reply="Понял: купить лампочку",
        task=None,
        reminders=[],
        facts=[],
        amend=AMEND,
    )

    assert task == Task(id="0e2f", title="купить лампочку", status="active")
    params = fake.calls[0][2]
    assert params["task"] is None
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
        task=TASK_FIELDS,
        reminders=[],
        facts=[],
        transcript="купить лампочку",
        transcript_confidence=0.93,
    )

    params = fake.calls[0][2]
    assert params["transcript"] == "купить лампочку"
    assert params["transcript_confidence"] == 0.93


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
        facts=[],
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
        facts=[],
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


def test_owner_is_required_by_every_query() -> None:
    """Инвариант 2 держится сигнатурой: владельца не забыть и не подставить."""
    for query in (
        db_tasks.record_message,
        db_tasks.record_understanding,
        db_tasks.list_active_tasks,
        db_tasks.open_question,
        db_reminders.due_reminders,
        db_reminders.mark_sent,
        db_reminders.mark_task_done,
        db_reminders.save_owner_timezone,
        db_reminders.moved_tasks,
        db_reminders.clear_due_moved,
        db_facts.list_facts,
    ):
        parameter = inspect.signature(query).parameters["owner_telegram_id"]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is inspect.Parameter.empty
