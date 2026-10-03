"""Утренний план — вызовы базы и чистые функции (`techspec/20-morning-plan.md`).

Сети и базы здесь нет: клиент Supabase — подделка из `test_reminders.py`,
окно, границы дня и строки плана считаются без них. Шаг минутного цикла —
в `test_reminders.py`, рядом с остальными шагами тика.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import cast
from zoneinfo import ZoneInfo

import pytest
from supabase import Client

from solomon.db import morning as db_morning
from solomon.db.morning import DayTask
from solomon.db.rpc import DatabaseError
from tests.conftest import OWNER_ID, OWNER_TIMEZONE
from tests.test_reminders import FakeRpcClient

TZ = ZoneInfo(OWNER_TIMEZONE)
# Понедельник, 5 октября 2026, в поясе владельца (+05:00).
TODAY = date(2026, 10, 5)
DAY_START = datetime(2026, 10, 5, 0, 0, tzinfo=TZ)
DAY_END = datetime(2026, 10, 6, 0, 0, tzinfo=TZ)
TASK_ID = "7c1d2e3f-4a5b-4c6d-8e9f-0a1b2c3d4e5f"


def day_row(**changes: object) -> dict[str, object]:
    """Строка `day_tasks`, как её отдаёт PostgREST."""
    row: dict[str, object] = {
        "task_id": TASK_ID,
        "title": "встреча с Ольгой",
        "due_at": "2026-10-05T04:00:00+00:00",
        "due_precision": "time",
    }
    row.update(changes)
    return row


# --- База: три вызова с фильтром по владельцу (§20.4) ---


async def test_plan_sent_asks_about_the_owner_day() -> None:
    """`morning_plan_sent` получает владельца и день; ответ — как есть."""
    client = FakeRpcClient({"morning_plan_sent": True})

    sent = await db_morning.morning_plan_sent(
        cast(Client, client), owner_telegram_id=OWNER_ID, day=TODAY
    )

    assert sent is True
    assert client.calls == ["morning_plan_sent"]
    assert client.params == [{"owner_telegram_id": OWNER_ID, "day": "2026-10-05"}]


async def test_plan_sent_without_a_clear_answer_is_a_refusal() -> None:
    """Не `true`/`false` — отказ: «плана не было» на догадке не строится."""
    client = FakeRpcClient({"morning_plan_sent": None})

    with pytest.raises(DatabaseError):
        await db_morning.morning_plan_sent(
            cast(Client, client), owner_telegram_id=OWNER_ID, day=TODAY
        )


async def test_day_tasks_go_with_the_owner_and_day_bounds() -> None:
    """Границы дня уходят моментами с поясом; строки — в `DayTask` по порядку базы."""
    client = FakeRpcClient(
        {
            "day_tasks": [
                day_row(),
                day_row(
                    task_id="0f0e0d0c-0b0a-4908-8706-050403020100",
                    title="купить лампочку в коридор",
                    due_at="2026-10-05T13:00:00+00:00",
                    due_precision="day",
                ),
            ]
        }
    )

    tasks = await db_morning.day_tasks(
        cast(Client, client), owner_telegram_id=OWNER_ID, day_start=DAY_START, day_end=DAY_END
    )

    assert client.calls == ["day_tasks"]
    assert client.params == [
        {
            "owner_telegram_id": OWNER_ID,
            "day_start": DAY_START.isoformat(),
            "day_end": DAY_END.isoformat(),
        }
    ]
    assert tasks == [
        DayTask(
            task_id=TASK_ID,
            title="встреча с Ольгой",
            due_at=datetime(2026, 10, 5, 9, 0, tzinfo=TZ),
            due_precision="time",
        ),
        DayTask(
            task_id="0f0e0d0c-0b0a-4908-8706-050403020100",
            title="купить лампочку в коридор",
            due_at=datetime(2026, 10, 5, 18, 0, tzinfo=TZ),
            due_precision="day",
        ),
    ]
    assert tasks[0].has_time is True
    assert tasks[1].has_time is False


async def test_empty_day_is_an_empty_list() -> None:
    client = FakeRpcClient({"day_tasks": None})

    tasks = await db_morning.day_tasks(
        cast(Client, client), owner_telegram_id=OWNER_ID, day_start=DAY_START, day_end=DAY_END
    )

    assert tasks == []


@pytest.mark.parametrize(
    "answer",
    [
        {"task_id": TASK_ID},
        [{"task_id": TASK_ID, "due_at": "2026-10-05T04:00:00+00:00", "due_precision": "time"}],
        [day_row(title=None)],
        [day_row(due_at=None)],
        [day_row(due_at="завтра")],
        ["встреча с Ольгой"],
    ],
)
async def test_incomplete_day_task_is_a_refusal(answer: object) -> None:
    """Неполная строка — отказ, а не строка плана без сути или без срока."""
    client = FakeRpcClient({"day_tasks": answer})

    with pytest.raises(DatabaseError):
        await db_morning.day_tasks(
            cast(Client, client),
            owner_telegram_id=OWNER_ID,
            day_start=DAY_START,
            day_end=DAY_END,
        )


async def test_record_plan_goes_with_the_owner_day_and_message() -> None:
    """`record_morning_plan` записывает день и сообщение; `false` — строка уже была."""
    client = FakeRpcClient({"record_morning_plan": False})

    recorded = await db_morning.record_morning_plan(
        cast(Client, client), owner_telegram_id=OWNER_ID, day=TODAY, telegram_message_id=501
    )

    assert recorded is False
    assert client.calls == ["record_morning_plan"]
    assert client.params == [
        {"owner_telegram_id": OWNER_ID, "day": "2026-10-05", "telegram_message_id": 501}
    ]


async def test_record_plan_without_a_clear_answer_is_a_refusal() -> None:
    client = FakeRpcClient({"record_morning_plan": "ok"})

    with pytest.raises(DatabaseError):
        await db_morning.record_morning_plan(
            cast(Client, client), owner_telegram_id=OWNER_ID, day=TODAY, telegram_message_id=501
        )


async def test_broken_connection_is_a_refusal_for_every_call() -> None:
    """Сбой клиента наружу не течёт — один понятный тип, как у остальных вызовов."""
    client = cast(Client, FakeRpcClient(broken=True))

    with pytest.raises(DatabaseError):
        await db_morning.morning_plan_sent(client, owner_telegram_id=OWNER_ID, day=TODAY)
    with pytest.raises(DatabaseError):
        await db_morning.day_tasks(
            client, owner_telegram_id=OWNER_ID, day_start=DAY_START, day_end=DAY_END
        )
    with pytest.raises(DatabaseError):
        await db_morning.record_morning_plan(
            client, owner_telegram_id=OWNER_ID, day=TODAY, telegram_message_id=501
        )
