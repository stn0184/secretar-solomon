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
from datetime import datetime
from typing import Any, Literal

from supabase import Client

from solomon.db.rpc import DatabaseError, ask, moment, single_row

RECORD_MESSAGE_FUNCTION = "record_message"
RECORD_UNDERSTANDING_FUNCTION = "record_understanding"
TASKS_TABLE = "tasks"
ACTIVE_STATUS = "active"
TASK_COLUMNS = "id, title, status"
# Поля задачи, которые нужны промпту с открытым вопросом и слиянию ответа
# с задачей (`techspec/10-dialog.md` §10.2).
QUESTION_COLUMNS = (
    "id, title, kind, due_at, due_precision, priority, promise, people, "
    "open_question, question_asked_at"
)

# Вид сообщения (`techspec/03-schema.md` §3.2, §9.1): текст, голосовое,
# видео-кружок. Голосовые виды — те, у которых есть файл и длительность.
SpeechKind = Literal["voice", "video_note"]
MessageKind = Literal["text"] | SpeechKind


@dataclass(frozen=True, slots=True)
class Task:
    """Строка `tasks` в том виде, в каком её читает бот."""

    id: str
    title: str
    status: str


@dataclass(frozen=True, slots=True)
class OpenQuestion:
    """Задача, по которой бот задал вопрос и ещё не получил ответа (§10.1).

    Вместе с вопросом — поля задачи как они есть: модель видит их в промпте,
    а бот сливает с ними ответ и по итогу перепланирует напоминания.
    """

    task_id: str
    question: str
    title: str
    kind: str
    due_at: datetime | None
    due_precision: str | None
    priority: str
    promise: str | None
    people: tuple[str, ...]
    asked_at: datetime


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


def _optional_text(value: Any) -> str | None:
    return None if value is None else str(value)


def _open_question_from_row(row: Any) -> OpenQuestion | None:
    """Разобрать строку задачи с вопросом.

    Вопроса в строке нет — `None`: записи разбора снимают его вместе со
    временем, и такая строка значит «спрашивать не о чем». Неполная или
    кривая строка — отказ: слить ответ с полупустой задачей нельзя.
    """
    if not isinstance(row, Mapping):
        raise DatabaseError("База вернула не строку задачи.")
    try:
        question = row["open_question"]
        if question is None:
            return None
        people = row["people"]
        if not isinstance(people, list):
            raise DatabaseError(f"В ответе базы не разобрать people: {people!r}.")
        due_at = row["due_at"]
        return OpenQuestion(
            task_id=str(row["id"]),
            question=str(question),
            title=str(row["title"]),
            kind=str(row["kind"]),
            due_at=None if due_at is None else moment(due_at, "due_at"),
            due_precision=_optional_text(row["due_precision"]),
            priority=str(row["priority"]),
            promise=_optional_text(row["promise"]),
            people=tuple(str(person) for person in people),
            asked_at=moment(row["question_asked_at"], "question_asked_at"),
        )
    except KeyError as error:
        raise DatabaseError(f"В ответе базы нет поля задачи: {error}.") from error


async def record_message(
    db: Client,
    *,
    owner_telegram_id: int,
    chat_id: int,
    telegram_message_id: int,
    text: str,
    kind: MessageKind = "text",
    telegram_file_id: str | None = None,
    duration_seconds: int | None = None,
) -> SavedMessage:
    """Шаг первый: сохранить сообщение до всякого разбора.

    Зовётся SQL-функция `record_message` (`techspec/03-schema.md` §3.4):
    поручение лежит в базе с первой секунды (инвариант 5), даже если модель
    потом не ответит. Повтор того же обновления новой строки не пишет и
    возвращает прежнюю — вместе с ответом, который бот уже давал.

    У голоса и кружка `text` пуст до расшифровки, а `telegram_file_id` и
    `duration_seconds` заполнены (§9.3): по файлу звук можно скачать снова.
    """
    params = {
        "owner_telegram_id": owner_telegram_id,
        "chat_id": chat_id,
        "telegram_message_id": telegram_message_id,
        "text": text,
        "kind": kind,
        "telegram_file_id": telegram_file_id,
        "duration_seconds": duration_seconds,
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
    transcript: str | None = None,
    transcript_confidence: float | None = None,
    amend: Mapping[str, Any] | None = None,
) -> Task | None:
    """Шаг второй: разбор, ответ бота, задача, напоминания и память — одной транзакцией.

    Возвращает заведённую задачу; `None` — когда задачи и не должно быть
    (разговор, сведение о себе). Владелец передаётся явно и сверяется с
    владельцем сообщения на стороне базы (`techspec/04-access.md` §4.3).

    `reminders` — список `{stage, fire_at}` от `services/reminders.py` (§3.5):
    напоминания рождаются вместе с задачей, иначе отказ между двумя вставками
    оставил бы задачу, о которой некому напомнить. `facts` — список
    `{category, text, status}` (§3.7): статус уже проставлен ботом, повтор
    по владельцу, категории и тексту база схлопывает сама. `transcript` —
    расшифровка голоса (§9.3): она становится текстом сообщения; у текста и
    у нерасслышанного голоса её нет, и текст не трогается.

    `amend` — ответ на открытый вопрос (`techspec/10-dialog.md` §10.2):
    `{task_id, fields, reminders}`. Тогда `task` пуст, а база дополняет
    прежнюю задачу и заменяет её неотправленные напоминания; возвращается
    она же. Чужая или закрытая задача — отказ базы, а не тихий пропуск. Открытые вопросы
    владельца база снимает при любом разборе сама (§3.4).
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
        "transcript": transcript,
        "transcript_confidence": transcript_confidence,
        "amend": amend,
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


async def open_question(
    db: Client, *, owner_telegram_id: int, since: datetime
) -> OpenQuestion | None:
    """Последний открытый вопрос владельца, заданный не раньше `since`.

    Срок жизни вопроса (сутки, §10.3) решает слой выше и передаёт сюда
    границу. Вопрос у закрытой задачи не в счёт: дополнять нечего.
    """
    rows = await ask(
        lambda: (
            db.table(TASKS_TABLE)
            .select(QUESTION_COLUMNS)
            .eq("owner_telegram_id", owner_telegram_id)
            .eq("status", ACTIVE_STATUS)
            .gte("question_asked_at", since.isoformat())
            .order("question_asked_at", desc=True)
            .limit(1)
            .execute()
            .data
        )
    )
    if not isinstance(rows, list):
        raise DatabaseError("База вернула не список задач.")
    return _open_question_from_row(rows[0]) if rows else None
