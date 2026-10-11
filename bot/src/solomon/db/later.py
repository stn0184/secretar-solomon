"""Отложенные сообщения владельца: отложить, взять голову очереди, вернуть, снять.

Закон тот же, что у `tasks.py` и `searches.py`: `owner_telegram_id`
именованный и без значения по умолчанию в каждой функции, и он же уходит в
SQL-функцию — ключ service-role правила доступа обходит, поэтому разделение
по владельцу держит код (`techspec/04-access.md` §4.3).

Что делать с сообщением — разбирать, слать ответ, писать «как есть» — решает
бот (`services/later.py`); база помнит, что сообщение ждёт, и не даёт двум
заходам взять одну голову очереди (`techspec/33-retry-later.md` §33.2).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from supabase import Client

from solomon.db.rpc import DatabaseError, ask, moment, single_row

DEFER_FUNCTION = "defer_message"
WAITING_FUNCTION = "deferred_waiting"
TAKE_FUNCTION = "take_deferred"
RELEASE_FUNCTION = "release_deferred"
FINISH_FUNCTION = "finish_deferred"


@dataclass(frozen=True, slots=True)
class DeferredMessage:
    """Ждущее сообщение владельца, как его отдаёт `take_deferred` (§3.2).

    `text` у голоса — сохранённая расшифровка, у снимка — подпись; `reply` —
    записанный и ещё не ушедший ответ; `attempts` — сколько раз звали модель,
    эта попытка тоже; `retry_input` — что нужно разбору сверх строки: свайп,
    вид снимка, переписка (§33.2).
    """

    id: str
    chat_id: int
    telegram_message_id: int
    kind: str
    text: str
    received_at: datetime
    attempts: int
    telegram_file_id: str | None = None
    transcript_confidence: float | None = None
    forwarded_from: str | None = None
    reply: str | None = None
    retry_input: Mapping[str, Any] = field(default_factory=dict)


def _optional_text(value: Any) -> str | None:
    return None if value is None else str(value)


def deferred_from_row(row: Any) -> DeferredMessage:
    """Разобрать строку сообщения. Без чата, номера или времени — отказ: ответить
    было бы некуда, а разобрать — не от какого момента."""
    if not isinstance(row, Mapping):
        raise DatabaseError("База вернула не строку сообщения.")
    try:
        message_id = row["id"]
        chat_id = row["chat_id"]
        telegram_message_id = row["telegram_message_id"]
        received_at = row["received_at"]
    except KeyError as error:
        raise DatabaseError(f"В ответе базы нет поля сообщения: {error}.") from error
    if None in (message_id, chat_id, telegram_message_id, received_at):
        raise DatabaseError("База вернула отложенное сообщение без чата, номера или времени.")
    retry_input = row.get("retry_input")
    if retry_input is not None and not isinstance(retry_input, Mapping):
        raise DatabaseError(f"В ответе базы не разобрать retry_input: {retry_input!r}.")
    confidence = row.get("transcript_confidence")
    try:
        return DeferredMessage(
            id=str(message_id),
            chat_id=int(chat_id),
            telegram_message_id=int(telegram_message_id),
            kind=str(row.get("kind") or "text"),
            text=str(row.get("text") or ""),
            received_at=moment(received_at, "received_at"),
            attempts=int(row.get("retry_attempts") or 0),
            telegram_file_id=_optional_text(row.get("telegram_file_id")),
            transcript_confidence=None if confidence is None else float(confidence),
            forwarded_from=_optional_text(row.get("forwarded_from")),
            reply=_optional_text(row.get("reply")),
            retry_input=dict(retry_input or {}),
        )
    except (TypeError, ValueError) as error:
        raise DatabaseError(f"В ответе базы не разобрать сообщение: {error}.") from error


def _answer(data: Any, what: str) -> bool:
    """Ответ «да или нет»: не `bool` — отказ, а не догадка."""
    if not isinstance(data, bool):
        raise DatabaseError(f"База не ответила, {what}: {data!r}.")
    return data


async def defer_message(
    db: Client,
    *,
    owner_telegram_id: int,
    message_id: str,
    retry_at: datetime,
    attempts: int,
    retry_input: Mapping[str, Any] | None = None,
    transcript: str | None = None,
    transcript_confidence: float | None = None,
) -> bool:
    """Отложить сообщение (§33.2, шаг 1): когда пробовать, сколько раз уже звали
    модель и что нужно разбору. Расшифровка голоса ложится текстом сообщения.

    `False` — у сообщения уже есть ответ: откладывать нечего. Уже ждёт — `True`
    без правки. Чужое — отказ базы.
    """
    params = {
        "owner_telegram_id": owner_telegram_id,
        "message_id": message_id,
        "retry_at": retry_at.isoformat(),
        "attempts": attempts,
        "retry_input": None if retry_input is None else dict(retry_input),
        "transcript": transcript,
        "transcript_confidence": transcript_confidence,
    }
    data = await ask(lambda: db.rpc(DEFER_FUNCTION, params).execute().data)
    return _answer(data, "отложено ли сообщение")


async def deferred_waiting(db: Client, *, owner_telegram_id: int, message_id: str) -> bool:
    """Ждёт ли другое сообщение владельца (§33.2, шаг 5)."""
    params = {"owner_telegram_id": owner_telegram_id, "message_id": message_id}
    data = await ask(lambda: db.rpc(WAITING_FUNCTION, params).execute().data)
    return _answer(data, "ждёт ли очередь")


async def take_deferred(
    db: Client, *, owner_telegram_id: int, due_before: datetime, stale_before: datetime
) -> DeferredMessage | None:
    """Голова очереди владельца (§33.2): самое раннее ждущее сообщение, взятое
    этим заходом. `None` — очередь пуста, голова в работе (взята не раньше
    `stale_before`) или её попытка позже `due_before`."""
    params = {
        "owner_telegram_id": owner_telegram_id,
        "due_before": due_before.isoformat(),
        "stale_before": stale_before.isoformat(),
    }
    data = single_row(await ask(lambda: db.rpc(TAKE_FUNCTION, params).execute().data))
    return None if data is None else deferred_from_row(data)


async def release_deferred(
    db: Client, *, owner_telegram_id: int, message_id: str, retry_at: datetime
) -> bool:
    """Вернуть сообщение в очередь со следующим временем. `False` — не ждёт."""
    params = {
        "owner_telegram_id": owner_telegram_id,
        "message_id": message_id,
        "retry_at": retry_at.isoformat(),
    }
    data = await ask(lambda: db.rpc(RELEASE_FUNCTION, params).execute().data)
    return _answer(data, "возвращено ли сообщение в очередь")


async def finish_deferred(db: Client, *, owner_telegram_id: int, message_id: str) -> bool:
    """Ответ ушёл — сообщение больше не ждёт. `False` — уже не ждало."""
    params = {"owner_telegram_id": owner_telegram_id, "message_id": message_id}
    data = await ask(lambda: db.rpc(FINISH_FUNCTION, params).execute().data)
    return _answer(data, "снято ли ожидание")
