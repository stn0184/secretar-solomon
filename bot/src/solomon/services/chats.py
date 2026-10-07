"""Личные чаты: приём, согласие, разбор и что видит владелец.

Источник правды — `techspec/25-chats.md`. Общая часть для всех площадок:
Telegram (этап 025) приходит бизнес-обновлениями через `handlers.py`,
Instagram (026) и MAX (027) встанут сюда же своим приёмом.

- Приём (§25.1–25.2): сообщение уходит в базу, и база сама сверяет
  подключение и согласие — до «Согласен» ничего не хранится. Незнакомое
  подключение бот один раз спрашивает у Telegram (`ConnectionLookup`):
  владельца — принимает, чужое — запоминает и молчит. Голосовое
  расшифровывается сразу после записи.
- Согласие (§25.5): вопрос с кнопками «Согласен» и «Не надо» при первом
  включении площадки, порядок «отправить → пометить».

Сеть трогают только замыкания из сборки: база — через протокол `ChatStore`,
Telegram — `OwnerSender` и `ConnectionLookup`, Deepgram — `Transcriber`;
тесты подставляют свои. **В чаты владельца отсюда не уходит ничего**: всё,
что бот говорит о чатах, — владельцу в чат с Соломоном (§25.2). В журнал —
ни текстов, ни имён: id, числа и исходы.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, get_args

from supabase import Client

from solomon import texts
from solomon.config import Settings
from solomon.db import chats as db_chats
from solomon.db.chats import (
    ChatKind,
    ChatMessage,
    ChatReport,
    ChatSource,
    ChatToAnalyze,
    ChatTrace,
    Direction,
    Platform,
    Stored,
    WaitingChat,
)
from solomon.db.rpc import DatabaseError
from solomon.services.tasks import Button, PressOutcome
from solomon.services.transcription import Transcriber, Transcript

logger = logging.getLogger(__name__)

# Кнопки согласия (§25.5): `consent:<площадка>:yes|no`.
CONSENT_PREFIX = "consent:"
CONSENT_YES = "yes"
CONSENT_NO = "no"
PLATFORMS: tuple[Platform, ...] = get_args(Platform)
SPEECH_KINDS: tuple[ChatKind, ...] = ("voice", "video_note")


@dataclass(frozen=True, slots=True)
class Incoming:
    """Сообщение чата, как его увидел приём площадки (§25.1).

    `chat_key` — ключ чата на площадке, `chat_name` — собеседник или группа;
    `direction` — `out` у сообщения владельца. У голосового и кружка `text`
    пуст, а `file_id` — файл, который расшифрует Deepgram (§25.2).
    """

    platform: Platform
    connection_id: str | None
    chat_key: str
    chat_name: str
    external_id: str
    direction: Direction
    sender: str
    sent_at: datetime
    kind: ChatKind
    text: str
    file_id: str | None = None


@dataclass(frozen=True, slots=True)
class Connection:
    """Подключение, как его назвал Telegram (`getBusinessConnection`)."""

    user_id: int
    is_enabled: bool


def consent_data(platform: Platform, *, agreed: bool) -> str:
    """Callback кнопки согласия: `consent:telegram:yes`."""
    return f"{CONSENT_PREFIX}{platform}:{CONSENT_YES if agreed else CONSENT_NO}"


def parse_consent(data: str) -> tuple[Platform, bool] | None:
    """Площадка и ответ из callback согласия; кривой — `None`."""
    parts = data.removeprefix(CONSENT_PREFIX).split(":")
    if not data.startswith(CONSENT_PREFIX) or len(parts) != 2:
        return None
    platform, answer = parts
    if platform not in PLATFORMS or answer not in (CONSENT_YES, CONSENT_NO):
        return None
    found: Platform = next(known for known in PLATFORMS if known == platform)
    return found, answer == CONSENT_YES


def consent_buttons(platform: Platform) -> tuple[Button, ...]:
    """«Согласен» и «Не надо» под вопросом (§25.5)."""
    return (
        Button(texts.CONSENT_YES, consent_data(platform, agreed=True)),
        Button(texts.CONSENT_NO, consent_data(platform, agreed=False)),
    )


class ChatStore(Protocol):
    """Чаты владельца в базе (§3.11–3.15), владелец уже подставлен.

    Тест подменяет его памятью; обычная сборка — `DatabaseChatStore` поверх
    `db/chats.py`. Любой отказ — `DatabaseError`.
    """

    async def connect(
        self, platform: Platform, connection_id: str | None, enabled: bool
    ) -> ChatSource: ...

    async def sources_to_ask(self) -> list[ChatSource]: ...

    async def mark_asked(self, platform: Platform) -> bool: ...

    async def answer(self, platform: Platform, agreed: bool) -> ChatSource | None: ...

    async def store(self, incoming: Incoming, tracks_waiting: bool = True) -> Stored: ...

    async def set_transcript(self, message_id: str, transcript: str) -> bool: ...

    async def edit(self, incoming: Incoming) -> bool: ...

    async def erase(
        self,
        platform: Platform,
        connection_id: str | None,
        chat_key: str,
        external_ids: Sequence[str],
    ) -> int: ...

    async def to_analyze(
        self, quiet_before: datetime, stale_before: datetime
    ) -> list[ChatToAnalyze]: ...

    async def new_messages(self, thread_id: str, limit: int) -> list[ChatMessage]: ...

    async def earlier_messages(self, thread_id: str, limit: int) -> list[ChatMessage]: ...

    async def record(
        self,
        thread_id: str,
        message_ids: Sequence[str],
        trace: ChatTrace,
        chat_with: str | None,
        waiting: Mapping[str, str] | None,
        tasks: Sequence[Mapping[str, Any]],
    ) -> str | None: ...

    async def failed(self, thread_id: str) -> int | None: ...

    async def skip(self, thread_id: str, message_ids: Sequence[str]) -> str | None: ...

    async def reports_to_send(self) -> list[str]: ...

    async def report(self, analysis_id: str) -> ChatReport | None: ...

    async def report_sent(self, analysis_id: str, telegram_message_id: int | None) -> bool: ...

    async def drop(self, analysis_id: str, item: int) -> str | None: ...

    async def waiting(self, asked_before: datetime) -> list[WaitingChat]: ...

    async def reminded(self, thread_id: str, since: datetime) -> bool: ...

    async def erase_old(self, before: datetime) -> int: ...


class DatabaseChatStore:
    """`ChatStore` поверх настоящей базы. Владелец — из настроек, а не из
    сообщения (инвариант 2, `techspec/04-access.md` §4.3)."""

    def __init__(self, settings: Settings, db: Client) -> None:
        self._owner = settings.owner_telegram_id
        self._db = db

    async def connect(
        self, platform: Platform, connection_id: str | None, enabled: bool
    ) -> ChatSource:
        return await db_chats.connect_chat_source(
            self._db,
            owner_telegram_id=self._owner,
            platform=platform,
            connection_id=connection_id,
            is_enabled=enabled,
        )

    async def sources_to_ask(self) -> list[ChatSource]:
        return await db_chats.sources_to_ask(self._db, owner_telegram_id=self._owner)

    async def mark_asked(self, platform: Platform) -> bool:
        return await db_chats.mark_consent_asked(
            self._db, owner_telegram_id=self._owner, platform=platform
        )

    async def answer(self, platform: Platform, agreed: bool) -> ChatSource | None:
        return await db_chats.answer_consent(
            self._db, owner_telegram_id=self._owner, platform=platform, agreed=agreed
        )

    async def store(self, incoming: Incoming, tracks_waiting: bool = True) -> Stored:
        return await db_chats.store_chat_message(
            self._db,
            owner_telegram_id=self._owner,
            platform=incoming.platform,
            connection_id=incoming.connection_id,
            chat_key=incoming.chat_key,
            chat_name=incoming.chat_name,
            external_id=incoming.external_id,
            direction=incoming.direction,
            sender=incoming.sender,
            sent_at=incoming.sent_at,
            kind=incoming.kind,
            text=incoming.text,
            tracks_waiting=tracks_waiting,
        )

    async def set_transcript(self, message_id: str, transcript: str) -> bool:
        return await db_chats.set_chat_transcript(
            self._db, owner_telegram_id=self._owner, message_id=message_id, transcript=transcript
        )

    async def edit(self, incoming: Incoming) -> bool:
        return await db_chats.edit_chat_message(
            self._db,
            owner_telegram_id=self._owner,
            platform=incoming.platform,
            connection_id=incoming.connection_id,
            chat_key=incoming.chat_key,
            external_id=incoming.external_id,
            text=incoming.text,
        )

    async def erase(
        self,
        platform: Platform,
        connection_id: str | None,
        chat_key: str,
        external_ids: Sequence[str],
    ) -> int:
        return await db_chats.erase_chat_messages(
            self._db,
            owner_telegram_id=self._owner,
            platform=platform,
            connection_id=connection_id,
            chat_key=chat_key,
            external_ids=external_ids,
        )

    async def to_analyze(
        self, quiet_before: datetime, stale_before: datetime
    ) -> list[ChatToAnalyze]:
        return await db_chats.chats_to_analyze(
            self._db,
            owner_telegram_id=self._owner,
            quiet_before=quiet_before,
            stale_before=stale_before,
        )

    async def new_messages(self, thread_id: str, limit: int) -> list[ChatMessage]:
        return await db_chats.new_chat_messages(
            self._db, owner_telegram_id=self._owner, thread_id=thread_id, limit=limit
        )

    async def earlier_messages(self, thread_id: str, limit: int) -> list[ChatMessage]:
        return await db_chats.earlier_chat_messages(
            self._db, owner_telegram_id=self._owner, thread_id=thread_id, limit=limit
        )

    async def record(
        self,
        thread_id: str,
        message_ids: Sequence[str],
        trace: ChatTrace,
        chat_with: str | None,
        waiting: Mapping[str, str] | None,
        tasks: Sequence[Mapping[str, Any]],
    ) -> str | None:
        return await db_chats.record_chat_analysis(
            self._db,
            owner_telegram_id=self._owner,
            thread_id=thread_id,
            message_ids=message_ids,
            trace=trace,
            chat_with=chat_with,
            waiting=waiting,
            tasks=tasks,
        )

    async def failed(self, thread_id: str) -> int | None:
        return await db_chats.chat_failed(
            self._db, owner_telegram_id=self._owner, thread_id=thread_id
        )

    async def skip(self, thread_id: str, message_ids: Sequence[str]) -> str | None:
        return await db_chats.skip_chat_messages(
            self._db, owner_telegram_id=self._owner, thread_id=thread_id, message_ids=message_ids
        )

    async def reports_to_send(self) -> list[str]:
        return await db_chats.reports_to_send(self._db, owner_telegram_id=self._owner)

    async def report(self, analysis_id: str) -> ChatReport | None:
        return await db_chats.chat_report(
            self._db, owner_telegram_id=self._owner, analysis_id=analysis_id
        )

    async def report_sent(self, analysis_id: str, telegram_message_id: int | None) -> bool:
        return await db_chats.mark_chat_report_sent(
            self._db,
            owner_telegram_id=self._owner,
            analysis_id=analysis_id,
            telegram_message_id=telegram_message_id,
        )

    async def drop(self, analysis_id: str, item: int) -> str | None:
        return await db_chats.drop_chat_task(
            self._db, owner_telegram_id=self._owner, analysis_id=analysis_id, item=item
        )

    async def waiting(self, asked_before: datetime) -> list[WaitingChat]:
        return await db_chats.chats_waiting(
            self._db, owner_telegram_id=self._owner, asked_before=asked_before
        )

    async def reminded(self, thread_id: str, since: datetime) -> bool:
        return await db_chats.mark_waiting_reminded(
            self._db, owner_telegram_id=self._owner, thread_id=thread_id, since=since
        )

    async def erase_old(self, before: datetime) -> int:
        return await db_chats.erase_old_chat_messages(
            self._db, owner_telegram_id=self._owner, before=before
        )


class OwnerSender(Protocol):
    """Сообщение владельцу в чат с Соломоном — никогда в его чаты (§25.2).

    Возвращает id сообщения. Приходит из сборки замыканием над ботом: сервис
    не знает про aiogram.
    """

    async def __call__(self, *, text: str, buttons: Sequence[Button] = ()) -> int: ...


class ConnectionLookup(Protocol):
    """Чьё это подключение — спросить Telegram (`getBusinessConnection`)."""

    async def __call__(self, connection_id: str) -> Connection: ...


AudioLoader = Callable[[], Awaitable[bytes]]


class ChatService:
    """Чаты владельца: приём, согласие, разбор и сообщения о них.

    Собирается один раз при запуске бота. Чужие подключения процесс помнит,
    чтобы не спрашивать о них Telegram на каждом сообщении (бот один,
    `techspec/16-server.md` §16.3).
    """

    def __init__(
        self,
        settings: Settings,
        store: ChatStore,
        send: OwnerSender,
        lookup: ConnectionLookup | None = None,
        transcriber: Transcriber | None = None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._send = send
        # Без него незнакомое подключение не принимается — так собираются тесты.
        self._lookup = lookup
        # Без него голосовое остаётся «не расслышал» (§25.2).
        self._transcriber = transcriber
        self._foreign: set[str] = set()

    @classmethod
    def with_database(
        cls,
        settings: Settings,
        db: Client,
        send: OwnerSender,
        lookup: ConnectionLookup,
        transcriber: Transcriber,
    ) -> ChatService:
        """Обычная сборка: настоящая база, Telegram и Deepgram."""
        return cls(
            settings=settings,
            store=DatabaseChatStore(settings, db),
            send=send,
            lookup=lookup,
            transcriber=transcriber,
        )

    # --- Подключение и согласие (§25.2, §25.5) --------------------------------

    async def connected(
        self, *, platform: Platform, connection_id: str, user_id: int, enabled: bool
    ) -> None:
        """Telegram: бота подключили к аккаунту или отключили (§25.2).

        Чужое подключение — подключить бота может кто угодно — в журнал, и
        больше ничего: ни записи, ни ответа. Владельца — `enable`.
        """
        if user_id != self._settings.owner_telegram_id:
            self._foreign.add(connection_id)
            logger.info("Чужое подключение к боту (%s): не храню и не отвечаю", platform)
            return
        self._foreign.discard(connection_id)
        await self.enable(platform, connection_id, enabled=enabled)

    async def enable(
        self, platform: Platform, connection_id: str | None = None, *, enabled: bool = True
    ) -> None:
        """Площадка владельца включена или выключена — общий вход всех площадок
        (§25.5): Telegram — событие подключения, Instagram — первый опрос с
        ключом, MAX — первое сообщение боту. Включение — вопрос о согласии,
        если его ещё не было; выключение останавливает приём."""
        try:
            await self._store.connect(platform, connection_id, enabled)
        except DatabaseError as error:
            logger.error("Подключение %s не записано: %s", platform, error)
            return
        logger.info("Подключение %s: включено %s", platform, enabled)
        if enabled:
            await self.ask_consents()

    async def ask_consents(self) -> int:
        """Вопрос о согласии площадкам, о которых ещё не спрашивали (§25.5):
        «отправить → пометить». Не ушло — спросит следующий тик. Возвращает,
        сколько вопросов ушло."""
        try:
            sources = await self._store.sources_to_ask()
        except DatabaseError as error:
            logger.error("Площадки для вопроса о согласии не прочитаны: %s", error)
            return 0
        sent = 0
        for source in sources:
            try:
                await self._send(
                    text=texts.consent_question(source.platform),
                    buttons=consent_buttons(source.platform),
                )
            except Exception as error:  # noqa: BLE001 - отказ Telegram не роняет приём
                logger.warning(
                    "Вопрос о согласии (%s) не ушёл: %s", source.platform, type(error).__name__
                )
                continue
            sent += 1
            logger.info("Вопрос о согласии (%s) ушёл", source.platform)
            try:
                await self._store.mark_asked(source.platform)
            except DatabaseError as error:
                logger.error(
                    "Вопрос о согласии (%s) ушёл, но не помечен: %s", source.platform, error
                )
        return sent

    async def answer_consent(self, platform: Platform, agreed: bool) -> PressOutcome:
        """«Согласен» или «Не надо» (§25.5): сначала база, потом сообщение.

        Под ответом остаётся одна кнопка — поменять решение: «Больше не
        читать» после согласия, «Согласен» после отказа.
        """
        try:
            source = await self._store.answer(platform, agreed)
        except DatabaseError as error:
            logger.error("Ответ о согласии (%s) не записан: %s", platform, error)
            return PressOutcome(message=texts.CONSENT_NOT_SAVED, replace=False)
        if source is None:
            logger.warning("Ответ о согласии без площадки %s", platform)
            return PressOutcome(message=texts.CONSENT_UNKNOWN, replace=False)
        logger.info("Согласие %s: %s", platform, "да" if agreed else "нет")
        if agreed:
            button = Button(texts.CONSENT_STOP, consent_data(platform, agreed=False))
        else:
            button = Button(texts.CONSENT_YES, consent_data(platform, agreed=True))
        return PressOutcome(
            message=texts.consent_answered(platform, agreed=agreed),
            replace=True,
            buttons=(button,),
        )

    # --- Приём (§25.1–25.2) ----------------------------------------------------

    async def receive(
        self, incoming: Incoming, load_audio: AudioLoader | None = None
    ) -> Stored | None:
        """Сообщение чата — в базу; база сама сверяет подключение и согласие.

        Подключение базе незнакомо — Telegram называет его владельца: это
        владелец — подключение записывается, и сообщение пишется ещё раз;
        чужое — запоминается, ничего не хранится. Голосовое и кружок
        расшифровываются после записи. Возвращает итог записи; отказ базы —
        `None` и строка в журнал.
        """
        try:
            stored = await self._store.store(incoming)
            if (
                stored.outcome in ("no_source", "unknown_connection")
                and incoming.connection_id is not None
                and await self._adopt(incoming.platform, incoming.connection_id)
            ):
                stored = await self._store.store(incoming)
        except DatabaseError as error:
            logger.error("Сообщение чата (%s) не записано: %s", incoming.platform, error)
            return None
        if stored.outcome != "stored":
            logger.info("Сообщение чата (%s) не хранится: %s", incoming.platform, stored.outcome)
            return stored
        if incoming.kind in SPEECH_KINDS and stored.message_id is not None:
            await self._hear(incoming, stored.message_id, load_audio)
        return stored

    async def _adopt(self, platform: Platform, connection_id: str) -> bool:
        """Незнакомое подключение: спросить Telegram, чьё оно (§25.2).

        Владельца — записать (и спросить о согласии, если ещё не спрашивали):
        `True`. Чужое, неизвестное или Telegram не ответил — `False`.
        """
        if connection_id in self._foreign or self._lookup is None:
            return False
        try:
            connection = await self._lookup(connection_id)
        except Exception as error:  # noqa: BLE001 - отказ Telegram: сообщение не хранится
            logger.warning("Подключение не проверено: %s", type(error).__name__)
            return False
        await self.connected(
            platform=platform,
            connection_id=connection_id,
            user_id=connection.user_id,
            enabled=connection.is_enabled,
        )
        return connection.user_id == self._settings.owner_telegram_id

    async def _hear(
        self, incoming: Incoming, message_id: str, load_audio: AudioLoader | None
    ) -> None:
        """Расшифровка голосового (§25.2): не вышло — текст пустой, и в
        переписке строка «[голосовое, не расслышал]». Подсказка — имя
        собеседника."""
        if self._transcriber is None or load_audio is None:
            return
        try:
            audio = await load_audio()
        except Exception as error:  # noqa: BLE001 - не скачалось — «не расслышал»
            logger.warning("Голосовое чата не скачано: %s", type(error).__name__)
            return
        result = await self._transcriber.transcribe(audio, (incoming.chat_name,))
        if not isinstance(result, Transcript):
            return
        try:
            await self._store.set_transcript(message_id, result.text)
        except DatabaseError as error:
            logger.error("Расшифровка голосового чата не записана: %s", error)

    async def edited(self, incoming: Incoming) -> bool:
        """Правка на площадке (§25.1): до разбора меняет текст, после — ничего."""
        try:
            changed = await self._store.edit(incoming)
        except DatabaseError as error:
            logger.error("Правка сообщения чата не записана: %s", error)
            return False
        logger.info("Правка сообщения чата (%s): записана %s", incoming.platform, changed)
        return changed

    async def deleted(
        self,
        *,
        platform: Platform,
        connection_id: str | None,
        chat_key: str,
        external_ids: Sequence[str],
    ) -> int:
        """Удаление на площадке стирает текст (§25.1). Возвращает, сколько стёрто."""
        try:
            erased = await self._store.erase(platform, connection_id, chat_key, external_ids)
        except DatabaseError as error:
            logger.error("Удаление сообщений чата не записано: %s", error)
            return 0
        logger.info("Удалены сообщения чата (%s): стёрто %s", platform, erased)
        return erased
