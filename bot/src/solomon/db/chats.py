"""Личные чаты: согласие по площадке, сообщения, разборы и «ждёт ответа».

Закон тот же, что у `tasks.py` и `searches.py`: `owner_telegram_id`
именованный и без значения по умолчанию в каждой функции, и он же уходит в
запрос — ключ service-role правила доступа обходит, поэтому разделение по
владельцу держит код (`techspec/04-access.md` §4.3).

Что делать с чатом — когда разбирать, что сказать владельцу, — решает бот
(`services/chats.py`); база помнит переписку семь дней, сама не даёт
сохранить сообщение без согласия и пишет разбор одной транзакцией
(`techspec/03-schema.md` §3.11–3.15, `techspec/25-chats.md`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, cast, get_args

from supabase import Client

from solomon.db.rpc import DatabaseError, ask, moment, single_row

CONNECT_FUNCTION = "connect_chat_source"
MARK_ASKED_FUNCTION = "mark_consent_asked"
ANSWER_CONSENT_FUNCTION = "answer_consent"
STORE_FUNCTION = "store_chat_message"
TRANSCRIPT_FUNCTION = "set_chat_transcript"
EDIT_FUNCTION = "edit_chat_message"
ERASE_FUNCTION = "erase_chat_messages"
TO_ANALYZE_FUNCTION = "chats_to_analyze"
RECORD_FUNCTION = "record_chat_analysis"
FAILED_FUNCTION = "chat_failed"
SKIP_FUNCTION = "skip_chat_messages"
REPORT_FUNCTION = "chat_report"
REPORT_SENT_FUNCTION = "mark_chat_report_sent"
DROP_FUNCTION = "drop_chat_task"
WAITING_FUNCTION = "chats_waiting"
REMINDED_FUNCTION = "mark_waiting_reminded"
ERASE_OLD_FUNCTION = "erase_old_chat_messages"
REPORTED_FUNCTION = "reported_chat"

SOURCES_TABLE = "chat_sources"
MESSAGES_TABLE = "chat_messages"
ANALYSES_TABLE = "chat_analyses"
SOURCE_COLUMNS = "platform, connection_id, is_enabled, asked_at, consented_at, declined_at"
MESSAGE_COLUMNS = "id, direction, sender, sent_at, kind, text, erased_at"
# Сообщение для часов по сферам (`techspec/31-hours.md` §31.1): чат, чьё, время
# площадки — и сфера чата вложенной строкой: сессия идёт в нынешнюю сферу чата.
STAMP_COLUMNS = "id, thread_id, direction, sent_at, chat_threads(sphere_id)"
# PostgREST отдаёт не больше тысячи строк за раз: неделя переписки читается
# страницами, а сверх предела страниц — режется, а не падает.
STAMP_PAGE = 1000
STAMP_PAGES = 50

# Площадка (§25.1): общий путь для Telegram (этап 025), Instagram (026) и MAX
# (027).
Platform = Literal["telegram", "instagram", "max"]
# `in` — собеседник, `out` — владелец.
Direction = Literal["in", "out"]
ChatKind = Literal["text", "voice", "video_note", "photo", "other"]
# Что вышло у приёма (§3.15): сохранено, повтор или почему не сохранено.
StoreOutcome = Literal[
    "stored", "repeat", "no_source", "unknown_connection", "disabled", "no_consent"
]


@dataclass(frozen=True, slots=True)
class ChatSource:
    """Площадка и согласие на неё (§3.11)."""

    platform: Platform
    connection_id: str | None
    is_enabled: bool
    asked_at: datetime | None
    consented_at: datetime | None
    declined_at: datetime | None


@dataclass(frozen=True, slots=True)
class Stored:
    """Итог приёма: что вышло и id сообщения (у `stored` и `repeat`)."""

    outcome: StoreOutcome
    message_id: str | None


@dataclass(frozen=True, slots=True)
class ChatToAnalyze:
    """Чат, кусок которого пора разбирать (§25.3)."""

    thread_id: str
    platform: Platform
    chat_key: str
    name: str


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """Сообщение чата для строки переписки: стёртое — без текста (§25.1)."""

    id: str
    direction: Direction
    sender: str
    sent_at: datetime
    kind: ChatKind
    text: str
    erased: bool


@dataclass(frozen=True, slots=True)
class ChatStamp:
    """Сообщение чата для сессий переписки (`techspec/31-hours.md` §31.1): чат,
    его нынешняя сфера, время площадки и чьё оно — `mine` у владельца."""

    thread_id: str
    sphere_id: str | None
    sent_at: datetime
    mine: bool


@dataclass(frozen=True, slots=True)
class ReportLine:
    """Дело разбора в сообщении владельцу — какой задача стала сейчас (§25.4)."""

    item: int
    task_id: str
    title: str
    due_at: datetime | None
    due_precision: str | None
    promise: str | None
    status: str


@dataclass(frozen=True, slots=True)
class ChatReport:
    """Сообщение о разборе: площадка, чат, «с кем» и дела по номерам.

    `chat_key` и `username` — ключ чата и нынешнее имя пользователя
    собеседника: из них строится «Открыть чат» (§25.4). `sphere` — нынешняя
    сфера чата (`techspec/30-spheres.md` §30.3).
    """

    platform: Platform
    chat_key: str
    chat_name: str
    username: str | None
    chat_with: str | None
    lines: tuple[ReportLine, ...]
    sphere: str | None = None


@dataclass(frozen=True, slots=True)
class ReportedChat:
    """Чат, о разборе которого бот написал владельцу этим сообщением
    (`techspec/30-spheres.md` §30.2): ответ на отчёт меняет сферу чата.
    `chat_with` — имя в творительном падеже из разбора, у заметок пусто."""

    thread_id: str
    platform: Platform
    chat_name: str
    chat_with: str | None
    sphere: str | None


@dataclass(frozen=True, slots=True)
class WaitingChat:
    """Чат, где владелец не ответил (§25.4): с какого времени, о чём, кому —
    и ключ чата с именем пользователя для «Открыть чат»."""

    thread_id: str
    platform: Platform
    chat_key: str
    name: str
    username: str | None
    since: datetime
    about: str
    to: str


@dataclass(frozen=True, slots=True)
class ChatTrace:
    """След разбора (§25.3): ответ модели, модель, токены и длительность."""

    analysis: Mapping[str, Any]
    model: str
    input_tokens: int
    output_tokens: int
    duration_ms: int


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


def _count(data: Any, what: str) -> int:
    """Число из функции; не число — отказ."""
    value = single_row(data)
    if isinstance(value, bool) or not isinstance(value, int):
        raise DatabaseError(f"База не назвала число, {what}: {data!r}.")
    return value


def _optional_id(data: Any, what: str) -> str | None:
    """Id из функции или пусто; не строка — отказ."""
    value = single_row(data)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise DatabaseError(f"База не вернула id, {what}: {data!r}.")
    return value


def _optional_text(value: Any) -> str | None:
    return None if value is None else str(value)


def _optional_moment(value: Any, field: str) -> datetime | None:
    return None if value is None else moment(value, field)


def _platform(value: Any) -> Platform:
    if value not in get_args(Platform):
        raise DatabaseError(f"База вернула незнакомую площадку: {value!r}.")
    platform: Platform = value
    return platform


def _source_from_row(row: Any) -> ChatSource | None:
    """Площадка из строки; пустая составная строка — площадки нет."""
    if row is None or (isinstance(row, Mapping) and row.get("platform") is None):
        return None
    if not isinstance(row, Mapping):
        raise DatabaseError("База вернула не строку площадки.")
    try:
        connection = row["connection_id"]
        return ChatSource(
            platform=_platform(row["platform"]),
            connection_id=_optional_text(connection),
            is_enabled=bool(row["is_enabled"]),
            asked_at=_optional_moment(row["asked_at"], "asked_at"),
            consented_at=_optional_moment(row["consented_at"], "consented_at"),
            declined_at=_optional_moment(row["declined_at"], "declined_at"),
        )
    except KeyError as error:
        raise DatabaseError(f"В ответе базы нет поля площадки: {error}.") from error


def _message_from_row(row: Any) -> ChatMessage:
    """Сообщение чата из строки. Неполное — отказ, а не строка без времени."""
    if not isinstance(row, Mapping):
        raise DatabaseError("База вернула не строку сообщения чата.")
    try:
        direction = row["direction"]
        kind = row["kind"]
        if direction not in get_args(Direction) or kind not in get_args(ChatKind):
            raise DatabaseError(f"База вернула сообщение чата не того вида: {direction}, {kind}.")
        return ChatMessage(
            id=str(row["id"]),
            direction=direction,
            sender=str(row["sender"] or ""),
            sent_at=moment(row["sent_at"], "sent_at"),
            kind=kind,
            text=str(row["text"] or ""),
            erased=row["erased_at"] is not None,
        )
    except KeyError as error:
        raise DatabaseError(f"В ответе базы нет поля сообщения чата: {error}.") from error


# --- Согласие ---------------------------------------------------------------


async def connect_chat_source(
    db: Client,
    *,
    owner_telegram_id: int,
    platform: Platform,
    connection_id: str | None,
    is_enabled: bool,
) -> ChatSource:
    """Площадка подключена или отключена (§25.2, §25.5): завести или обновить."""
    params = {
        "owner_telegram_id": owner_telegram_id,
        "platform": platform,
        "connection_id": connection_id,
        "is_enabled": is_enabled,
    }
    data = await ask(lambda: db.rpc(CONNECT_FUNCTION, params).execute().data)
    source = _source_from_row(single_row(data))
    if source is None:
        raise DatabaseError("База не вернула площадку после подключения.")
    return source


async def sources_to_ask(db: Client, *, owner_telegram_id: int) -> list[ChatSource]:
    """Площадки, о согласии на которые пора спросить (§25.5): включены, вопрос
    не уходил, решения нет."""
    rows = await ask(
        lambda: (
            db.table(SOURCES_TABLE)
            .select(SOURCE_COLUMNS)
            .eq("owner_telegram_id", owner_telegram_id)
            .eq("is_enabled", True)
            .is_("asked_at", None)
            .is_("consented_at", None)
            .is_("declined_at", None)
            .execute()
            .data
        )
    )
    sources = [_source_from_row(row) for row in _rows(rows, "площадки")]
    return [source for source in sources if source is not None]


async def chat_source(
    db: Client, *, owner_telegram_id: int, platform: Platform
) -> ChatSource | None:
    """Площадка владельца и согласие на неё (§25.5). Нет строки — `None`."""
    rows = await ask(
        lambda: (
            db.table(SOURCES_TABLE)
            .select(SOURCE_COLUMNS)
            .eq("owner_telegram_id", owner_telegram_id)
            .eq("platform", platform)
            .limit(1)
            .execute()
            .data
        )
    )
    found = _rows(rows, "площадка")
    return _source_from_row(found[0]) if found else None


async def mark_consent_asked(db: Client, *, owner_telegram_id: int, platform: Platform) -> bool:
    """Вопрос о согласии ушёл. `False` — уже спрашивали или решение есть."""
    params = {"owner_telegram_id": owner_telegram_id, "platform": platform}
    data = await ask(lambda: db.rpc(MARK_ASKED_FUNCTION, params).execute().data)
    return _answer(data, "помечен ли вопрос о согласии")


async def answer_consent(
    db: Client, *, owner_telegram_id: int, platform: Platform, agreed: bool
) -> ChatSource | None:
    """«Согласен» или «Не надо» (§25.5). `None` — площадки нет."""
    params = {"owner_telegram_id": owner_telegram_id, "platform": platform, "agreed": agreed}
    data = await ask(lambda: db.rpc(ANSWER_CONSENT_FUNCTION, params).execute().data)
    return _source_from_row(single_row(data))


# --- Приём ------------------------------------------------------------------


async def store_chat_message(
    db: Client,
    *,
    owner_telegram_id: int,
    platform: Platform,
    connection_id: str | None,
    chat_key: str,
    chat_name: str,
    external_id: str,
    direction: Direction,
    sender: str,
    sent_at: datetime,
    kind: ChatKind,
    text: str,
    tracks_waiting: bool = True,
    username: str | None = None,
) -> Stored:
    """Сообщение чата в базу (§25.1). Подключение и согласие сверяет база:
    без них ничего не пишется, и ответ говорит почему.

    `username` — имя пользователя собеседника для «Открыть чат» (§3.15):
    `None` — площадка его не знает, и в чате остаётся прежнее; пустое —
    имени больше нет."""
    params = {
        "owner_telegram_id": owner_telegram_id,
        "platform": platform,
        "connection_id": connection_id,
        "chat_key": chat_key,
        "chat_name": chat_name,
        "external_id": external_id,
        "direction": direction,
        "sender": sender,
        "sent_at": sent_at.isoformat(),
        "kind": kind,
        "message_text": text,
        "tracks_waiting": tracks_waiting,
        "username": username,
    }
    data = await ask(lambda: db.rpc(STORE_FUNCTION, params).execute().data)
    row = single_row(data)
    if not isinstance(row, Mapping):
        raise DatabaseError(f"База не ответила на приём сообщения чата: {data!r}.")
    answer = row.get("outcome")
    if answer not in get_args(StoreOutcome):
        raise DatabaseError(f"База ответила на приём непонятно: {answer!r}.")
    outcome = cast(StoreOutcome, answer)
    message_id = row.get("message_id")
    if outcome in ("stored", "repeat") and not message_id:
        raise DatabaseError("База сохранила сообщение чата без id.")
    return Stored(outcome=outcome, message_id=None if message_id is None else str(message_id))


async def set_chat_transcript(
    db: Client, *, owner_telegram_id: int, message_id: str, transcript: str
) -> bool:
    """Расшифровка голосового (§25.2). `False` — уже разобрано или стёрто."""
    params = {
        "owner_telegram_id": owner_telegram_id,
        "message_id": message_id,
        "transcript": transcript,
    }
    data = await ask(lambda: db.rpc(TRANSCRIPT_FUNCTION, params).execute().data)
    return _answer(data, "записана ли расшифровка")


async def edit_chat_message(
    db: Client,
    *,
    owner_telegram_id: int,
    platform: Platform,
    connection_id: str | None,
    chat_key: str,
    external_id: str,
    text: str,
) -> bool:
    """Правка на площадке (§25.1): до разбора меняет текст. `False` — нечего."""
    params = {
        "owner_telegram_id": owner_telegram_id,
        "platform": platform,
        "connection_id": connection_id,
        "chat_key": chat_key,
        "external_id": external_id,
        "message_text": text,
    }
    data = await ask(lambda: db.rpc(EDIT_FUNCTION, params).execute().data)
    return _answer(data, "записана ли правка сообщения чата")


async def erase_chat_messages(
    db: Client,
    *,
    owner_telegram_id: int,
    platform: Platform,
    connection_id: str | None,
    chat_key: str,
    external_ids: Sequence[str],
) -> int:
    """Удаление на площадке стирает текст (§25.1). Возвращает, сколько стёрто."""
    params = {
        "owner_telegram_id": owner_telegram_id,
        "platform": platform,
        "connection_id": connection_id,
        "chat_key": chat_key,
        "external_ids": list(external_ids),
    }
    data = await ask(lambda: db.rpc(ERASE_FUNCTION, params).execute().data)
    return _count(data, "сколько сообщений чата стёрто")


# --- Разбор -----------------------------------------------------------------


async def chats_to_analyze(
    db: Client, *, owner_telegram_id: int, quiet_before: datetime, stale_before: datetime
) -> list[ChatToAnalyze]:
    """Чаты к разбору (§25.3): затихли или ждут дольше двух часов, старшие первыми."""
    params = {
        "owner_telegram_id": owner_telegram_id,
        "quiet_before": quiet_before.isoformat(),
        "stale_before": stale_before.isoformat(),
    }
    rows = await ask(lambda: db.rpc(TO_ANALYZE_FUNCTION, params).execute().data)
    found: list[ChatToAnalyze] = []
    for row in _rows(rows, "чаты к разбору"):
        if not isinstance(row, Mapping) or row.get("thread_id") is None:
            raise DatabaseError("База вернула чат к разбору без id.")
        found.append(
            ChatToAnalyze(
                thread_id=str(row["thread_id"]),
                platform=_platform(row.get("platform")),
                chat_key=str(row.get("chat_key") or ""),
                name=str(row.get("name") or ""),
            )
        )
    return found


async def new_chat_messages(
    db: Client, *, owner_telegram_id: int, thread_id: str, limit: int
) -> list[ChatMessage]:
    """Неразобранные сообщения чата, старшие первыми, не больше `limit`."""
    rows = await ask(
        lambda: (
            db.table(MESSAGES_TABLE)
            .select(MESSAGE_COLUMNS)
            .eq("owner_telegram_id", owner_telegram_id)
            .eq("thread_id", thread_id)
            .is_("analysis_id", None)
            .order("sent_at")
            .order("created_at")
            .limit(limit)
            .execute()
            .data
        )
    )
    return [_message_from_row(row) for row in _rows(rows, "новые сообщения чата")]


async def earlier_chat_messages(
    db: Client, *, owner_telegram_id: int, thread_id: str, limit: int
) -> list[ChatMessage]:
    """До `limit` последних разобранных и не стёртых сообщений чата — «раньше»
    (§25.3), от старых к новым."""
    rows = await ask(
        lambda: (
            db.table(MESSAGES_TABLE)
            .select(MESSAGE_COLUMNS)
            .eq("owner_telegram_id", owner_telegram_id)
            .eq("thread_id", thread_id)
            .not_.is_("analysis_id", None)
            .is_("erased_at", None)
            .order("sent_at", desc=True)
            .limit(limit)
            .execute()
            .data
        )
    )
    found = [_message_from_row(row) for row in _rows(rows, "прежние сообщения чата")]
    return sorted(found, key=lambda message: message.sent_at)


async def record_chat_analysis(
    db: Client,
    *,
    owner_telegram_id: int,
    thread_id: str,
    message_ids: Sequence[str],
    trace: ChatTrace,
    chat_with: str | None,
    waiting: Mapping[str, str] | None,
    tasks: Sequence[Mapping[str, Any]],
    sphere: str | None = None,
) -> str | None:
    """Разбор одной транзакцией (§25.3, §3.15): строка разбора, пометка,
    задачи с напоминаниями и «ждёт ответа». `None` — кусок уже разобран.
    `sphere` — сфера чата из списка (`techspec/30-spheres.md` §30.2): уходит,
    только когда есть, — вызов без неё работает и на базе до миграции 032."""
    params = {
        "owner_telegram_id": owner_telegram_id,
        "thread_id": thread_id,
        "message_ids": list(message_ids),
        "analysis": dict(trace.analysis),
        "ai_model": trace.model,
        "input_tokens": trace.input_tokens,
        "output_tokens": trace.output_tokens,
        "duration_ms": trace.duration_ms,
        "chat_with": chat_with,
        "waiting": None if waiting is None else dict(waiting),
        "tasks": [dict(task) for task in tasks],
    }
    if sphere is not None:
        params["sphere"] = sphere
    data = await ask(lambda: db.rpc(RECORD_FUNCTION, params).execute().data)
    return _optional_id(data, "id разбора")


async def chat_failed(db: Client, *, owner_telegram_id: int, thread_id: str) -> int | None:
    """Неудача разбора подряд (§25.3): сколько их теперь. `None` — чата нет."""
    params = {"owner_telegram_id": owner_telegram_id, "thread_id": thread_id}
    data = await ask(lambda: db.rpc(FAILED_FUNCTION, params).execute().data)
    if single_row(data) is None:
        return None
    return _count(data, "сколько неудач подряд")


async def skip_chat_messages(
    db: Client, *, owner_telegram_id: int, thread_id: str, message_ids: Sequence[str]
) -> str | None:
    """Кусок без разбора (§25.3): разобран без дел. `None` — помечать нечего."""
    params = {
        "owner_telegram_id": owner_telegram_id,
        "thread_id": thread_id,
        "message_ids": list(message_ids),
    }
    data = await ask(lambda: db.rpc(SKIP_FUNCTION, params).execute().data)
    return _optional_id(data, "id пропуска")


# --- Что видит владелец -------------------------------------------------------


async def reports_to_send(db: Client, *, owner_telegram_id: int) -> list[str]:
    """Разборы с делами, о которых владелец ещё не узнал (§25.4), по порядку."""
    rows = await ask(
        lambda: (
            db.table(ANALYSES_TABLE)
            .select("id")
            .eq("owner_telegram_id", owner_telegram_id)
            .is_("reported_at", None)
            .neq("items", 0)
            .order("created_at")
            .execute()
            .data
        )
    )
    found: list[str] = []
    for row in _rows(rows, "разборы к отправке"):
        if not isinstance(row, Mapping) or not row.get("id"):
            raise DatabaseError("База вернула разбор без id.")
        found.append(str(row["id"]))
    return found


async def chat_report(db: Client, *, owner_telegram_id: int, analysis_id: str) -> ChatReport | None:
    """Дела разбора, какими они стали сейчас (§25.4). `None` — дел нет."""
    params = {"owner_telegram_id": owner_telegram_id, "analysis_id": analysis_id}
    rows = _rows(await ask(lambda: db.rpc(REPORT_FUNCTION, params).execute().data), "отчёт")
    if not rows:
        return None
    lines: list[ReportLine] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise DatabaseError("База вернула не строку отчёта.")
        try:
            lines.append(
                ReportLine(
                    item=int(row["item"]),
                    task_id=str(row["task_id"]),
                    title=str(row["title"]),
                    due_at=_optional_moment(row["due_at"], "due_at"),
                    due_precision=_optional_text(row["due_precision"]),
                    promise=_optional_text(row["promise"]),
                    status=str(row["status"]),
                )
            )
        except (KeyError, TypeError, ValueError) as error:
            raise DatabaseError(f"В ответе базы не разобрать дело отчёта: {error}.") from error
    first = rows[0]
    return ChatReport(
        platform=_platform(first.get("platform")),
        chat_key=str(first.get("chat_key") or ""),
        chat_name=str(first.get("chat_name") or ""),
        username=_optional_text(first.get("username")),
        chat_with=_optional_text(first.get("chat_with")),
        lines=tuple(lines),
        sphere=_optional_text(first.get("sphere")),
    )


async def reported_chat(
    db: Client, *, owner_telegram_id: int, telegram_message_id: int
) -> ReportedChat | None:
    """Чат, о разборе которого бот написал сообщением `telegram_message_id`
    (`techspec/30-spheres.md` §30.2) — или `None`, если это не отчёт."""
    params = {"owner_telegram_id": owner_telegram_id, "telegram_message_id": telegram_message_id}
    rows = _rows(await ask(lambda: db.rpc(REPORTED_FUNCTION, params).execute().data), "отчёт")
    if not rows:
        return None
    row = rows[0]
    if not isinstance(row, Mapping) or not row.get("thread_id"):
        raise DatabaseError("База вернула чат отчёта без id.")
    return ReportedChat(
        thread_id=str(row["thread_id"]),
        platform=_platform(row.get("platform")),
        chat_name=str(row.get("chat_name") or ""),
        chat_with=_optional_text(row.get("chat_with")),
        sphere=_optional_text(row.get("sphere")),
    )


async def mark_chat_report_sent(
    db: Client, *, owner_telegram_id: int, analysis_id: str, telegram_message_id: int | None
) -> bool:
    """Сообщение о разборе ушло — «отправить → пометить». `False` — уже помечено."""
    params = {
        "owner_telegram_id": owner_telegram_id,
        "analysis_id": analysis_id,
        "telegram_message_id": telegram_message_id,
    }
    data = await ask(lambda: db.rpc(REPORT_SENT_FUNCTION, params).execute().data)
    return _answer(data, "помечен ли отчёт отправленным")


async def drop_chat_task(
    db: Client, *, owner_telegram_id: int, analysis_id: str, item: int
) -> str | None:
    """«Убрать» (§25.4): статус задачи после нажатия. `None` — такой нет."""
    params = {"owner_telegram_id": owner_telegram_id, "analysis_id": analysis_id, "item": item}
    row = single_row(await ask(lambda: db.rpc(DROP_FUNCTION, params).execute().data))
    if row is None or (isinstance(row, Mapping) and row.get("id") is None):
        return None
    if not isinstance(row, Mapping) or not row.get("status"):
        raise DatabaseError("База вернула задачу без статуса.")
    return str(row["status"])


async def chats_waiting(
    db: Client, *, owner_telegram_id: int, asked_before: datetime
) -> list[WaitingChat]:
    """Кому владелец не ответил дольше трёх часов (§25.4)."""
    params = {"owner_telegram_id": owner_telegram_id, "asked_before": asked_before.isoformat()}
    rows = await ask(lambda: db.rpc(WAITING_FUNCTION, params).execute().data)
    found: list[WaitingChat] = []
    for row in _rows(rows, "ждущие ответа чаты"):
        if not isinstance(row, Mapping):
            raise DatabaseError("База вернула не строку ждущего чата.")
        try:
            about = row["waiting_about"]
            to = row["waiting_to"]
            if row["thread_id"] is None or not about or not to:
                raise DatabaseError("База вернула ждущий чат без вопроса.")
            found.append(
                WaitingChat(
                    thread_id=str(row["thread_id"]),
                    platform=_platform(row["platform"]),
                    chat_key=str(row["chat_key"] or ""),
                    name=str(row["name"] or ""),
                    username=_optional_text(row["username"]),
                    since=moment(row["waiting_since"], "waiting_since"),
                    about=str(about),
                    to=str(to),
                )
            )
        except KeyError as error:
            raise DatabaseError(f"В ответе базы нет поля ждущего чата: {error}.") from error
    return found


async def mark_waiting_reminded(
    db: Client, *, owner_telegram_id: int, thread_id: str, since: datetime
) -> bool:
    """Напоминание о неотвеченном ушло. `False` — вопрос уже другой или снят."""
    params = {
        "owner_telegram_id": owner_telegram_id,
        "thread_id": thread_id,
        "waiting_since": since.isoformat(),
    }
    data = await ask(lambda: db.rpc(REMINDED_FUNCTION, params).execute().data)
    return _answer(data, "помечено ли напоминание о неотвеченном")


async def erase_old_chat_messages(db: Client, *, owner_telegram_id: int, before: datetime) -> int:
    """Срок хранения (§25.1): стереть текст пришедшего раньше `before`."""
    params = {"owner_telegram_id": owner_telegram_id, "before": before.isoformat()}
    data = await ask(lambda: db.rpc(ERASE_OLD_FUNCTION, params).execute().data)
    return _count(data, "сколько сообщений чата стёрто сроком")


# --- Часы по сферам ---------------------------------------------------------------


def _stamp_from_row(row: Any) -> ChatStamp:
    """Сообщение для сессий из строки; без чата, направления или времени — отказ."""
    if not isinstance(row, Mapping):
        raise DatabaseError("База вернула не строку сообщения чата.")
    try:
        direction = row["direction"]
        if direction not in get_args(Direction):
            raise DatabaseError(f"База вернула сообщение чата не того вида: {direction}.")
        thread = row.get("chat_threads")
        sphere_id = thread.get("sphere_id") if isinstance(thread, Mapping) else None
        return ChatStamp(
            thread_id=str(row["thread_id"]),
            sphere_id=_optional_text(sphere_id),
            sent_at=moment(row["sent_at"], "sent_at"),
            mine=direction == "out",
        )
    except KeyError as error:
        raise DatabaseError(f"В ответе базы нет поля сообщения чата: {error}.") from error


async def chat_stamps(db: Client, *, owner_telegram_id: int, since: datetime) -> list[ChatStamp]:
    """Сообщения личных чатов владельца по времени площадки не раньше `since`
    (`techspec/31-hours.md` §31.1) — стёртые тоже: время и направление у них
    остаются. Страницами по `STAMP_PAGE`, порядок — по времени и id, чтобы
    страницы не перекрывались; больше `STAMP_PAGES` страниц — счёт по первым,
    часы и так примерные."""
    stamps: list[ChatStamp] = []
    for page in range(STAMP_PAGES):
        first = page * STAMP_PAGE

        def query(first: int = first) -> Any:
            return (
                db.table(MESSAGES_TABLE)
                .select(STAMP_COLUMNS)
                .eq("owner_telegram_id", owner_telegram_id)
                .gte("sent_at", since.isoformat())
                .order("sent_at")
                .order("id")
                .range(first, first + STAMP_PAGE - 1)
                .execute()
                .data
            )

        rows = _rows(await ask(query), "сообщения чатов для часов")
        stamps.extend(_stamp_from_row(row) for row in rows)
        if len(rows) < STAMP_PAGE:
            break
    return stamps
