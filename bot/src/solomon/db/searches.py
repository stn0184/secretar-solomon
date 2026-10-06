"""Поиски по поручению: завести, взять в работу, записать ответ и итог.

Закон тот же, что у `tasks.py` и `morning.py`: `owner_telegram_id`
именованный и без значения по умолчанию в каждой функции, и он же уходит в
SQL-функцию — ключ service-role правила доступа обходит, поэтому разделение
по владельцу держит код (`techspec/04-access.md` §4.3).

Что делать с поиском — искать, слать ответ, говорить «не получилось» —
решает бот (`services/search.py`); база помнит строку и не даёт двум
заходам взять один поиск (`techspec/24-search.md` §24.5).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from supabase import Client

from solomon.db.rpc import DatabaseError, ask, moment, single_row

START_SEARCH_FUNCTION = "start_search"
TAKE_SEARCH_FUNCTION = "take_search"
RECORD_SEARCH_ANSWER_FUNCTION = "record_search_answer"
FINISH_SEARCH_FUNCTION = "finish_search"
RELEASE_SEARCH_FUNCTION = "release_search"
FAIL_SEARCH_FUNCTION = "fail_search"
SEARCHES_TO_RESUME_FUNCTION = "searches_to_resume"
PREVIOUS_SEARCH_FUNCTION = "previous_search"


@dataclass(frozen=True, slots=True)
class SearchRow:
    """Поиск, как его отдают `take_search` и `searches_to_resume` (§3.10).

    `answer` — записанный ответ, который ещё не ушёл; `chat_id` и
    `request_message_id` — сообщение с просьбой: ответ уходит ответом на него.
    """

    id: str
    query: str
    attempts: int
    answer: str | None
    created_at: datetime
    chat_id: int
    request_message_id: int


@dataclass(frozen=True, slots=True)
class PastSearch:
    """Прошлый завершённый поиск — для уточнения вдогонку (§24.2)."""

    query: str
    answer: str


@dataclass(frozen=True, slots=True)
class SearchTrace:
    """След поиска для разработчика (§24.2): ложится в строку с ответом."""

    input_tokens: int
    output_tokens: int
    web_searches: int
    web_fetches: int
    duration_ms: int


def _search_from_row(row: Any) -> SearchRow:
    """Разобрать строку поиска. Неполная — отказ, а не поиск без запроса или чата."""
    if not isinstance(row, Mapping):
        raise DatabaseError("База вернула не строку поиска.")
    try:
        search_id = row["id"]
        query = row["query"]
        attempts = row["attempts"]
        created_at = row["created_at"]
        chat_id = row["request_chat_id"]
        request_message_id = row["request_message_id"]
    except KeyError as error:
        raise DatabaseError(f"В ответе базы нет поля поиска: {error}.") from error
    if None in (search_id, query, attempts, created_at, chat_id, request_message_id):
        raise DatabaseError("База вернула поиск без запроса, попыток, времени или чата.")
    answer = row.get("answer")
    try:
        return SearchRow(
            id=str(search_id),
            query=str(query),
            attempts=int(attempts),
            answer=None if answer is None else str(answer),
            created_at=moment(created_at, "created_at"),
            chat_id=int(chat_id),
            request_message_id=int(request_message_id),
        )
    except (TypeError, ValueError) as error:
        raise DatabaseError(f"В ответе базы не разобрать поиск: {error}.") from error


def _rows(data: Any, what: str) -> list[Any]:
    """Набор строк: PostgREST отдаёт список, пусто — пустой список."""
    if data is None:
        return []
    if not isinstance(data, list):
        raise DatabaseError(f"База вернула не список: {what}.")
    return data


def _answer(data: Any, what: str) -> bool:
    """Ответ «да или нет»: не `bool` — отказ, а не догадка."""
    if not isinstance(data, bool):
        raise DatabaseError(f"База не ответила, {what}: {data!r}.")
    return data


async def start_search(db: Client, *, owner_telegram_id: int, message_id: str, query: str) -> str:
    """Завести поиск по сообщению (§24.3, шаг 1) — раньше, чем бот скажет «Ищу».

    Возвращает id поиска; по этому сообщению поиск уже есть — его id, и
    второго не будет. Сообщение чужое — отказ базы.
    """
    params = {"owner_telegram_id": owner_telegram_id, "message_id": message_id, "query": query}
    data = await ask(lambda: db.rpc(START_SEARCH_FUNCTION, params).execute().data)
    if not isinstance(data, str) or not data:
        raise DatabaseError(f"База не вернула id поиска: {data!r}.")
    return data


async def take_search(
    db: Client, *, owner_telegram_id: int, search_id: str, stale_before: datetime
) -> SearchRow | None:
    """Взять поиск в работу (§24.3): попытка плюс одна, время начала — сейчас.

    `None` — поиск уже ищется (начат не раньше `stale_before`), завершён,
    с записанным ответом или чужой: искать его этим заходом не нужно.
    """
    params = {
        "owner_telegram_id": owner_telegram_id,
        "search_id": search_id,
        "stale_before": stale_before.isoformat(),
    }
    rows = _rows(
        await ask(lambda: db.rpc(TAKE_SEARCH_FUNCTION, params).execute().data), "взятый поиск"
    )
    return _search_from_row(rows[0]) if rows else None


async def record_search_answer(
    db: Client, *, owner_telegram_id: int, search_id: str, answer: str, trace: SearchTrace
) -> bool:
    """Записать ответ и след (§24.3, шаг 4) — до отправки владельцу.

    `False` — ответ уже записан или поиск не ждёт: второй раз не пишется.
    """
    params = {
        "owner_telegram_id": owner_telegram_id,
        "search_id": search_id,
        "answer": answer,
        "input_tokens": trace.input_tokens,
        "output_tokens": trace.output_tokens,
        "web_searches": trace.web_searches,
        "web_fetches": trace.web_fetches,
        "duration_ms": trace.duration_ms,
    }
    data = await ask(lambda: db.rpc(RECORD_SEARCH_ANSWER_FUNCTION, params).execute().data)
    return _answer(data, "записан ли ответ поиска")


async def finish_search(
    db: Client, *, owner_telegram_id: int, search_id: str, telegram_message_id: int
) -> bool:
    """Ответ ушёл — `done` с id сообщения (§24.3, шаг 4). `False` — уже помечен."""
    params = {
        "owner_telegram_id": owner_telegram_id,
        "search_id": search_id,
        "telegram_message_id": telegram_message_id,
    }
    data = await ask(lambda: db.rpc(FINISH_SEARCH_FUNCTION, params).execute().data)
    return _answer(data, "помечен ли поиск завершённым")


async def release_search(db: Client, *, owner_telegram_id: int, search_id: str) -> int | None:
    """Вернуть неудачную попытку (§24.6): поиск снова не начат.

    Возвращает, сколько раз поиск уже начинали; `None` — такого ждущего
    поиска без ответа нет.
    """
    params = {"owner_telegram_id": owner_telegram_id, "search_id": search_id}
    data = single_row(await ask(lambda: db.rpc(RELEASE_SEARCH_FUNCTION, params).execute().data))
    if data is None:
        return None
    if isinstance(data, bool) or not isinstance(data, int):
        raise DatabaseError(f"База не назвала число попыток: {data!r}.")
    return data


async def fail_search(db: Client, *, owner_telegram_id: int, search_id: str) -> bool:
    """Поиск не удался или не успел (§24.6) — `failed`, после того как владелец
    узнал. `False` — поиск уже не ждёт или ответ записан."""
    params = {"owner_telegram_id": owner_telegram_id, "search_id": search_id}
    data = await ask(lambda: db.rpc(FAIL_SEARCH_FUNCTION, params).execute().data)
    return _answer(data, "помечен ли поиск неудавшимся")


async def searches_to_resume(
    db: Client, *, owner_telegram_id: int, stale_before: datetime
) -> list[SearchRow]:
    """Поиски для тика (§24.3, шаг 3): о которых сказано «Ищу», а ответ не
    ушёл, — с записанным ответом, не начатые и брошенные (начатые раньше
    `stale_before`). Порядок — как отдала база: по времени заведения."""
    params = {"owner_telegram_id": owner_telegram_id, "stale_before": stale_before.isoformat()}
    rows = _rows(
        await ask(lambda: db.rpc(SEARCHES_TO_RESUME_FUNCTION, params).execute().data),
        "поиски для тика",
    )
    return [_search_from_row(row) for row in rows]


async def previous_search(
    db: Client, *, owner_telegram_id: int, before: datetime, since: datetime
) -> PastSearch | None:
    """Прошлый завершённый поиск (§24.2): заведён раньше `before`, завершён не
    раньше `since`. Нет — `None`."""
    params = {
        "owner_telegram_id": owner_telegram_id,
        "before": before.isoformat(),
        "since": since.isoformat(),
    }
    rows = _rows(
        await ask(lambda: db.rpc(PREVIOUS_SEARCH_FUNCTION, params).execute().data),
        "прошлый поиск",
    )
    if not rows:
        return None
    row = rows[0]
    if not isinstance(row, Mapping) or row.get("query") is None or row.get("answer") is None:
        raise DatabaseError("База вернула прошлый поиск без запроса или ответа.")
    return PastSearch(query=str(row["query"]), answer=str(row["answer"]))
