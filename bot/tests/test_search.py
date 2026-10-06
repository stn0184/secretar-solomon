"""Поиск по поручению (`techspec/24-search.md`).

Вызовы базы — с подменённым клиентом Supabase (`FakeRpcClient`): проверяется,
что уходит в функцию и что бот делает с ответом.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import cast
from zoneinfo import ZoneInfo

import pytest
from supabase import Client

from solomon.db import searches as db_searches
from solomon.db.rpc import DatabaseError
from solomon.db.searches import PastSearch, SearchRow, SearchTrace
from tests.conftest import OWNER_ID, OWNER_TIMEZONE
from tests.test_reminders import FakeRpcClient

TZ = ZoneInfo(OWNER_TIMEZONE)
SEARCH_ID = "5d1e0f3a-7b2c-4d8e-9f10-2a3b4c5d6e7f"
MESSAGE_ID = "9a71c2d4-1e5f-4a6b-8c7d-0e1f2a3b4c5d"
QUERY = "билеты на самолёт Екатеринбург — Москва 15 октября, обратно 18 октября"
NOW = datetime(2026, 10, 6, 12, 0, tzinfo=TZ)
TRACE = SearchTrace(
    input_tokens=2400, output_tokens=870, web_searches=2, web_fetches=1, duration_ms=41500
)


def search_row(**changes: object) -> dict[str, object]:
    """Строка `take_search` и `searches_to_resume`, как её отдаёт PostgREST."""
    row: dict[str, object] = {
        "id": SEARCH_ID,
        "query": QUERY,
        "attempts": 1,
        "answer": None,
        "created_at": "2026-10-06T07:00:00+00:00",
        "request_chat_id": OWNER_ID,
        "request_message_id": 4242,
    }
    row.update(changes)
    return row


# --------------------------------------------------------- база (§24.5)


async def test_start_search_goes_with_the_owner_message_and_query() -> None:
    client = FakeRpcClient({"start_search": SEARCH_ID})

    search_id = await db_searches.start_search(
        cast(Client, client), owner_telegram_id=OWNER_ID, message_id=MESSAGE_ID, query=QUERY
    )

    assert search_id == SEARCH_ID
    assert client.calls == ["start_search"]
    assert client.params == [
        {"owner_telegram_id": OWNER_ID, "message_id": MESSAGE_ID, "query": QUERY}
    ]


@pytest.mark.parametrize("answer", [None, "", 42, [SEARCH_ID]])
async def test_start_search_without_an_id_is_a_refusal(answer: object) -> None:
    client = FakeRpcClient({"start_search": answer})

    with pytest.raises(DatabaseError):
        await db_searches.start_search(
            cast(Client, client), owner_telegram_id=OWNER_ID, message_id=MESSAGE_ID, query=QUERY
        )


async def test_take_search_reads_the_row_with_the_request() -> None:
    """Взятый поиск — запрос, попытка, время и сообщение с просьбой."""
    client = FakeRpcClient({"take_search": [search_row()]})
    stale = NOW - timedelta(minutes=10)

    taken = await db_searches.take_search(
        cast(Client, client), owner_telegram_id=OWNER_ID, search_id=SEARCH_ID, stale_before=stale
    )

    assert taken == SearchRow(
        id=SEARCH_ID,
        query=QUERY,
        attempts=1,
        answer=None,
        created_at=datetime(2026, 10, 6, 12, 0, tzinfo=TZ),
        chat_id=OWNER_ID,
        request_message_id=4242,
    )
    assert client.params == [
        {"owner_telegram_id": OWNER_ID, "search_id": SEARCH_ID, "stale_before": stale.isoformat()}
    ]


async def test_take_search_not_taken_is_none() -> None:
    client = FakeRpcClient({"take_search": []})

    taken = await db_searches.take_search(
        cast(Client, client), owner_telegram_id=OWNER_ID, search_id=SEARCH_ID, stale_before=NOW
    )

    assert taken is None


@pytest.mark.parametrize(
    "row",
    [
        search_row(query=None),
        search_row(created_at=None),
        search_row(request_message_id=None),
        {"id": SEARCH_ID},
        "строка",
    ],
)
async def test_incomplete_search_row_is_a_refusal(row: object) -> None:
    client = FakeRpcClient({"searches_to_resume": [row]})

    with pytest.raises(DatabaseError):
        await db_searches.searches_to_resume(
            cast(Client, client), owner_telegram_id=OWNER_ID, stale_before=NOW
        )


async def test_record_answer_goes_with_the_trace() -> None:
    client = FakeRpcClient({"record_search_answer": True})

    saved = await db_searches.record_search_answer(
        cast(Client, client),
        owner_telegram_id=OWNER_ID,
        search_id=SEARCH_ID,
        answer="1. Победа",
        trace=TRACE,
    )

    assert saved is True
    assert client.params == [
        {
            "owner_telegram_id": OWNER_ID,
            "search_id": SEARCH_ID,
            "answer": "1. Победа",
            "input_tokens": 2400,
            "output_tokens": 870,
            "web_searches": 2,
            "web_fetches": 1,
            "duration_ms": 41500,
        }
    ]


async def test_finish_and_fail_go_with_the_owner() -> None:
    client = FakeRpcClient({"finish_search": True, "fail_search": False})

    finished = await db_searches.finish_search(
        cast(Client, client),
        owner_telegram_id=OWNER_ID,
        search_id=SEARCH_ID,
        telegram_message_id=501,
    )
    failed = await db_searches.fail_search(
        cast(Client, client), owner_telegram_id=OWNER_ID, search_id=SEARCH_ID
    )

    assert (finished, failed) == (True, False)
    assert client.calls == ["finish_search", "fail_search"]
    assert client.params == [
        {"owner_telegram_id": OWNER_ID, "search_id": SEARCH_ID, "telegram_message_id": 501},
        {"owner_telegram_id": OWNER_ID, "search_id": SEARCH_ID},
    ]


@pytest.mark.parametrize(("answer", "attempts"), [(2, 2), (None, None), ([3], 3)])
async def test_release_names_the_attempts(answer: object, attempts: int | None) -> None:
    client = FakeRpcClient({"release_search": answer})

    released = await db_searches.release_search(
        cast(Client, client), owner_telegram_id=OWNER_ID, search_id=SEARCH_ID
    )

    assert released == attempts
    assert client.params == [{"owner_telegram_id": OWNER_ID, "search_id": SEARCH_ID}]


@pytest.mark.parametrize("function", ["record_search_answer", "finish_search", "fail_search"])
async def test_yes_or_no_without_a_clear_answer_is_a_refusal(function: str) -> None:
    client = cast(Client, FakeRpcClient({function: "ok"}))

    with pytest.raises(DatabaseError):
        if function == "record_search_answer":
            await db_searches.record_search_answer(
                client, owner_telegram_id=OWNER_ID, search_id=SEARCH_ID, answer="а", trace=TRACE
            )
        elif function == "finish_search":
            await db_searches.finish_search(
                client, owner_telegram_id=OWNER_ID, search_id=SEARCH_ID, telegram_message_id=1
            )
        else:
            await db_searches.fail_search(client, owner_telegram_id=OWNER_ID, search_id=SEARCH_ID)


async def test_searches_to_resume_go_in_order_of_the_base() -> None:
    client = FakeRpcClient(
        {
            "searches_to_resume": [
                search_row(),
                search_row(id="b", attempts=0, answer="1. Победа", request_message_id=4343),
            ]
        }
    )
    stale = NOW - timedelta(minutes=10)

    rows = await db_searches.searches_to_resume(
        cast(Client, client), owner_telegram_id=OWNER_ID, stale_before=stale
    )

    assert [(row.id, row.attempts, row.answer, row.request_message_id) for row in rows] == [
        (SEARCH_ID, 1, None, 4242),
        ("b", 0, "1. Победа", 4343),
    ]
    assert client.params == [{"owner_telegram_id": OWNER_ID, "stale_before": stale.isoformat()}]


async def test_previous_search_is_the_query_and_the_answer() -> None:
    client = FakeRpcClient({"previous_search": [{"query": QUERY, "answer": "1. Победа"}]})
    before = NOW
    since = NOW - timedelta(hours=1)

    past = await db_searches.previous_search(
        cast(Client, client), owner_telegram_id=OWNER_ID, before=before, since=since
    )

    assert past == PastSearch(query=QUERY, answer="1. Победа")
    assert client.params == [
        {
            "owner_telegram_id": OWNER_ID,
            "before": before.isoformat(),
            "since": since.isoformat(),
        }
    ]


async def test_no_previous_search_is_none() -> None:
    client = FakeRpcClient({"previous_search": []})

    past = await db_searches.previous_search(
        cast(Client, client), owner_telegram_id=OWNER_ID, before=NOW, since=NOW
    )

    assert past is None


async def test_broken_connection_is_a_refusal_for_every_call() -> None:
    """Сбой клиента наружу не течёт — один понятный тип, как у остальных вызовов."""
    client = cast(Client, FakeRpcClient(broken=True))
    owner = OWNER_ID

    calls = [
        db_searches.start_search(client, owner_telegram_id=owner, message_id=MESSAGE_ID, query="а"),
        db_searches.take_search(client, owner_telegram_id=owner, search_id="s", stale_before=NOW),
        db_searches.record_search_answer(
            client, owner_telegram_id=owner, search_id="s", answer="а", trace=TRACE
        ),
        db_searches.finish_search(
            client, owner_telegram_id=owner, search_id="s", telegram_message_id=1
        ),
        db_searches.release_search(client, owner_telegram_id=owner, search_id="s"),
        db_searches.fail_search(client, owner_telegram_id=owner, search_id="s"),
        db_searches.searches_to_resume(client, owner_telegram_id=owner, stale_before=NOW),
        db_searches.previous_search(client, owner_telegram_id=owner, before=NOW, since=NOW),
    ]
    for call in calls:
        with pytest.raises(DatabaseError):
            await call
