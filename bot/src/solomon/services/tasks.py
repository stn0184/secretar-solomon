"""Приём поручения: сообщение в базу, разбор моделью, задача и ответ словами.

Порядок шагов — `techspec/03-schema.md` §3.4: сначала `record_message`
(поручение в базе с первой секунды, инвариант 5), потом модель, потом
`record_understanding` — разбор, ответ бота и задача одной транзакцией.
Повтор того же обновления отсекается на первом шаге: у сообщения уже есть
ответ, и модель не зовётся.

Голосовое идёт тем же путём с одним шагом посередине (`techspec/09-voice.md`
§9.3): сообщение с файлом пишется до того, как его расслышали, потом
скачивание и распознавание, дальше — как с текстом. Не расслышали — честный
ответ вместо задачи, и «Записал» не говорится (инвариант 4).

Уточняющий вопрос (`techspec/10-dialog.md`) живёт в том же хвосте: перед
моделью читается открытый вопрос владельца (не старше суток) и уходит ей в
промпт; модель решает, ответ ли это. Ответ дополняет прежнюю задачу
(`amend`) — новая не заводится; задача с вопросом записывается сразу, а
вопрос звучит второй фразой ответа. Не прочитался вопрос — разбор идёт без
него: поручение важнее контекста.

Правка задачи словом (`techspec/12-chat-edit.md`) — тоже здесь: перед
моделью читаются открытые задачи, последняя задача в разговоре и свайп и
уходят ей в промпт; разбор с `edit` правит, закрывает или убирает задачу
тем же вызовом `record_understanding`. Похожих задач несколько — вопрос с
кнопками, выбор пишет `pick`; кнопка «Вернуть» — `reopen`. Чистые правила
номеров, строк и правки для базы — в `services/edits.py`.

Обработчик ничего не решает: он зовёт `record_from_message` или
`record_from_voice` и отправляет то, что вернулось. Владелец берётся из
настроек, а не из сообщения — чужие обновления до этого слоя не доходят
(`middlewares.py`), и подставить чужой id из текста некому
(`techspec/04-access.md` §4.3).

Отказ базы разбирается в слова здесь, как в `db/health.py`: наружу выходит
причина для человека, а не трассировка.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from pydantic import ValidationError
from supabase import Client

from solomon import texts
from solomon.config import Settings
from solomon.db import reminders as db_reminders
from solomon.db import tasks as db_tasks
from solomon.db.reminders import Planned
from solomon.db.rpc import DatabaseError
from solomon.db.tasks import (
    MessageKind,
    OpenQuestion,
    PickedMessage,
    SavedMessage,
    SpeechKind,
    StoredMessage,
    Task,
    TaskDetails,
    TaskEvent,
)
from solomon.services import edits
from solomon.services.reminders import Planner, database_planner, next_fire_at
from solomon.services.transcription import (
    NotTranscribed,
    Transcriber,
    Transcript,
    TranscriptionResult,
)
from solomon.services.understanding import (
    TASK_KINDS,
    Analysis,
    AskedQuestion,
    Clock,
    OpenTask,
    SpeechQuality,
    TaskEdit,
    Understanding,
    UnderstandingService,
    Verdict,
    fact_status,
)

logger = logging.getLogger(__name__)

# Пересказ в ответе. В базу текст уходит целиком (инвариант 5), а ответ
# бота остаётся коротким и в лимит Telegram укладывается всегда.
SUMMARY_LIMIT = 200

# Сколько живёт вопрос без ответа (`techspec/10-dialog.md` §10.3): дольше —
# в промпт не попадает, и следующая запись его снимет (кроме «не расслышал»).
QUESTION_TTL = timedelta(hours=24)


def summarize(text: str) -> str:
    """Пересказ поручения для ответа: длинное обрезается, целое уже в базе."""
    if len(text) <= SUMMARY_LIMIT:
        return text
    return text[:SUMMARY_LIMIT] + "…"


@dataclass(frozen=True, slots=True)
class Button:
    """Inline-кнопка под ответом: надпись и callback (`techspec/12-chat-edit.md` §12.6)."""

    text: str
    data: str


@dataclass(frozen=True, slots=True)
class RecordOutcome:
    """Чем кончился приём поручения и что сказать человеку.

    `buttons` — кнопки под ответом, по одной в ряд: кандидаты «какую задачу»
    или «Вернуть» (§12.6). У повтора обновления их нет.
    """

    ok: bool
    message: str
    buttons: tuple[Button, ...] = ()


@dataclass(frozen=True, slots=True)
class PressOutcome:
    """Чем кончилось нажатие кнопки правки (§12.6).

    `replace` — сообщение с кнопками заменяется текстом `message` и кнопками
    `buttons`; иначе `message` — всплывающий ответ, а сообщение и его кнопки
    остаются, чтобы нажать ещё раз (как «Сделано», когда база не ответила).
    """

    message: str
    replace: bool
    buttons: tuple[Button, ...] = ()


class MessageRecorder(Protocol):
    """Первый шаг: сообщение в базу до всякого разбора.

    У голоса и кружка — вид, файл и длительность, а текст пуст до расшифровки
    (§9.3).
    """

    async def __call__(
        self,
        *,
        owner_telegram_id: int,
        chat_id: int,
        telegram_message_id: int,
        text: str,
        kind: MessageKind = "text",
        telegram_file_id: str | None = None,
        duration_seconds: int | None = None,
    ) -> SavedMessage: ...


class UnderstandingRecorder(Protocol):
    """Второй шаг: разбор, ответ бота и задача одной транзакцией.

    `transcript` — расшифровка голоса: тем же вызовом становится текстом
    сообщения (§9.3). `edit` — правка задачи словом (§12.4).
    """

    async def __call__(
        self,
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
    ) -> Task | None: ...


class QuestionReader(Protocol):
    """Открытый вопрос владельца, заданный не раньше `since` (§10.2)."""

    async def __call__(self, *, owner_telegram_id: int, since: datetime) -> OpenQuestion | None: ...


class Analyst(Protocol):
    """Разбор сообщения моделью — то, что подменяет тест вместо сети."""

    async def analyze(
        self,
        text: str,
        *,
        forwarded_from: str | None = None,
        spoken: SpeechQuality | None = None,
        open_question: AskedQuestion | None = None,
        tasks: Sequence[OpenTask] | None = None,
        last_task: int | None = None,
        swipe: str | None = None,
    ) -> Verdict: ...


# Скачивание звука из Telegram. Приходит из обработчика замыканием над
# `bot.download`, чтобы сервис не знал про aiogram — как отправка напоминаний
# приходит в `services/reminders.py`. Байты живут в памяти и на диск не
# пишутся (§9.2).
AudioLoader = Callable[[], Awaitable[bytes]]


@dataclass(frozen=True, slots=True)
class Swipe:
    """Сообщение, на которое владелец ответил свайпом (§12.2).

    `from_bot` — ответ на сообщение бота (напоминание или другое), иначе на
    своё. `text` — текст того сообщения в Telegram; у голосового его нет, и
    расшифровку бот берёт из базы.
    """

    telegram_message_id: int
    from_bot: bool
    text: str | None


@dataclass(frozen=True, slots=True)
class EditContext:
    """Подсказки модели для правки (§12.2): список, последняя задача, свайп.

    `tasks` — открытые задачи в порядке номеров (`edits.number_tasks`);
    `None` — блока 5 в промпте нет, и правки не бывает (пересланное, сбой
    чтения). `swipe` — готовая строка перед текстом сообщения.
    """

    tasks: list[TaskDetails] | None
    last_task: int | None
    swipe: str | None


NO_EDIT = EditContext(tasks=None, last_task=None, swipe=None)


@dataclass(frozen=True, slots=True)
class _Swiped:
    """На что ответили свайпом, как это знает база: вид, задача, текст."""

    target: edits.SwipeTarget
    task_id: str | None
    text: str | None


class EditStore(Protocol):
    """Чтение и запись правки словом (§12.2, §12.6), владелец уже подставлен.

    Тест подменяет его списком; обычная сборка — `DatabaseEditStore` поверх
    `db/`. Любой отказ — `DatabaseError`.
    """

    async def open_tasks(self, limit: int) -> list[TaskDetails]: ...

    async def last_message_event(self, since: datetime) -> TaskEvent | None: ...

    async def last_reminder_event(self, since: datetime) -> TaskEvent | None: ...

    async def reminder_task(self, telegram_message_id: int) -> str | None: ...

    async def message(self, chat_id: int, telegram_message_id: int) -> StoredMessage | None: ...

    async def task(self, task_id: str) -> TaskDetails | None: ...

    async def pick(self, message_id: str, edit: Mapping[str, Any], reply: str) -> PickedMessage: ...

    async def reopen(self, task_id: str, schedule: Sequence[Planned]) -> TaskDetails | None: ...


class DatabaseEditStore:
    """`EditStore` поверх настоящей базы. Владелец — из настроек, а не из
    сообщения или callback (инвариант 2, `techspec/04-access.md` §4.3)."""

    def __init__(self, settings: Settings, db: Client) -> None:
        self._owner = settings.owner_telegram_id
        self._db = db

    async def open_tasks(self, limit: int) -> list[TaskDetails]:
        return await db_tasks.list_open_tasks(self._db, owner_telegram_id=self._owner, limit=limit)

    async def last_message_event(self, since: datetime) -> TaskEvent | None:
        return await db_tasks.last_message_task(
            self._db, owner_telegram_id=self._owner, since=since
        )

    async def last_reminder_event(self, since: datetime) -> TaskEvent | None:
        return await db_tasks.last_reminder_task(
            self._db, owner_telegram_id=self._owner, since=since
        )

    async def reminder_task(self, telegram_message_id: int) -> str | None:
        return await db_tasks.reminder_task_id(
            self._db, owner_telegram_id=self._owner, telegram_message_id=telegram_message_id
        )

    async def message(self, chat_id: int, telegram_message_id: int) -> StoredMessage | None:
        return await db_tasks.message_by_telegram_id(
            self._db,
            owner_telegram_id=self._owner,
            chat_id=chat_id,
            telegram_message_id=telegram_message_id,
        )

    async def task(self, task_id: str) -> TaskDetails | None:
        return await db_tasks.task_details(self._db, owner_telegram_id=self._owner, task_id=task_id)

    async def pick(self, message_id: str, edit: Mapping[str, Any], reply: str) -> PickedMessage:
        return await db_tasks.pick_task(
            self._db, owner_telegram_id=self._owner, message_id=message_id, edit=edit, reply=reply
        )

    async def reopen(self, task_id: str, schedule: Sequence[Planned]) -> TaskDetails | None:
        return await db_reminders.reopen_task(
            self._db, owner_telegram_id=self._owner, task_id=task_id, schedule=schedule
        )


def task_fields(understanding: Understanding) -> dict[str, Any]:
    """Поля задачи для `record_understanding` — по именам колонок §3.3."""
    return {
        "title": understanding.title,
        "kind": understanding.kind,
        "due_at": understanding.due_at.isoformat() if understanding.due_at else None,
        "due_precision": understanding.due_precision,
        "priority": understanding.priority,
        "promise": understanding.promise,
        "people": understanding.people,
        "needs_review": understanding.needs_review,
    }


def question_of(understanding: Understanding) -> str | None:
    """Вопрос, который бот задаст (§10.1): только у задачи и только непустой.

    Идея, желание, разговор и сведение о себе вопросов не получают, даже если
    модель его отдала: правило проверяется кодом, а не только промптом.
    """
    if understanding.kind != "task" or understanding.question is None:
        return None
    return understanding.question.strip() or None


@dataclass(frozen=True, slots=True)
class Amendment:
    """Задача после ответа на вопрос (§10.2).

    `fields` уходит в `amend` как есть: только то, что ответ изменил, и
    `needs_review` всегда — пометка снимается, если модель не поставила её
    снова. Остальное — задача целиком, какой она станет: по ней
    перепланируются напоминания и собирается ответ «Понял».
    """

    fields: dict[str, Any]
    title: str
    kind: str
    due_at: datetime | None
    due_precision: str | None
    priority: str


def amendment(asked: OpenQuestion, understanding: Understanding) -> Amendment:
    """Слить ответ модели с задачей, по которой задан вопрос.

    Модель отдаёт только то, что ответ добавил (§5.2 п. 4), и пустое у неё
    значит «не менял», а не «стереть»: нет срока — срок задачи остаётся,
    `normal` — остаётся прежняя срочность, люди — дописываются к названным.
    `kind` из ответа не берётся: вид задачи ответ не меняет.
    """
    fields: dict[str, Any] = {}
    title = understanding.title.strip()
    if title and title != asked.title:
        fields["title"] = title
    due_at, due_precision = asked.due_at, asked.due_precision
    if understanding.due_at is not None and (
        understanding.due_at != asked.due_at or understanding.due_precision != asked.due_precision
    ):
        due_at, due_precision = understanding.due_at, understanding.due_precision
        fields["due_at"] = due_at.isoformat()
        fields["due_precision"] = due_precision
    if understanding.priority != "normal" and understanding.priority != asked.priority:
        fields["priority"] = understanding.priority
    if understanding.promise is not None and understanding.promise != asked.promise:
        fields["promise"] = understanding.promise
    added = [person for person in understanding.people if person not in asked.people]
    if added:
        fields["people"] = [*asked.people, *added]
    fields["needs_review"] = understanding.needs_review
    return Amendment(
        fields=fields,
        title=fields.get("title", asked.title),
        kind=asked.kind,
        due_at=due_at,
        due_precision=due_precision,
        priority=fields.get("priority", asked.priority),
    )


@dataclass(frozen=True, slots=True)
class Decision:
    """Что записать вторым шагом и что ответить человеку."""

    reply: str
    task: Mapping[str, Any] | None
    reminders: list[Planned]
    amend: Mapping[str, Any] | None = None
    edit: Mapping[str, Any] | None = None
    buttons: tuple[Button, ...] = ()


@dataclass(frozen=True, slots=True)
class Edited:
    """Правка узнанной задачи: `edit` для базы, ответ и кнопки под ним (§12.4–12.6).

    Форма `edit` — `techspec/03-schema.md` §3.4: `task_id`, `action`,
    `changes` (только отличия), `schedule` (готовый план) и `question`.
    """

    edit: dict[str, Any]
    reply: str
    buttons: tuple[Button, ...] = ()


def edit_row(
    task: TaskDetails,
    action: str,
    *,
    changes: Mapping[str, Any] | None = None,
    schedule: Sequence[Planned] = (),
    question: str | None = None,
) -> dict[str, Any]:
    """Аргумент `edit` для `record_understanding` и `pick_task` (§3.4)."""
    return {
        "task_id": task.id,
        "action": action,
        "changes": dict(changes or {}),
        "schedule": [item.as_row() for item in schedule],
        "question": question,
    }


def fact_rows(understanding: Understanding) -> list[dict[str, Any]]:
    """Записи памяти для `record_understanding` (§3.7): статус — по виду сообщения.

    Модель отдаёт только категорию и текст; факт это или предположение,
    решает бот по `kind` (`techspec/08-memory.md` §8.2), и решение
    проверяется кодом, а не моделью.
    """
    status = fact_status(understanding.kind)
    return [
        {"category": item.category, "text": item.text, "status": status}
        for item in understanding.facts
    ]


def literal_fields(text: str) -> dict[str, Any]:
    """Задача без разбора: текст как есть и пометка «человеку стоит взглянуть».

    Так поручение переживает отказ модели (`techspec/05-ai.md` §5.4): полей
    нет, но сама задача есть и не теряется.
    """
    return {
        "title": text,
        "kind": "task",
        "due_at": None,
        "due_precision": None,
        "priority": "normal",
        "promise": None,
        "people": [],
        "needs_review": True,
    }


class TaskService:
    """Операции над задачами. Собирается один раз при запуске бота."""

    def __init__(
        self,
        settings: Settings,
        record_message: MessageRecorder,
        record_understanding: UnderstandingRecorder,
        analyst: Analyst,
        transcriber: Transcriber,
        planner: Planner,
        clock: Clock | None = None,
        open_question: QuestionReader | None = None,
        edit_store: EditStore | None = None,
    ) -> None:
        self._settings = settings
        self._record_message = record_message
        self._record_understanding = record_understanding
        self._analyst = analyst
        self._transcriber = transcriber
        # Расписание считает база (`techspec/11-edit.md` §11.3); бот берёт план
        # у неё и передаёт в запись как есть.
        self._planner = planner
        # Без читателя вопросов бот ответа не узнаёт, но и не спотыкается:
        # разбор идёт как до этапа 008. Обычная сборка читатель подключает.
        self._read_question = open_question
        # Без хранилища правки модель видит пустой список («Открытых задач
        # нет.»): правка словом не находит задачу, а кнопкам нечего писать.
        # Обычная сборка хранилище подключает.
        self._edits = edit_store
        # «Сейчас» внедряется: от него зависит расписание напоминаний, и
        # тесты не должны угадывать, который час (`services/reminders.py`).
        self._clock = clock or self._now

    def _now(self) -> datetime:
        return datetime.now(self._settings.owner_timezone)

    @classmethod
    def with_database(
        cls, settings: Settings, db: Client, analyst: Analyst, transcriber: Transcriber
    ) -> TaskService:
        """Обычная сборка: пишет в настоящую базу."""

        async def record_message(
            *,
            owner_telegram_id: int,
            chat_id: int,
            telegram_message_id: int,
            text: str,
            kind: MessageKind = "text",
            telegram_file_id: str | None = None,
            duration_seconds: int | None = None,
        ) -> SavedMessage:
            return await db_tasks.record_message(
                db,
                owner_telegram_id=owner_telegram_id,
                chat_id=chat_id,
                telegram_message_id=telegram_message_id,
                text=text,
                kind=kind,
                telegram_file_id=telegram_file_id,
                duration_seconds=duration_seconds,
            )

        async def record_understanding(
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
            return await db_tasks.record_understanding(
                db,
                message_id=message_id,
                owner_telegram_id=owner_telegram_id,
                analysis=analysis,
                ai_model=ai_model,
                ai_input_tokens=ai_input_tokens,
                ai_output_tokens=ai_output_tokens,
                reply=reply,
                task=task,
                reminders=reminders,
                facts=facts,
                transcript=transcript,
                transcript_confidence=transcript_confidence,
                amend=amend,
                edit=edit,
            )

        async def read_question(*, owner_telegram_id: int, since: datetime) -> OpenQuestion | None:
            return await db_tasks.open_question(
                db, owner_telegram_id=owner_telegram_id, since=since
            )

        return cls(
            settings=settings,
            record_message=record_message,
            record_understanding=record_understanding,
            analyst=analyst,
            transcriber=transcriber,
            planner=database_planner(settings, db),
            open_question=read_question,
            edit_store=DatabaseEditStore(settings, db),
        )

    @classmethod
    def with_understanding(
        cls,
        settings: Settings,
        db: Client,
        understanding: UnderstandingService,
        transcriber: Transcriber,
    ) -> TaskService:
        """Сборка бота целиком: настоящая база, настоящая модель, настоящее распознавание."""
        return cls.with_database(settings, db, understanding, transcriber)

    async def record_from_message(
        self,
        *,
        chat_id: int,
        telegram_message_id: int,
        text: str,
        forwarded_from: str | None = None,
        swipe: Swipe | None = None,
    ) -> RecordOutcome:
        """Принять текстовое поручение и вернуть готовый ответ.

        `swipe` — сообщение, на которое ответили свайпом: подсказка модели,
        о какой задаче речь (§12.2).
        """
        try:
            saved = await self._record_message(
                owner_telegram_id=self._settings.owner_telegram_id,
                chat_id=chat_id,
                telegram_message_id=telegram_message_id,
                text=text,
            )
        except DatabaseError as error:
            # Инвариант 4: не отвечаем «Записал», пока база не подтвердила.
            logger.warning("Сообщение не записано: %s", error)
            return RecordOutcome(ok=False, message=texts.NOT_SAVED)

        if saved.reply:
            return self._repeated(saved.id, saved.reply)
        return await self._understand(
            saved,
            text,
            chat_id=chat_id,
            telegram_message_id=telegram_message_id,
            forwarded_from=forwarded_from,
            swipe=swipe,
        )

    async def record_from_voice(
        self,
        *,
        chat_id: int,
        telegram_message_id: int,
        kind: SpeechKind,
        file_id: str,
        duration: int,
        load_audio: AudioLoader,
        forwarded_from: str | None = None,
        swipe: Swipe | None = None,
    ) -> RecordOutcome:
        """Принять голосовое или кружок: сохранить, расслышать, дальше как текст (§9.3).

        Файл не качается, пока база не подтвердила, что сообщение записано:
        иначе отвечать было бы не о чем (инвариант 4). Повтор обновления виден
        там же — ответ уже есть, и ни скачивания, ни распознавания не будет.
        """
        try:
            saved = await self._record_message(
                owner_telegram_id=self._settings.owner_telegram_id,
                chat_id=chat_id,
                telegram_message_id=telegram_message_id,
                text="",
                kind=kind,
                telegram_file_id=file_id,
                duration_seconds=duration,
            )
        except DatabaseError as error:
            logger.warning("Голосовое не записано: %s", error)
            return RecordOutcome(ok=False, message=texts.NOT_SAVED)

        if saved.reply:
            return self._repeated(saved.id, saved.reply)

        heard = await self._hear(load_audio)
        if isinstance(heard, NotTranscribed):
            return await self._not_heard(saved, heard)
        logger.info("Расслышано сообщение %s: %s с, знаков %s", saved.id, duration, len(heard.text))
        return await self._understand(
            saved,
            heard.text,
            chat_id=chat_id,
            telegram_message_id=telegram_message_id,
            forwarded_from=forwarded_from,
            swipe=swipe,
            transcript=heard,
        )

    def _repeated(self, message_id: str, reply: str) -> RecordOutcome:
        """Повтор того же обновления: ответ уже давали, модель не зовём.

        Кнопок у повтора нет (§12.6): сохранён только текст ответа.
        """
        logger.info("Повтор сообщения %s: отвечаем сохранённым ответом", message_id)
        return RecordOutcome(ok=True, message=reply)

    async def _hear(self, load_audio: AudioLoader) -> TranscriptionResult:
        """Скачать файл и распознать. Отказ скачивания — тоже «не расслышал».

        Скачивание — граница с Telegram, и какие исключения оттуда придут,
        сервис не знает и знать не должен: любое из них — причина в журнал,
        человеку честный ответ, сообщение с `file_id` уже в базе.
        """
        try:
            audio = await load_audio()
        except Exception as error:  # noqa: BLE001 - граница Telegram, см. доккомментарий
            logger.warning("Файл не скачан из Telegram: %s: %s", type(error).__name__, error)
            return NotTranscribed(reason=f"download: {type(error).__name__}")
        return await self._transcriber.transcribe(audio)

    async def _not_heard(self, saved: SavedMessage, result: NotTranscribed) -> RecordOutcome:
        """Расшифровки нет: ответ в `reply`, задачи и разбора нет (§9.3).

        Открытый вопрос остаётся: запись без разбора, задачи и поправки база
        его не снимает (§3.4), и повтор, о котором бот просит, дойдёт до
        модели вместе с вопросом (§10.3).
        """
        reply = texts.NOT_HEARD
        try:
            await self._record_understanding(
                message_id=saved.id,
                owner_telegram_id=self._settings.owner_telegram_id,
                analysis=None,
                ai_model=None,
                ai_input_tokens=None,
                ai_output_tokens=None,
                reply=reply,
                task=None,
                reminders=[],
                facts=[],
            )
        except DatabaseError as error:
            # Сообщение с файлом уже в базе, и «сохранил» — правда. Без
            # записанного ответа повтор обновления распознает заново; это не
            # потеря, а лишний запрос.
            logger.warning("Ответ «не расслышал» не записан: %s", error)
        logger.info("Не расслышал сообщение %s: %s", saved.id, result.reason)
        return RecordOutcome(ok=False, message=reply)

    async def _open_question(self) -> OpenQuestion | None:
        """Открытый вопрос не старше суток — или ничего, если база не ответила.

        Поручение важнее контекста: отказ чтения не останавливает разбор, а
        уходит в журнал (как известные факты, `techspec/08-memory.md` §8.2).
        """
        if self._read_question is None:
            return None
        try:
            return await self._read_question(
                owner_telegram_id=self._settings.owner_telegram_id,
                since=self._clock() - QUESTION_TTL,
            )
        except DatabaseError as error:
            logger.error("Открытый вопрос не прочитан, разбор без него: %s", error)
            return None

    async def _understand(
        self,
        saved: SavedMessage,
        text: str,
        *,
        chat_id: int,
        telegram_message_id: int,
        forwarded_from: str | None,
        swipe: Swipe | None = None,
        transcript: Transcript | None = None,
    ) -> RecordOutcome:
        """Разбор моделью и запись разбора — общий хвост текста и голоса.

        `transcript` есть у голоса: модели говорится, что текст распознан и
        с каким качеством (§9.4), а расшифровка уходит в базу тем же вызовом,
        что разбор и задача (§9.3). Открытый вопрос читается до модели и
        уходит ей в промпт (§10.2) — и для текста, и для голоса. Рядом с ним —
        подсказки для правки словом (§12.2).
        """
        spoken: SpeechQuality | None = None
        if transcript is not None:
            spoken = "low" if transcript.low_confidence else "fine"

        asked, context = await asyncio.gather(
            self._open_question(), self._edit_context(chat_id, forwarded_from, swipe)
        )
        verdict = await self._analyst.analyze(
            text,
            forwarded_from=forwarded_from,
            spoken=spoken,
            open_question=asked,
            tasks=context.tasks,
            last_task=context.last_task,
            swipe=context.swipe,
        )
        now = self._clock()
        if isinstance(verdict, Analysis):
            understanding = verdict.understanding
            try:
                decision = await self._decide(
                    understanding, asked, now, context, telegram_message_id
                )
            except DatabaseError as error:
                # Без плана «Напомню» было бы неправдой, а задача без
                # напоминаний — тихой потерей: честнее не записать (§11.3).
                logger.warning("Расписание не получено, разбор не записан: %s", error)
                return RecordOutcome(ok=False, message=texts.NOT_SAVED)
            analysis: Mapping[str, Any] | None = understanding.model_dump(mode="json")
            facts = fact_rows(understanding)
            ai_model: str | None = verdict.model
            input_tokens: int | None = verdict.input_tokens
            output_tokens: int | None = verdict.output_tokens
        else:
            # Разбора не случилось: записываем буквально и говорим об этом.
            # Срока у такой задачи нет, значит и напоминать не о чем.
            decision = Decision(
                reply=texts.RECORDED_AS_IS.format(text=summarize(text)),
                task=literal_fields(text),
                reminders=[],
            )
            analysis = None
            facts = []
            ai_model = None
            input_tokens = None
            output_tokens = None

        try:
            recorded = await self._record_understanding(
                message_id=saved.id,
                owner_telegram_id=self._settings.owner_telegram_id,
                analysis=analysis,
                ai_model=ai_model,
                ai_input_tokens=input_tokens,
                ai_output_tokens=output_tokens,
                reply=decision.reply,
                task=decision.task,
                reminders=[item.as_row() for item in decision.reminders],
                facts=facts,
                transcript=transcript.text if transcript is not None else None,
                transcript_confidence=transcript.confidence if transcript is not None else None,
                amend=decision.amend,
                edit=decision.edit,
            )
        except DatabaseError as error:
            # Правку база отклоняет и тогда, когда задачу закрыли или удалили,
            # пока модель думала (§12.3): откат целиком, честное «не смог».
            logger.warning("Разбор не записан: %s", error)
            return RecordOutcome(ok=False, message=texts.NOT_SAVED)

        if facts:
            logger.info("Записано сведений о владельце: %s", len(facts))

        if decision.edit is not None:
            logger.info(
                "Правка словом: %s задачи %s", decision.edit["action"], decision.edit["task_id"]
            )
        elif recorded is None:
            logger.info("Задачи нет: сообщение %s сохранено с разбором", saved.id)
        elif decision.amend is not None:
            logger.info("Ответ на вопрос дополнил задачу %s", recorded.id)
        else:
            logger.info("Записана задача %s", recorded.id)
        return RecordOutcome(ok=True, message=decision.reply, buttons=decision.buttons)

    async def _decide(
        self,
        understanding: Understanding,
        asked: OpenQuestion | None,
        now: datetime,
        context: EditContext,
        telegram_message_id: int,
    ) -> Decision:
        """Четыре пути разбора: ответ на вопрос, правка словом, запись с
        вопросом, обычная запись.

        Ответ дополняет задачу, по которой спрашивали (§10.2): напоминания
        планируются заново по сроку, какой у неё станет, и уходят в `amend`, а
        не новой задачей. Вопрос — только у задачи (§10.1): она записывается
        сразу, с пометкой и текстом вопроса. «Ответ» без открытого вопроса
        отвечать не на что — это обычная запись.

        Правка — только при блоке 5 в промпте (§12.2): без него `edit` модель
        отдать не могла, а если отдала, бот её не слушает. Ответ на открытый
        вопрос главнее правки (§12.1).

        План берётся у базы, только когда есть что планировать — задача или
        поправка; у разговора и сведения о себе задачи нет, и звать базу
        незачем. Отказ базы выходит наружу `DatabaseError`.
        """
        if asked is not None and understanding.answers_question:
            changed = amendment(asked, understanding)
            planned = await self._planner(
                due_at=changed.due_at,
                due_precision=changed.due_precision,
                kind=changed.kind,
                now=now,
            )
            reply = texts.understood_reply(
                title=changed.title,
                due=self._due_words(changed.due_at, changed.due_precision),
                review_reason=understanding.review_reason if understanding.needs_review else None,
                # Срочность звучит, только если её изменил сам ответ.
                priority=changed.priority if "priority" in changed.fields else "normal",
                remind_at=self._remind_words(planned, now),
            )
            amend = {
                "task_id": asked.task_id,
                "fields": changed.fields,
                "reminders": [item.as_row() for item in planned],
            }
            return Decision(reply=reply, task=None, reminders=[], amend=amend)

        if understanding.edit is not None and context.tasks is not None:
            return await self._decide_edit(
                understanding, understanding.edit, context.tasks, now, telegram_message_id
            )

        task_row = task_fields(understanding) if understanding.kind in TASK_KINDS else None
        planned = []
        if task_row is not None:
            planned = await self._planner(
                due_at=understanding.due_at,
                due_precision=understanding.due_precision,
                kind=understanding.kind,
                now=now,
            )
        question = question_of(understanding)
        if question is not None:
            reply = texts.asked_reply(
                title=understanding.title,
                question=question,
                due=self._due_words(understanding.due_at, understanding.due_precision),
                remind_at=self._remind_words(planned, now),
            )
            task = {**task_fields(understanding), "needs_review": True, "open_question": question}
            return Decision(reply=reply, task=task, reminders=planned)

        return Decision(
            reply=self._reply_for(understanding, planned, now), task=task_row, reminders=planned
        )

    async def _edit_context(
        self, chat_id: int, forwarded_from: str | None, swipe: Swipe | None
    ) -> EditContext:
        """Подсказки для правки (§12.2) — или их отсутствие.

        Пересланное правкой не бывает: блока 5 нет, свайп не читается. Без
        хранилища — пустой список. Не прочитался список — блока нет, и разбор
        идёт как до правки словом (поручение важнее контекста); не
        прочиталась последняя задача или свайп — нет только этой строки.
        """
        if forwarded_from is not None:
            return NO_EDIT
        store = self._edits
        if store is None:
            return EditContext(tasks=[], last_task=None, swipe=None)
        now = self._clock()
        tasks, events, swiped = await asyncio.gather(
            self._open_tasks(store),
            self._last_events(store, now - edits.LAST_TASK_WINDOW),
            self._swiped(store, chat_id, swipe),
        )
        if tasks is None:
            return NO_EDIT
        last_task = edits.last_task_number(events, tasks, now)
        line = None
        if swiped is not None:
            number = edits.number_of(tasks, swiped.task_id)
            line = edits.swipe_line(swiped.target, number, swiped.text)
        logger.info(
            "Контекст правки: задач %s, последняя №%s, свайп %s",
            len(tasks),
            last_task,
            line is not None,
        )
        return EditContext(tasks=tasks, last_task=last_task, swipe=line)

    async def _open_tasks(self, store: EditStore) -> list[TaskDetails] | None:
        """Открытые задачи по номерам; база не ответила — `None`, блока нет."""
        try:
            return edits.number_tasks(await store.open_tasks(edits.TASK_LIMIT))
        except DatabaseError as error:
            logger.error("Открытые задачи не прочитаны, разбор без правки: %s", error)
            return None

    async def _last_events(self, store: EditStore, since: datetime) -> list[TaskEvent | None]:
        """Два события разговора за час (§12.2). Любой отказ — ни одного.

        Одно событие без другого могло бы назвать не ту задачу: более позднее
        как раз и не прочиталось.
        """
        try:
            by_message, by_reminder = await asyncio.gather(
                store.last_message_event(since), store.last_reminder_event(since)
            )
        except DatabaseError as error:
            logger.warning("Последняя задача в разговоре не прочитана: %s", error)
            return []
        return [by_message, by_reminder]

    async def _swiped(self, store: EditStore, chat_id: int, swipe: Swipe | None) -> _Swiped | None:
        """На что ответили свайпом (§12.2): задача напоминания или своего
        сообщения и текст. База не ответила — строки свайпа нет."""
        if swipe is None:
            return None
        try:
            if swipe.from_bot:
                task_id = await store.reminder_task(swipe.telegram_message_id)
                target: edits.SwipeTarget = "bot" if task_id is None else "reminder"
                return _Swiped(target=target, task_id=task_id, text=swipe.text)
            stored = await store.message(chat_id, swipe.telegram_message_id)
        except DatabaseError as error:
            logger.warning("Свайп не прочитан: %s", error)
            return None
        text = swipe.text
        if not text and stored is not None:
            # Голосовое: в Telegram текста нет, расшифровка — в базе.
            text = stored.text
        return _Swiped(
            target="own", task_id=stored.task_id if stored is not None else None, text=text
        )

    async def _decide_edit(
        self,
        understanding: Understanding,
        edit: TaskEdit,
        tasks: Sequence[TaskDetails],
        now: datetime,
        telegram_message_id: int,
    ) -> Decision:
        """Правка словом (§12.3): задача узнана, кандидаты или не найдено.

        Номер модели переводится в задачу по тому же списку, что ушёл в
        промпт. Кандидаты — вопрос с кнопками, до выбора ничего не меняется.
        Не найдено: перенос записывается новой задачей (инвариант 5),
        остальное — не записывается ничего.
        """
        task = edits.task_by_number(tasks, edit.task)
        if task is not None:
            edited = await self._edit_known(understanding, edit, task, now)
            return Decision(
                reply=edited.reply,
                task=None,
                reminders=[],
                edit=edited.edit,
                buttons=edited.buttons,
            )
        candidates = edits.candidates_of(tasks, edit.candidates)
        if candidates:
            timezone = self._settings.owner_timezone
            buttons = tuple(
                Button(
                    text=edits.candidate_label(item, timezone),
                    data=edits.pick_data(telegram_message_id, item.id),
                )
                for item in candidates
            )
            return Decision(
                reply=edits.pick_question(edit, now, timezone),
                task=None,
                reminders=[],
                buttons=buttons,
            )
        if edit.action == "change" and edit.due_at is not None and understanding.kind in TASK_KINDS:
            return await self._unfound_move(understanding, edit.due_at, edit.due_precision, now)
        return Decision(
            reply=texts.NOT_FOUND.format(title=understanding.title), task=None, reminders=[]
        )

    async def _unfound_move(
        self,
        understanding: Understanding,
        edit_due_at: datetime,
        edit_precision: str | None,
        now: datetime,
    ) -> Decision:
        """Перенос задачи, которой нет в списке, — новой задачей (§12.3).

        Поля — верхнего уровня; нет там срока — срок из правки. Вопрос
        верхнего уровня задачу не получает: он был о правке, а не о новом
        поручении (решение 3 плана этапа 010).
        """
        due_at = understanding.due_at
        precision: str | None = understanding.due_precision
        if due_at is None:
            due_at, precision = edit_due_at, edit_precision or "time"
        if due_at.tzinfo is None:
            due_at = due_at.replace(tzinfo=self._settings.owner_timezone)
        planned = await self._planner(
            due_at=due_at, due_precision=precision, kind=understanding.kind, now=now
        )
        task = {
            **task_fields(understanding),
            "due_at": due_at.isoformat(),
            "due_precision": precision,
        }
        reply = texts.not_found_reply(
            title=understanding.title,
            due=self._due_words(due_at, precision),
            review_reason=understanding.review_reason if understanding.needs_review else None,
            priority=understanding.priority,
            remind_at=self._remind_words(planned, now),
        )
        return Decision(reply=reply, task=task, reminders=planned)

    async def _edit_known(
        self, understanding: Understanding, edit: TaskEdit, task: TaskDetails, now: datetime
    ) -> Edited:
        """Правка узнанной задачи (§12.3, §12.5): что записать и что ответить.

        Её же строит нажатие кнопки кандидата — с разбором из базы и планом на
        момент нажатия (§12.6). Непонятное значение — вопрос верхнего уровня:
        ничего не меняется, даже понятное, задача получает пометку и вопрос.
        План берётся у базы, только если срок сменился и не снят: иначе
        напоминания задачи остаются как есть или снимаются целиком.
        """
        if edit.action == "done" or edit.action == "cancel":
            head = texts.CLOSED if edit.action == "done" else texts.CANCELLED
            back = Button(text=texts.REOPEN_BUTTON, data=edits.reopen_data(task.id))
            return Edited(
                edit=edit_row(task, edit.action),
                reply=head.format(title=task.title),
                buttons=(back,),
            )
        question = (understanding.question or "").strip()
        if question:
            return Edited(
                edit=edit_row(task, "change", question=question),
                reply=texts.UNCLEAR_EDIT.format(title=task.title, question=question),
            )
        change = edits.edit_changes(task, edit, self._settings.owner_timezone)
        if not change.changes:
            # Задача всё равно пишется в `edit`: база проверит, что она
            # активна, и сообщение станет «о ней» (решение 6 плана).
            return Edited(
                edit=edit_row(task, "change"),
                reply=texts.NOTHING_TO_CHANGE.format(title=task.title),
            )
        priority = change.priority if "priority" in change.changes else None
        people = change.people if "people" in change.changes else None
        planned: list[Planned] = []
        if change.due_changed and change.due_at is None:
            reply = texts.DUE_REMOVED.format(title=change.title)
        elif change.due_changed:
            planned = await self._planner(
                due_at=change.due_at, due_precision=change.due_precision, kind=task.kind, now=now
            )
            reply = texts.edited_reply(
                texts.MOVED_BY_WORD.format(title=change.title),
                self._due_words(change.due_at, change.due_precision),
                self._remind_words(planned, now),
                priority,
                people,
            )
        else:
            reply = texts.edited_reply(
                texts.FIXED.format(title=change.title),
                self._due_words(change.due_at, change.due_precision),
                priority=priority,
                people=people,
            )
        return Edited(
            edit=edit_row(task, "change", changes=change.changes, schedule=planned), reply=reply
        )

    async def pick(self, *, chat_id: int, telegram_message_id: int, task_id: str) -> PressOutcome:
        """Кнопка кандидата (§12.6): та же правка для выбранной задачи.

        Разбор берётся из базы — из сообщения владельца, под которым задан
        вопрос; расписание — у `reminder_plan` на момент нажатия. Пишет
        `pick_task` одной транзакцией, и по её ответу видно, чья правка
        легла: своя — ответ и кнопка «Вернуть», прежняя (второе нажатие,
        другая кнопка) — сохранённый текст без кнопок. Отказ базы — всплывающий
        ответ, вопрос с кнопками остаётся (решение 10 плана).
        """
        store = self._edits
        if store is None:
            return PressOutcome(message=texts.NOT_PICKED, replace=False)
        try:
            stored = await store.message(chat_id, telegram_message_id)
            if stored is None:
                return PressOutcome(message=texts.DONE_UNKNOWN, replace=False)
            if stored.task_id is not None:
                return self._picked_before(stored.reply)
            understanding = self._stored_understanding(stored)
            if understanding is None or understanding.edit is None:
                return PressOutcome(message=texts.DONE_UNKNOWN, replace=False)
            task = await store.task(task_id)
            if task is None or task.status != db_tasks.ACTIVE_STATUS:
                return PressOutcome(message=texts.PICKED_GONE, replace=False)
            edited = await self._edit_known(understanding, understanding.edit, task, self._clock())
            picked = await store.pick(stored.id, edited.edit, edited.reply)
        except DatabaseError as error:
            logger.warning("Выбор задачи не записан: %s", error)
            return PressOutcome(message=texts.NOT_PICKED, replace=False)
        if picked.task_id is None:
            return PressOutcome(message=texts.PICKED_GONE, replace=False)
        if picked.task_id == task.id and picked.reply == edited.reply:
            logger.info("Выбрана задача %s: %s", task.id, edited.edit["action"])
            return PressOutcome(message=edited.reply, replace=True, buttons=edited.buttons)
        return self._picked_before(picked.reply)

    @staticmethod
    def _picked_before(reply: str | None) -> PressOutcome:
        """Правка по сообщению уже сделана: сохранённый ответ, без кнопок."""
        logger.info("Правка по сообщению уже записана: второй раз не пишем")
        if not reply:
            return PressOutcome(message=texts.DONE_UNKNOWN, replace=False)
        return PressOutcome(message=reply, replace=True)

    @staticmethod
    def _stored_understanding(stored: StoredMessage) -> Understanding | None:
        """Разбор из `messages.analysis`; не читается — `None`, правка не угадывается."""
        if stored.analysis is None:
            return None
        try:
            return Understanding.model_validate(stored.analysis)
        except ValidationError as error:
            logger.warning("Разбор сообщения %s не читается: %s", stored.id, error)
            return None

    async def reopen(self, *, task_id: str) -> PressOutcome:
        """Кнопка «Вернуть» (§12.6): задача снова активна, напоминания заново.

        План — у `reminder_plan` на момент нажатия; строка «Напомню» — из
        него же, и срок в прошлом её не получает (решение 4 плана). Уже
        активная задача — тот же ответ без записи; удалённая — «Не нашёл».
        """
        store = self._edits
        if store is None:
            return PressOutcome(message=texts.NOT_REOPENED, replace=False)
        now = self._clock()
        try:
            task = await store.task(task_id)
            if task is None:
                return PressOutcome(message=texts.DONE_UNKNOWN, replace=False)
            planned = await self._planner(
                due_at=task.due_at, due_precision=task.due_precision, kind=task.kind, now=now
            )
            reopened = await store.reopen(task_id, planned)
        except DatabaseError as error:
            logger.warning("Задача не возвращена в работу: %s", error)
            return PressOutcome(message=texts.NOT_REOPENED, replace=False)
        if reopened is None:
            return PressOutcome(message=texts.DONE_UNKNOWN, replace=False)
        logger.info("Задача %s возвращена в работу", reopened.id)
        reply = texts.edited_reply(
            texts.REOPENED.format(title=reopened.title),
            self._due_words(reopened.due_at, reopened.due_precision),
            self._remind_words(planned, now),
        )
        return PressOutcome(message=reply, replace=True)

    def _due_words(self, due_at: datetime | None, precision: str | None) -> str | None:
        """Срок словами в поясе владельца; нет срока — нет и строки."""
        if due_at is None:
            return None
        return texts.format_due(due_at.astimezone(self._settings.owner_timezone), precision)

    def _remind_words(self, planned: list[Planned], now: datetime) -> str | None:
        """Ближайшее напоминание словами — из того же плана, что уходит в базу (§6.4)."""
        nearest = next_fire_at(planned)
        if nearest is None:
            return None
        timezone = self._settings.owner_timezone
        return texts.format_remind_at(nearest.astimezone(timezone), now.astimezone(timezone))

    def _reply_for(
        self, understanding: Understanding, planned: list[Planned], now: datetime
    ) -> str:
        """Ответ человеку по видам. Дословно из модели — причина и текст записи.

        Строка «Напомню» берётся из того же плана, который уходит в базу
        (§6.4): бот обещает ровно то, что записал, — и ничего сверх того
        (инвариант 4). Сведение о себе подтверждается словами «Запомнил: …»
        (`techspec/08-memory.md` §8.2); предположения из поручения в ответ
        не попадают — они видны в приложении.
        """
        if understanding.kind == "about_me":
            if understanding.facts:
                return texts.remembered([item.text for item in understanding.facts])
            # Сведение есть, а нового нет — значит, оно уже в памяти (§8.2).
            return texts.ALREADY_KNOWN
        if understanding.kind not in TASK_KINDS:
            return texts.NO_ERRAND
        return texts.recorded_reply(
            kind=understanding.kind,
            title=understanding.title,
            due=self._due_words(understanding.due_at, understanding.due_precision),
            review_reason=understanding.review_reason if understanding.needs_review else None,
            priority=understanding.priority,
            remind_at=self._remind_words(planned, now),
        )
