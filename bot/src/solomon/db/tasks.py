"""Входящие сообщения и задачи: запись и чтение.

Бот ходит в базу ключом service-role — правила доступа его не ограничивают,
поэтому разделение по владельцу держит этот слой: `owner_telegram_id` есть в
каждой сигнатуре, он именованный и без значения по умолчанию, так что вызов
«забыл владельца» не собирается (`techspec/04-access.md` §4.3).

Поход в базу и разбор отказа — общие для слоя, они живут в `rpc.py`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from supabase import Client

from solomon.db.rpc import DatabaseError, ask, single_row

RECORD_MESSAGE_FUNCTION = "record_message"
RECORD_UNDERSTANDING_FUNCTION = "record_understanding"
TASKS_TABLE = "tasks"
ACTIVE_STATUS = "active"
TASK_COLUMNS = "id, title, status"


@dataclass(frozen=True, slots=True)
class Task:
    """Строка `tasks` в том виде, в каком её читает бот."""

    id: str
    title: str
    status: str


@dataclass(frozen=True, slots=True)
class SavedMessage:
    """Строка `messages` после первого шага приёма (§3.4).

    `reply` пуст — ответа этому сообщению ещё не давали: либо оно только что
    заведено, либо прошлый заход упал между шагами и разбирать нужно заново.
    """

    id: str
    reply: str | None


def _message_from_row(row: Any) -> SavedMessage:
    """Разобрать строку сообщения. Без `id` второй шаг некуда адресовать."""
    if not isinstance(row, Mapping):
        raise DatabaseError("База вернула не строку сообщения.")
    try:
        message_id = str(row["id"])
    except KeyError as error:
        raise DatabaseError(f"В ответе базы нет поля сообщения: {error}.") from error
    reply = row.get("reply")
    return SavedMessage(id=message_id, reply=None if reply is None else str(reply))


def task_from_row(row: Any) -> Task:
    """Разобрать строку ответа. Неполная строка — отказ, а не полупустая задача."""
    if not isinstance(row, Mapping):
        raise DatabaseError("База вернула не строку задачи.")
    try:
        return Task(id=str(row["id"]), title=str(row["title"]), status=str(row["status"]))
    except KeyError as error:
        raise DatabaseError(f"В ответе базы нет поля задачи: {error}.") from error


async def record_message(
    db: Client,
    *,
    owner_telegram_id: int,
    chat_id: int,
    telegram_message_id: int,
    text: str,
) -> SavedMessage:
    """Шаг первый: сохранить сообщение до всякого разбора.

    Зовётся SQL-функция `record_message` (`techspec/03-schema.md` §3.4):
    поручение лежит в базе с первой секунды (инвариант 5), даже если модель
    потом не ответит. Повтор того же обновления новой строки не пишет и
    возвращает прежнюю — вместе с ответом, который бот уже давал.
    """
    params = {
        "owner_telegram_id": owner_telegram_id,
        "chat_id": chat_id,
        "telegram_message_id": telegram_message_id,
        "text": text,
    }
    data = single_row(await ask(lambda: db.rpc(RECORD_MESSAGE_FUNCTION, params).execute().data))
    if data is None:
        raise DatabaseError("База не вернула сообщение.")
    return _message_from_row(data)


async def record_understanding(
    db: Client,
    *,
    message_id: str,
    owner_telegram_id: int,
    analysis: Mapping[str, Any] | None,
    ai_model: str | None,
    ai_input_tokens: int | None,
    ai_output_tokens: int | None,
    reply: str,
    task: Mapping[str, Any] | None,
    reminders: Sequence[Mapping[str, Any]],
    facts: Sequence[Mapping[str, Any]],
) -> Task | None:
    """Шаг второй: разбор, ответ бота, задача, напоминания и память — одной транзакцией.

    Возвращает заведённую задачу; `None` — когда задачи и не должно быть
    (разговор, сведение о себе). Владелец передаётся явно и сверяется с
    владельцем сообщения на стороне базы (`techspec/04-access.md` §4.3).

    `reminders` — список `{stage, fire_at}` от `services/reminders.py` (§3.5):
    напоминания рождаются вместе с задачей, иначе отказ между двумя вставками
    оставил бы задачу, о которой некому напомнить. `facts` — список
    `{category, text, status}` (§3.7): статус уже проставлен ботом, повтор
    по владельцу, категории и тексту база схлопывает сама.
    """
    params = {
        "message_id": message_id,
        "owner_telegram_id": owner_telegram_id,
        "analysis": analysis,
        "ai_model": ai_model,
        "ai_input_tokens": ai_input_tokens,
        "ai_output_tokens": ai_output_tokens,
        "reply": reply,
        "task": task,
        "reminders": list(reminders),
        "facts": list(facts),
    }
    data = single_row(
        await ask(lambda: db.rpc(RECORD_UNDERSTANDING_FUNCTION, params).execute().data)
    )
    # Функция возвращает пустую строку составного типа, когда задачи нет:
    # у неё нет и `id`, и это не отказ базы, а «записывать было нечего».
    if data is None or (isinstance(data, Mapping) and data.get("id") is None):
        return None
    return task_from_row(data)


async def list_active_tasks(db: Client, *, owner_telegram_id: int, limit: int) -> list[Task]:
    """Активные задачи владельца, новые сверху."""
    rows = await ask(
        lambda: (
            db.table(TASKS_TABLE)
            .select(TASK_COLUMNS)
            .eq("owner_telegram_id", owner_telegram_id)
            .eq("status", ACTIVE_STATUS)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
            .data
        )
    )
    if not isinstance(rows, list):
        raise DatabaseError("База вернула не список задач.")
    return [task_from_row(row) for row in rows]
