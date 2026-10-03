"""Утренний план — вызовы базы и чистые функции (`techspec/20-morning-plan.md`).

Сети и базы здесь нет: клиент Supabase — подделка из `test_reminders.py`,
окно, границы дня и строки плана считаются без них. Шаг минутного цикла —
в `test_reminders.py`, рядом с остальными шагами тика.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import cast
from zoneinfo import ZoneInfo

import pytest
from supabase import Client

from solomon import texts
from solomon.db import morning as db_morning
from solomon.db.morning import DayTask
from solomon.db.rpc import DatabaseError
from solomon.services import asks, morning
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


# --- Чистые функции: окно, день владельца, строки плана (§20.2, §20.3) ---


def task(
    title: str,
    due_at: datetime,
    precision: str | None = "time",
    task_id: str = TASK_ID,
) -> DayTask:
    return DayTask(task_id=task_id, title=title, due_at=due_at, due_precision=precision)


def at(hour: int, minute: int = 0) -> datetime:
    """Сегодня в этот час по поясу владельца."""
    return datetime(2026, 10, 5, hour, minute, tzinfo=TZ)


def test_window_and_line_limit_are_constants() -> None:
    """Окно 08:00–12:00 и предел в 20 строк — константы, а не настройка (§20.2)."""
    assert morning.WINDOW_START == time(8, 0)
    assert morning.WINDOW_END == time(12, 0)
    assert morning.LINE_LIMIT == 20


@pytest.mark.parametrize(
    ("moment", "open_"),
    [
        (at(7, 59), False),
        (at(8, 0), True),
        (at(10, 30), True),
        (at(11, 59), True),
        (at(12, 0), False),
        (at(23, 0), False),
        (at(3, 0), False),
        # 03:00 по UTC — 08:00 у владельца (+05:00): окно по его поясу.
        (datetime(2026, 10, 5, 3, 0, tzinfo=UTC), True),
        (datetime(2026, 10, 5, 7, 0, tzinfo=UTC), False),
    ],
)
def test_window_is_eight_to_noon_by_owner_time(moment: datetime, open_: bool) -> None:
    """С 08:00 до 12:00 по поясу владельца; с 12:00 план не догоняет (§20.2)."""
    assert morning.in_window(moment, TZ) is open_


def test_day_bounds_are_owner_midnights() -> None:
    """День владельца и его полуночи — те же, что у вопроса (`services/asks.py`)."""
    now = at(8, 0)

    bounds = morning.day_bounds(now, TZ)

    assert bounds.day == TODAY
    assert bounds.day_start == DAY_START
    assert bounds.day_end == DAY_END
    assert bounds.day_start == asks.bounds(now, TZ).day_start


def test_day_is_taken_by_owner_zone_not_by_utc() -> None:
    """22:30 по UTC 4 октября — уже 5 октября у владельца."""
    bounds = morning.day_bounds(datetime(2026, 10, 4, 22, 30, tzinfo=UTC), TZ)

    assert bounds.day == TODAY
    assert bounds.day_start == DAY_START


def test_day_end_is_next_midnight_even_when_clocks_change() -> None:
    """В день перевода часов сутки длиннее: в базу уходят обе полуночи с их сдвигом."""
    berlin = ZoneInfo("Europe/Berlin")

    bounds = morning.day_bounds(datetime(2026, 10, 25, 9, 0, tzinfo=berlin), berlin)

    assert bounds.day_start.isoformat() == "2026-10-25T00:00:00+02:00"
    assert bounds.day_end.isoformat() == "2026-10-26T00:00:00+01:00"
    assert bounds.day_end.astimezone(UTC) - bounds.day_start.astimezone(UTC) == timedelta(hours=25)


def test_plan_reads_as_in_the_techspec() -> None:
    """Шапка, дела со временем по времени, затем дела на день (§20.3)."""
    tasks = [
        task("встреча с Ольгой", at(9, 0)),
        task("позвонить Сергею", at(15, 30)),
        task("купить лампочку в коридор", at(18, 0), "day"),
    ]

    assert morning.plan_text(tasks, TZ) == (
        "Доброе утро! На сегодня:\n"
        "09:00 — встреча с Ольгой\n"
        "15:30 — позвонить Сергею\n"
        "В течение дня — купить лампочку в коридор"
    )


def test_timed_tasks_go_first_and_by_time() -> None:
    """Дело на день (18:00 в `due_at`) идёт после дела на 19:00 (§20.1)."""
    tasks = [
        task("купить лампочку в коридор", at(18, 0), "day"),
        task("ужин с Анной", at(19, 0)),
        task("забрать посылку", at(18, 0), "day"),
        task("встреча с Ольгой", at(9, 0)),
    ]

    assert morning.plan_lines(tasks, TZ) == [
        "09:00 — встреча с Ольгой",
        "19:00 — ужин с Анной",
        "В течение дня — купить лампочку в коридор",
        "В течение дня — забрать посылку",
    ]


def test_time_is_shown_in_owner_zone() -> None:
    """База отдаёт время в UTC; в строке — час владельца."""
    tasks = [task("встреча с Ольгой", datetime(2026, 10, 5, 4, 0, tzinfo=UTC))]

    assert morning.plan_lines(tasks, TZ) == ["09:00 — встреча с Ольгой"]


def test_task_without_precision_is_a_day_task() -> None:
    """Точности нет — срок не со временем: строка «В течение дня»."""
    assert morning.plan_lines([task("оплатить свет", at(18, 0), None)], TZ) == [
        "В течение дня — оплатить свет"
    ]


def test_empty_day_says_there_is_nothing() -> None:
    """Дел нет — так и сказано, без пустой шапки (§20.3)."""
    assert morning.plan_text([], TZ) == "Доброе утро! На сегодня дел нет."


def test_twenty_lines_fit_without_a_tail() -> None:
    tasks = [task(f"дело {n}", at(8, n)) for n in range(20)]

    lines = morning.plan_lines(tasks, TZ)

    assert len(lines) == 20
    assert lines[-1] == "08:19 — дело 19"


def test_more_than_twenty_lines_end_with_a_count() -> None:
    """Первые 20 строк в порядке плана и «И ещё N — в приложении.» (§20.3)."""
    timed = [task(f"дело {n}", at(9, n)) for n in range(18)]
    all_day = [task(f"на день {n}", at(18, 0), "day") for n in range(5)]

    lines = morning.plan_lines(all_day + timed, TZ)

    assert len(lines) == 21
    assert lines[0] == "09:00 — дело 0"
    assert lines[17] == "09:17 — дело 17"
    assert lines[18:20] == ["В течение дня — на день 0", "В течение дня — на день 1"]
    assert lines[20] == "И ещё 3 — в приложении."


def test_texts_of_the_plan() -> None:
    """Слова плана живут в `texts.py` (§20.3)."""
    assert texts.MORNING_HEAD == "Доброе утро! На сегодня:"
    assert texts.MORNING_EMPTY == "Доброе утро! На сегодня дел нет."
    assert texts.MORNING_ALL_DAY == "В течение дня"
    assert texts.morning_more(4) == "И ещё 4 — в приложении."


def test_help_tells_about_the_morning_plan() -> None:
    """В /help — что каждое утро в 8:00 бот присылает дела на сегодня (§20.3)."""
    assert "Каждое утро в 8:00 присылаю дела со сроком на сегодня" in texts.HELP
