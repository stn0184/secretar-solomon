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
PICK_TASK_FUNCTION = "pick_task"
TASKS_TABLE = "tasks"
MESSAGES_TABLE = "messages"
REMINDERS_TABLE = "reminders"
ACTIVE_STATUS = "active"
TASK_COLUMNS = "id, title, status"
# Поля задачи, которые нужны списку в промпте (`techspec/12-chat-edit.md`
# §12.2), правке словом и кнопкам «какую задачу» и «Вернуть» (§12.6), —
# с правилом повтора и разом (`techspec/13-repeat.md` §13.2).
DETAIL_COLUMNS = (
    "id, title, kind, status, due_at, due_precision, priority, promise, people, created_at, "
    "repeat, occurrence_at"
)
# Сообщение владельца, на которое ответили свайпом или по которому нажали
# кнопку кандидата: текст, задача, разбор и ответ бота (§12.2, §12.6).
STORED_MESSAGE_COLUMNS = "id, text, task_id, analysis, reply"
# Поля задачи, которые нужны промпту с открытым вопросом и слиянию ответа
# с задачей (`techspec/10-dialog.md` §10.2).
QUESTION_COLUMNS = (
    "id, title, kind, due_at, due_precision, priority, promise, people, "
    "open_question, question_asked_at, repeat"
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
    repeat: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class TaskDetails:
    """Задача со всеми полями, которые видит правка словом (§12.2).

    Строка списка открытых задач в промпте, цель правки и кнопки «Вернуть»:
    суть, вид, срок, срочность, обещание, люди — и статус, потому что по
    кнопке приходит и закрытая задача. `repeat` и `occurrence_at` — правило
    повтора и раз, который задача сейчас представляет (§13.2); у разовой
    оба пусты.
    """

    id: str
    title: str
    kind: str
    status: str
    due_at: datetime | None
    due_precision: str | None
    priority: str
    promise: str | None
    people: tuple[str, ...]
    created_at: datetime
    repeat: Mapping[str, Any] | None = None
    occurrence_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class TaskEvent:
    """Событие разговора о задаче: сообщение владельца или ушедшее напоминание.

    По более позднему из них бот называет модели последнюю задачу в
    разговоре (§12.2).
    """

    task_id: str
    at: datetime


@dataclass(frozen=True, slots=True)
class StoredMessage:
    """Сообщение владельца, как оно лежит в базе (§3.2).

    `text` у голосового — расшифровка; `analysis` — сырой разбор модели, из
    него кнопка кандидата строит правку (§12.6); `task_id` — задача, о
    которой сообщение, если бот её знает.
    """

    id: str
    text: str
    task_id: str | None
    analysis: Mapping[str, Any] | None
    reply: str | None


@dataclass(frozen=True, slots=True)
class PickedMessage:
    """Строка сообщения после `pick_task` (§3.4): чья правка в ней лежит.

    `task_id` и `reply` совпали с переданными — правку записал этот вызов;
    `task_id` другой — её сделали раньше; пуст — выбранную задачу уже
    закрыли, убрали или удалили, и ничего не записано.
    """

    id: str
    task_id: str | None
    reply: str | None


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


def _people(value: Any) -> tuple[str, ...]:
    """Список людей из строки задачи; не список — отказ, а не пустота."""
    if not isinstance(value, list):
        raise DatabaseError(f"В ответе базы не разобрать people: {value!r}.")
    return tuple(str(person) for person in value)


def repeat_of(value: Any) -> dict[str, Any] | None:
    """Правило повтора из строки (§13.2): объект или пусто; иное — отказ.

    Форму правила держит база (`repeat_valid`), здесь — только вид: правило
    строкой или списком читать вслепую нельзя.
    """
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise DatabaseError(f"В ответе базы не разобрать repeat: {value!r}.")
    return dict(value)


def optional_moment(value: Any, field: str) -> datetime | None:
    """Время или пусто — `moment` для колонок, которые бывают `null`."""
    return None if value is None else moment(value, field)


def task_details_from_row(row: Any) -> TaskDetails:
    """Разобрать строку задачи целиком. Неполная — отказ: править её вслепую нельзя."""
    if not isinstance(row, Mapping):
        raise DatabaseError("База вернула не строку задачи.")
    try:
        due_at = row["due_at"]
        return TaskDetails(
            id=str(row["id"]),
            title=str(row["title"]),
            kind=str(row["kind"]),
            status=str(row["status"]),
            due_at=None if due_at is None else moment(due_at, "due_at"),
            due_precision=_optional_text(row["due_precision"]),
            priority=str(row["priority"]),
            promise=_optional_text(row["promise"]),
            people=_people(row["people"]),
            created_at=moment(row["created_at"], "created_at"),
            # Повтор читается мягко: строка без этих колонок — разовая задача.
            repeat=repeat_of(row.get("repeat")),
            occurrence_at=optional_moment(row.get("occurrence_at"), "occurrence_at"),
        )
    except KeyError as error:
        raise DatabaseError(f"В ответе базы нет поля задачи: {error}.") from error


def _stored_message_from_row(row: Any) -> StoredMessage:
    """Разобрать строку сообщения. Разбор — объект или ничего."""
    if not isinstance(row, Mapping):
        raise DatabaseError("База вернула не строку сообщения.")
    try:
        analysis = row["analysis"]
        if analysis is not None and not isinstance(analysis, Mapping):
            raise DatabaseError(f"В ответе базы не разобрать analysis: {analysis!r}.")
        return StoredMessage(
            id=str(row["id"]),
            text=str(row["text"] or ""),
            task_id=_optional_text(row["task_id"]),
            analysis=analysis,
            reply=_optional_text(row["reply"]),
        )
    except KeyError as error:
        raise DatabaseError(f"В ответе базы нет поля сообщения: {error}.") from error


def _rows(data: Any, what: str) -> list[Any]:
    """Ответ выборки — список; иначе отказ."""
    if not isinstance(data, list):
        raise DatabaseError(f"База вернула не список {what}.")
    return data


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
            repeat=repeat_of(row.get("repeat")),
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
    edit: Mapping[str, Any] | None = None,
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
    она же. Чужая или закрытая задача — отказ базы, а не тихий пропуск.

    `edit` — правка задачи из списка словом (`techspec/12-chat-edit.md`
    §12.4): `{task_id, action, changes, schedule, question}`. Тогда `task` и
    `amend` пусты, а возвращается поправленная, закрытая или убранная
    задача. Не активная, чужая или удалённая — отказ базы, и откатывается
    всё, включая разбор и память.

    Открытые вопросы владельца база снимает сама (§3.4) — любой записью,
    кроме «не расслышал»: без разбора, задачи и поправки вопрос остаётся.
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
        "edit": edit,
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


async def list_open_tasks(db: Client, *, owner_telegram_id: int, limit: int) -> list[TaskDetails]:
    """Открытые задачи владельца для промпта (§12.2): сначала со сроком по
    возрастанию, потом без срока, новые выше — и не больше `limit`.

    Порядок нужен уже в запросе: он решает, какие задачи попадут в первые
    `limit`, а нумерацию бот всё равно ставит сам (`services/edits.py`).
    """
    rows = await ask(
        lambda: (
            db.table(TASKS_TABLE)
            .select(DETAIL_COLUMNS)
            .eq("owner_telegram_id", owner_telegram_id)
            .eq("status", ACTIVE_STATUS)
            .order("due_at", desc=False, nullsfirst=False)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
            .data
        )
    )
    return [task_details_from_row(row) for row in _rows(rows, "задач")]


async def task_details(db: Client, *, owner_telegram_id: int, task_id: str) -> TaskDetails | None:
    """Задача владельца по id — в любом статусе; чужая или удалённая — `None`.

    Её зовут кнопки (§12.6): `task_id` приходит из callback, то есть снаружи,
    и фильтр по владельцу здесь не формальность (инвариант 2).
    """
    rows = await ask(
        lambda: (
            db.table(TASKS_TABLE)
            .select(DETAIL_COLUMNS)
            .eq("owner_telegram_id", owner_telegram_id)
            .eq("id", task_id)
            .limit(1)
            .execute()
            .data
        )
    )
    found = _rows(rows, "задач")
    return task_details_from_row(found[0]) if found else None


def _event_from_row(row: Any, field: str) -> TaskEvent:
    if not isinstance(row, Mapping):
        raise DatabaseError("База вернула не строку события.")
    try:
        return TaskEvent(task_id=str(row["task_id"]), at=moment(row[field], field))
    except KeyError as error:
        raise DatabaseError(f"В ответе базы нет поля события: {error}.") from error


async def last_message_task(
    db: Client, *, owner_telegram_id: int, since: datetime
) -> TaskEvent | None:
    """Последнее сообщение владельца о задаче не раньше `since` (§12.2).

    «О задаче» — у сообщения есть `task_id` (§3.2): из него задачу завели,
    дополнили, поправили, закрыли или выбрали кнопкой.
    """
    rows = await ask(
        lambda: (
            db.table(MESSAGES_TABLE)
            .select("task_id, received_at")
            .eq("owner_telegram_id", owner_telegram_id)
            .not_.is_("task_id", "null")
            .gte("received_at", since.isoformat())
            .order("received_at", desc=True)
            .limit(1)
            .execute()
            .data
        )
    )
    found = _rows(rows, "сообщений")
    return _event_from_row(found[0], "received_at") if found else None


async def last_reminder_task(
    db: Client, *, owner_telegram_id: int, since: datetime
) -> TaskEvent | None:
    """Последнее ушедшее напоминание владельцу не раньше `since` (§12.2).

    Строка «Перенёс» после правки из приложения (§11.4) напоминанием не
    записывается и событием разговора не считается.
    """
    rows = await ask(
        lambda: (
            db.table(REMINDERS_TABLE)
            .select("task_id, sent_at")
            .eq("owner_telegram_id", owner_telegram_id)
            .gte("sent_at", since.isoformat())
            .order("sent_at", desc=True)
            .limit(1)
            .execute()
            .data
        )
    )
    found = _rows(rows, "напоминаний")
    return _event_from_row(found[0], "sent_at") if found else None


async def reminder_task_id(
    db: Client, *, owner_telegram_id: int, telegram_message_id: int
) -> str | None:
    """Задача напоминания, которое ушло сообщением `telegram_message_id` (§12.2).

    Так свайп на напоминание становится номером задачи. Нет такого
    напоминания — `None`: это другое сообщение бота.
    """
    rows = await ask(
        lambda: (
            db.table(REMINDERS_TABLE)
            .select("task_id")
            .eq("owner_telegram_id", owner_telegram_id)
            .eq("telegram_message_id", telegram_message_id)
            .limit(1)
            .execute()
            .data
        )
    )
    found = _rows(rows, "напоминаний")
    if not found:
        return None
    row = found[0]
    if not isinstance(row, Mapping) or "task_id" not in row:
        raise DatabaseError("В ответе базы нет задачи напоминания.")
    return str(row["task_id"])


async def message_by_telegram_id(
    db: Client, *, owner_telegram_id: int, chat_id: int, telegram_message_id: int
) -> StoredMessage | None:
    """Сообщение владельца по id в Telegram — для свайпа и кнопок (§12.2, §12.6).

    Ключ тот же, что у повтора в `record_message`: владелец, чат и номер
    сообщения. Нет в базе — `None`.
    """
    rows = await ask(
        lambda: (
            db.table(MESSAGES_TABLE)
            .select(STORED_MESSAGE_COLUMNS)
            .eq("owner_telegram_id", owner_telegram_id)
            .eq("chat_id", chat_id)
            .eq("telegram_message_id", telegram_message_id)
            .limit(1)
            .execute()
            .data
        )
    )
    found = _rows(rows, "сообщений")
    return _stored_message_from_row(found[0]) if found else None


async def pick_task(
    db: Client,
    *,
    owner_telegram_id: int,
    message_id: str,
    edit: Mapping[str, Any],
    reply: str,
) -> PickedMessage:
    """Выбор задачи кнопкой (§12.6): правка, `messages.task_id` и `reply` —
    одной транзакцией.

    Форма `edit` — та же, что у `record_understanding`. Что именно вышло,
    видно по строке сообщения в ответе (`PickedMessage`); чужое или
    несуществующее сообщение — отказ базы.
    """
    params = {
        "owner_telegram_id": owner_telegram_id,
        "message_id": message_id,
        "edit": dict(edit),
        "reply": reply,
    }
    data = single_row(await ask(lambda: db.rpc(PICK_TASK_FUNCTION, params).execute().data))
    if not isinstance(data, Mapping) or data.get("id") is None:
        raise DatabaseError("База не вернула сообщение.")
    return PickedMessage(
        id=str(data["id"]),
        task_id=_optional_text(data.get("task_id")),
        reply=_optional_text(data.get("reply")),
    )
