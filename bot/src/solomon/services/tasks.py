"""Приём поручения: сообщение в базу, разбор моделью, задача и ответ словами.

Порядок шагов — `techspec/03-schema.md` §3.4: сначала `record_message`
(поручение в базе с первой секунды, инвариант 5), потом модель, потом
`record_understanding` — разбор, ответ бота и задача одной транзакцией.
Повтор того же обновления отсекается на первом шаге: у сообщения уже есть
ответ, и модель не зовётся.

Голосовое идёт тем же путём с одним шагом посередине (`techspec/09-voice.md`
§9.3): сообщение с файлом пишется до того, как его расслышали, потом
скачивание и распознавание, дальше — как с текстом. Не расслышали — честный
ответ вместо задачи, и «Записал» не говорится (инвариант 4). Пока файл
качается, читаются имена, которые бот уже знает (§9.5): они уходят
распознаванию подсказками, а не прочитались — голосовое слышится без них.

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

Повторяющаяся задача (`techspec/13-repeat.md`) идёт теми же путями: правило
пишется вместе со сроком первого раза, «сделал» и пропуск переводят её на
следующий раз — его бот берёт у базы (`repeat_next`) заранее, вместе с
планом, как расписание правки (§12.4), и ответ называет ровно записанное.
Кнопка «Вернуть» под «Отметил» и «Пропускаю» возвращает прежний раз.

Разговор (`techspec/17-conversation.md`) — тоже здесь: рядом со списком
задач у своего текста и голоса читается недавний разговор (блок 6) — у
пересланного и снимка он не читается, — а разбор `chat` своего сообщения
отвечает текстом модели из `reply_hint`. Ответ проходит проверку: о
сделанном разговор не говорит (инвариант 4). Чистые правила блока и
ответа — в `services/conversation.py`.

Переписка, пересланная разом (`techspec/18-forwarded.md`), — тоже здесь:
текст и голос после записи в базу встают в пачку чата (`services/batches.py`)
и ждут секунду тишины. Пачка без пересланного или из одного сообщения
разбирается по одному, как раньше; переписку разбирает её голова —
последнее пересланное: голосовые распознаются параллельно, модель зовётся
один раз, разбор, ответ и задача ложатся на голову, расшифровки остальных —
в их строки. Остальные сообщения переписки получают пустой ответ, и
обработчик его не отправляет.

Поиск по поручению (`techspec/24-search.md`) начинается здесь: разбор с
поиском заводит строку поиска раньше, чем ответ скажет «Ищу» (инварианты 4
и 5), — не записалась, и «Ищу» не звучит. Ищет `services/search.py`, а
обработчик запускает поиск, когда «Ищу» уже ушло. Пересланное, снимок и
переписка поиска не запускают: ответ — просьба сказать текстом или голосом.

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
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Protocol

from pydantic import ValidationError
from supabase import Client

from solomon import texts
from solomon.config import Settings
from solomon.db import facts as db_facts
from solomon.db import reminders as db_reminders
from solomon.db import searches as db_searches
from solomon.db import tasks as db_tasks
from solomon.db.reminders import Planned
from solomon.db.rpc import DatabaseError
from solomon.db.tasks import (
    MessageKind,
    OpenQuestion,
    PickedMessage,
    RecentMessage,
    SavedMessage,
    SpeechKind,
    StoredMessage,
    Task,
    TaskDetails,
    TaskEvent,
)
from solomon.services import batches, conversation, edits, overdue
from solomon.services.batches import Batches, Line
from solomon.services.names import known_names
from solomon.services.reminders import Planner, database_planner, next_fire_at
from solomon.services.repeat import (
    NO_RULE,
    RuleOutcome,
    malformed_reason,
    moment_of,
    occurrence_seconds,
    record_rule,
    same_rule,
    series_precision,
)
from solomon.services.transcription import (
    NotTranscribed,
    Transcriber,
    Transcript,
    TranscriptionResult,
)
from solomon.services.understanding import (
    SEARCH_KIND,
    TASK_KINDS,
    Analysis,
    AskedQuestion,
    Clock,
    ConversationAnalysis,
    ConversationUnderstanding,
    ConversationVerdict,
    ImageType,
    MessageUnderstanding,
    OpenTask,
    PhotoAnalysis,
    PhotoUnderstanding,
    PhotoVerdict,
    SpeechQuality,
    TaskEdit,
    TaskItem,
    Understanding,
    UnderstandingService,
    Verdict,
    also_of,
    fact_status,
    searches_of,
)

logger = logging.getLogger(__name__)

# Пересказ в ответе. В базу текст уходит целиком (инвариант 5), а ответ
# бота остаётся коротким и в лимит Telegram укладывается всегда.
SUMMARY_LIMIT = 200

# Сколько живёт вопрос без ответа (`techspec/10-dialog.md` §10.3): дольше —
# в промпт не попадает, и следующая запись его снимет (кроме «не расслышал»).
QUESTION_TTL = timedelta(hours=24)

# Сколько задач с людьми и записей памяти читается ради имён (§9.5). В
# подсказки всё равно входит лишь то, что вмещает предел провайдера
# (`transcription.KEYTERM_LIMIT`), — чтение берёт с запасом над ним.
NAME_TASKS_LIMIT = 200
NAME_FACTS_LIMIT = 200
# Дел на одно сообщение (`techspec/23-several-tasks.md` §23.3): верх и девять
# из `also`; остальные называются сутью, а не записываются. Тот же предел
# проверяет callback «Записать отдельно» с номером дела (§23.5).
MAX_ITEMS = edits.ITEM_LIMIT

# Виды записи под значком 💡 (`techspec/29-icons.md` §29.1); дело — ✅.
IDEA_KINDS = ("idea", "wish")


def summarize(text: str) -> str:
    """Пересказ поручения для ответа: длинное обрезается, целое уже в базе."""
    if len(text) <= SUMMARY_LIMIT:
        return text
    return text[:SUMMARY_LIMIT] + "…"


def as_is_reply(text: str) -> str:
    """«Записал как есть» при отказе модели (`techspec/05-ai.md` §5.4): задача
    есть, а разбора нет — значок «не получилось» (`techspec/29-icons.md`)."""
    return texts.iconed(texts.ICON_TROUBLE, texts.RECORDED_AS_IS.format(text=summarize(text)))


@dataclass(frozen=True, slots=True)
class Button:
    """Inline-кнопка под ответом: надпись и callback (`techspec/12-chat-edit.md` §12.6).

    Кнопка-ссылка — `url` вместо callback: «Открыть чат» под сообщениями о
    переписке (`techspec/25-chats.md` §25.4); `data` у неё пуст.
    """

    text: str
    data: str = ""
    url: str | None = None


@dataclass(frozen=True, slots=True)
class RecordOutcome:
    """Чем кончился приём поручения и что сказать человеку.

    `buttons` — кнопки под ответом, по одной в ряд: кандидаты «какую задачу»
    или «Вернуть» (§12.6). У повтора обновления их нет.

    `search_id` — поиск, заведённый этим сообщением (`techspec/24-search.md`
    §24.3): обработчик запускает его, когда ответ «Ищу» уже ушёл. У повтора
    обновления его нет — брошенный поиск подхватит тик.
    """

    ok: bool
    message: str
    buttons: tuple[Button, ...] = ()
    search_id: str | None = None


@dataclass(frozen=True, slots=True)
class PressOutcome:
    """Чем кончилось нажатие кнопки правки (§12.6).

    `replace` — сообщение с кнопками заменяется текстом `message` и кнопками
    `buttons`; иначе `message` — всплывающий ответ, а сообщение и его кнопки
    остаются, чтобы нажать ещё раз (как «Сделано», когда база не ответила).

    `follow_up` — нажатие под ответом о нескольких делах
    (`techspec/23-several-tasks.md` §23.5): текст ответа не меняется, с него
    снимаются кнопки нажатого вопроса, а `message` с кнопками `buttons`
    приходит новым сообщением.
    """

    message: str
    replace: bool
    buttons: tuple[Button, ...] = ()
    follow_up: bool = False


class MessageRecorder(Protocol):
    """Первый шаг: сообщение в базу до всякого разбора.

    У голоса и кружка — вид, файл и длительность, а текст пуст до расшифровки
    (§9.3). `forwarded_from` — от кого переслано (§17.5), у своего пусто.
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
        forwarded_from: str | None = None,
    ) -> SavedMessage: ...


class UnderstandingRecorder(Protocol):
    """Второй шаг: разбор, ответ бота и задачи одной транзакцией.

    `tasks` — новые дела сообщения `{item, task, reminders}`, номер дела от
    1 до 10 (`techspec/23-several-tasks.md` §23.6); возвращаются задачи
    сообщения: поправленная ответом или правкой, найденная дублем и новые.
    `transcript` — расшифровка голоса: тем же вызовом становится текстом
    сообщения (§9.3). `edit` — правка задачи словом (§12.4). `photo_text` —
    прочитанное со снимка (§14.2). `same_task` — задача, которую сообщение
    повторяет (`techspec/15-duplicates.md` §15.3): новой не заводится.
    `reply` пуст у расшифровки голосового из переписки (§18.3): ответа у
    такого сообщения нет.
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
        reply: str | None,
        tasks: Sequence[Mapping[str, Any]],
        facts: Sequence[Mapping[str, Any]],
        transcript: str | None = None,
        transcript_confidence: float | None = None,
        amend: Mapping[str, Any] | None = None,
        edit: Mapping[str, Any] | None = None,
        photo_text: str | None = None,
        same_task: str | None = None,
    ) -> list[Task]: ...


class NextOccurrence(Protocol):
    """Следующий раз повторяющейся задачи — у базы (`repeat_next`, §13.2).

    Пояс владельца знает сборка. Строго позже `after`; отказ — `DatabaseError`.
    """

    async def __call__(
        self, *, repeat: Mapping[str, Any], occurrence_at: datetime, after: datetime
    ) -> datetime: ...


async def no_next_occurrence(
    *, repeat: Mapping[str, Any], occurrence_at: datetime, after: datetime
) -> datetime:
    """Сборка без базы: следующего раза не посчитать — как отказ базы."""
    raise DatabaseError("repeat_next is not wired")


class SearchStarter(Protocol):
    """Завести поиск по сообщению (`techspec/24-search.md` §24.3, шаг 1).

    Возвращает id поиска; по этому сообщению он уже есть — тот же id. Отказ —
    `DatabaseError`: «Ищу» тогда не звучит (§24.6).
    """

    async def __call__(self, *, message_id: str, query: str) -> str: ...


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
        last_tasks: Sequence[int] = (),
        swipe: str | None = None,
        recent: str | None = None,
    ) -> Verdict: ...

    async def analyze_photo(
        self,
        image: bytes,
        *,
        media_type: ImageType,
        caption: str,
        forwarded_from: str | None = None,
        open_question: AskedQuestion | None = None,
        tasks: Sequence[OpenTask] | None = None,
    ) -> PhotoVerdict: ...

    async def analyze_conversation(
        self,
        text: str,
        *,
        open_question: AskedQuestion | None = None,
        tasks: Sequence[OpenTask] | None = None,
        recent: str | None = None,
    ) -> ConversationVerdict: ...


# Скачивание звука из Telegram. Приходит из обработчика замыканием над
# `handlers.load_file` (сбой сети там повторяется, §9.3), чтобы сервис не знал
# про aiogram — как отправка напоминаний приходит в `services/reminders.py`.
# Байты живут в памяти и на диск не пишутся (§9.2).
AudioLoader = Callable[[], Awaitable[bytes]]

# Скачивание снимка — так же, замыканием из обработчика; байты уходят модели
# и после запроса не хранятся (`techspec/14-photo.md` §14.2).
ImageLoader = Callable[[], Awaitable[bytes]]


@dataclass(frozen=True, slots=True)
class Pending:
    """Сообщение в пачке (`techspec/18-forwarded.md` §18.1): строка в базе,
    строка переписки и, у голоса, чем его скачать и сколько он длится."""

    saved: SavedMessage
    telegram_message_id: int
    line: Line
    load_audio: AudioLoader | None = None
    duration: int | None = None


# Ответ сообщению переписки, которое не голова (§18.1): обработчик его не
# отправляет — переписке отвечает одна голова.
SILENT = RecordOutcome(ok=True, message="")


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
    """Подсказки модели для правки (§12.2): список, последние задачи, свайп,
    недавний разговор.

    `tasks` — открытые задачи в порядке номеров (`edits.number_tasks`);
    `None` — блока 5 в промпте нет, и не бывает ни правки, ни дубля (сбой
    чтения). `swipe` — готовая строка перед текстом сообщения. `edits` —
    правка разрешена: у пересланного и снимка список есть только для сверки
    дублей (`techspec/15-duplicates.md` §15.2), и их `edit` бот не слушает.
    `recent` — готовый блок 6 (`techspec/17-conversation.md` §17.3); `None` —
    блока нет.
    """

    tasks: list[TaskDetails] | None
    last_tasks: tuple[int, ...]
    swipe: str | None
    edits: bool = True
    recent: str | None = None


NO_EDIT = EditContext(tasks=None, last_tasks=(), swipe=None)


@dataclass(frozen=True, slots=True)
class _Swiped:
    """На что ответили свайпом, как это знает база: вид, задачи, текст.

    У напоминания задача одна, у своего сообщения — все его задачи (§23.6).
    """

    target: edits.SwipeTarget
    task_ids: tuple[str, ...]
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

    async def same_minute(self, due_at: datetime, exclude_task_id: str | None) -> list[str]: ...

    async def recent_messages(
        self, since: datetime, before: datetime, limit: int
    ) -> list[RecentMessage]: ...

    async def pick(self, message_id: str, edit: Mapping[str, Any], reply: str) -> PickedMessage: ...

    async def record_separately(
        self,
        message_id: str,
        task: Mapping[str, Any],
        reminders: Sequence[Planned],
        reply: str,
        item: int = 1,
    ) -> PickedMessage: ...

    async def append_reply(self, message_id: str, paragraph: str) -> PickedMessage: ...

    async def reopen(self, task_id: str, schedule: Sequence[Planned]) -> TaskDetails | None: ...

    async def return_occurrence(
        self, task_id: str, back_to: int, moved_from: int, schedule: Sequence[Planned]
    ) -> TaskDetails | None: ...


class NameSource(Protocol):
    """Откуда бот знает имена для подсказок (§9.5), владелец уже подставлен.

    Тест подменяет его списками; обычная сборка — `DatabaseNames` поверх
    `db/`. Любой отказ — `DatabaseError`.
    """

    async def memory_texts(self, limit: int) -> list[str]: ...

    async def task_people(self, limit: int) -> list[tuple[str, ...]]: ...


class DatabaseNames:
    """`NameSource` поверх настоящей базы: тексты памяти и люди задач владельца
    из настроек (инвариант 2, `techspec/04-access.md` §4.3)."""

    def __init__(self, settings: Settings, db: Client) -> None:
        self._owner = settings.owner_telegram_id
        self._db = db

    async def memory_texts(self, limit: int) -> list[str]:
        return await db_facts.list_fact_texts(self._db, owner_telegram_id=self._owner, limit=limit)

    async def task_people(self, limit: int) -> list[tuple[str, ...]]:
        return await db_tasks.list_task_people(self._db, owner_telegram_id=self._owner, limit=limit)


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

    async def same_minute(self, due_at: datetime, exclude_task_id: str | None) -> list[str]:
        return await db_tasks.same_minute_titles(
            self._db,
            owner_telegram_id=self._owner,
            due_at=due_at,
            exclude_task_id=exclude_task_id,
        )

    async def recent_messages(
        self, since: datetime, before: datetime, limit: int
    ) -> list[RecentMessage]:
        return await db_tasks.recent_messages(
            self._db, owner_telegram_id=self._owner, since=since, before=before, limit=limit
        )

    async def pick(self, message_id: str, edit: Mapping[str, Any], reply: str) -> PickedMessage:
        return await db_tasks.pick_task(
            self._db, owner_telegram_id=self._owner, message_id=message_id, edit=edit, reply=reply
        )

    async def record_separately(
        self,
        message_id: str,
        task: Mapping[str, Any],
        reminders: Sequence[Planned],
        reply: str,
        item: int = 1,
    ) -> PickedMessage:
        return await db_tasks.record_separately(
            self._db,
            owner_telegram_id=self._owner,
            message_id=message_id,
            task=task,
            reminders=[planned.as_row() for planned in reminders],
            reply=reply,
            item=item,
        )

    async def append_reply(self, message_id: str, paragraph: str) -> PickedMessage:
        return await db_tasks.append_reply(
            self._db, owner_telegram_id=self._owner, message_id=message_id, paragraph=paragraph
        )

    async def reopen(self, task_id: str, schedule: Sequence[Planned]) -> TaskDetails | None:
        return await db_reminders.reopen_task(
            self._db, owner_telegram_id=self._owner, task_id=task_id, schedule=schedule
        )

    async def return_occurrence(
        self, task_id: str, back_to: int, moved_from: int, schedule: Sequence[Planned]
    ) -> TaskDetails | None:
        return await db_reminders.return_occurrence(
            self._db,
            owner_telegram_id=self._owner,
            task_id=task_id,
            back_to=back_to,
            moved_from=moved_from,
            schedule=schedule,
        )


def rule_of(understanding: Understanding, due_at: datetime | None) -> RuleOutcome:
    """Правило модели для записи задачи со сроком `due_at` (§13.5)."""
    return record_rule(understanding.kind, due_at, understanding.repeat)


def task_fields(understanding: Understanding, rule: RuleOutcome | None = None) -> dict[str, Any]:
    """Поля задачи для `record_understanding` — по именам колонок §3.3.

    `repeat` — правило без часа: час серии база возьмёт из срока (§13.2).
    Правило не по форме даёт разовую задачу с пометкой (§13.5).
    """
    outcome = rule if rule is not None else rule_of(understanding, understanding.due_at)
    return {
        "title": understanding.title,
        "kind": understanding.kind,
        "due_at": understanding.due_at.isoformat() if understanding.due_at else None,
        "due_precision": understanding.due_precision,
        "repeat": outcome.rule,
        "priority": understanding.priority,
        "promise": understanding.promise,
        "people": understanding.people,
        "needs_review": understanding.needs_review or outcome.malformed,
    }


def several_items(also: Sequence[TaskItem]) -> tuple[list[tuple[int, TaskItem]], list[str]]:
    """Номера дел сообщения (`techspec/23-several-tasks.md` §23.3) и суть дел
    сверх десяти.

    Номер 1 — поля верхнего уровня, даже когда они ответ, правка или
    болтовня; дело `also[i]` — номер `i + 2`, до десятого. Номер — позиция в
    `also`: дело с пустой сутью пропускается, а номера остальных не
    сдвигаются, и кнопка «Записать отдельно», перечитав разбор, найдёт то же
    дело (решение 5 плана). Сверх десяти — непустые сути по порядку.
    """
    numbered: list[tuple[int, TaskItem]] = []
    beyond: list[str] = []
    for index, item in enumerate(also):
        title = item.title.strip()
        if not title:
            continue
        number = index + 2
        if number <= MAX_ITEMS:
            numbered.append((number, item))
        else:
            beyond.append(title)
    return numbered, beyond


def is_several(understanding: Understanding) -> bool:
    """Сообщение о нескольких делах (`techspec/23-several-tasks.md` §23.5) —
    то, чей разбор пошёл путём `_decide_several`: в `also` есть дело с сутью
    или в сообщении есть поиск (`techspec/24-search.md` §24.4). Кнопки под
    таким ответом шлют итог новым сообщением и не стирают «Ищу»."""
    numbered, beyond = several_items(also_of(understanding))
    return bool(numbered or beyond or searches_of(understanding))


def item_of(understanding: Understanding, item: int) -> Understanding | None:
    """Дело номер `item` из сохранённого разбора (§23.3) как разбор об одном
    деле; номера в разборе нет — `None`.

    Номер 1 — сам разбор: его поля верхнего уровня; `also` запись одного
    дела не читает.
    """
    if item == 1:
        return understanding
    numbered, _ = several_items(also_of(understanding))
    found = next((entry for number, entry in numbered if number == item), None)
    return found.as_understanding() if found is not None else None


def review_reason(understanding: Understanding, rule: RuleOutcome) -> str | None:
    """Причина «Перепроверьте» в ответе: своя у модели и «не разобрал повтор»."""
    own = understanding.review_reason if understanding.needs_review else None
    if rule.malformed:
        return malformed_reason(own)
    return own


def rule_words(rule: Mapping[str, Any] | None) -> str | None:
    """Правило словами для строки «Повтор»; у разовой задачи строки нет."""
    return texts.repeat_words(rule) if rule is not None else None


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
    # Правило после ответа (§13.5) и что стало с правилом модели.
    repeat: Mapping[str, Any] | None = None
    rule: RuleOutcome = NO_RULE


def amendment(asked: OpenQuestion, understanding: Understanding) -> Amendment:
    """Слить ответ модели с задачей, по которой задан вопрос.

    Модель отдаёт только то, что ответ добавил (§5.2 п. 4), и пустое у неё
    значит «не менял», а не «стереть»: нет срока — срок задачи остаётся,
    `normal` — остаётся прежняя срочность, люди — дописываются к названным.
    `kind` из ответа не берётся: вид задачи ответ не меняет.

    Ответ может дать правило вместе со сроком (§13.5): «каждый месяц
    платить за квартиру» — «десятого». Правило уходит, когда оно новое или
    сменился срок — тогда срок становится первым разом; правило не по
    форме — пометка и причина, задача остаётся какой была.
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
    rule = record_rule(asked.kind, due_at, understanding.repeat)
    repeat = asked.repeat if due_at is not None else None
    if rule.rule is not None and ("due_at" in fields or not same_rule(asked.repeat, rule.rule)):
        fields["repeat"] = rule.rule
        repeat = rule.rule
    fields["needs_review"] = understanding.needs_review or rule.malformed
    return Amendment(
        fields=fields,
        title=fields.get("title", asked.title),
        kind=asked.kind,
        due_at=due_at,
        due_precision=due_precision,
        priority=fields.get("priority", asked.priority),
        repeat=repeat,
        rule=rule,
    )


# Правки, которые побеждают ответ на вопрос о той же задаче (§19.5).
CLOSING_ACTIONS = ("done", "cancel")


def edit_closes_asked(
    edit: TaskEdit | None, asked: OpenQuestion | None, tasks: Sequence[TaskDetails] | None
) -> bool:
    """Правка закрывает или убирает ту задачу, о которой вопрос (§19.5).

    «Уже купил», «уже не нужно» в ответ на вопрос — не ответ, а правка:
    модель могла отдать и ответ, и `done` или `cancel`, и тогда побеждает
    правка. Номер задачи — по тому же списку, что ушёл в промпт. Остальные
    правки уступают ответу, как раньше (§12.1).
    """
    if edit is None or asked is None or tasks is None or edit.action not in CLOSING_ACTIONS:
        return False
    task = edits.task_by_number(tasks, edit.task)
    return task is not None and task.id == asked.task_id


def edit_beats_answer(
    edit: TaskEdit | None, asked: OpenQuestion | None, tasks: Sequence[TaskDetails] | None
) -> bool:
    """Правка той задачи, о которой вопрос, главнее ответа на него.

    При вопросах о прошедшем деле (§22.5) побеждает любая правка этой
    задачи: «да» — `done`, «перенеси на понедельник» — `change` по §12.8,
    с прежним часом. При остальных вопросах — только `done` и `cancel`
    (`edit_closes_asked`, §19.5).
    """
    if asked is None or asked.question not in overdue.OVERDUE_QUESTIONS:
        return edit_closes_asked(edit, asked, tasks)
    if edit is None or tasks is None:
        return False
    task = edits.task_by_number(tasks, edit.task)
    return task is not None and task.id == asked.task_id


@dataclass(frozen=True, slots=True)
class NewTask:
    """Новое дело сообщения о нескольких делах (`techspec/23-several-tasks.md`
    §23.3): номер дела, поля задачи и её план."""

    item: int
    task: Mapping[str, Any]
    reminders: list[Planned]


@dataclass(frozen=True, slots=True)
class Decision:
    """Что записать вторым шагом и что ответить человеку.

    `task` и `reminders` — дело номер 1; `more` — новые дела 2–10 сообщения
    о нескольких делах (§23.3).
    """

    reply: str
    task: Mapping[str, Any] | None
    reminders: list[Planned]
    amend: Mapping[str, Any] | None = None
    edit: Mapping[str, Any] | None = None
    buttons: tuple[Button, ...] = ()
    # Дубль (§15.3): задача, о которой сообщение, — новой нет.
    same_task: str | None = None
    more: tuple[NewTask, ...] = ()

    def task_rows(self) -> list[dict[str, Any]]:
        """Аргумент `tasks` для `record_understanding` (§23.6): дела по номерам."""
        rows = []
        if self.task is not None:
            rows.append(
                {
                    "item": 1,
                    "task": dict(self.task),
                    "reminders": [item.as_row() for item in self.reminders],
                }
            )
        rows.extend(
            {
                "item": new.item,
                "task": dict(new.task),
                "reminders": [item.as_row() for item in new.reminders],
            }
            for new in self.more
        )
        return rows


@dataclass(frozen=True, slots=True)
class Edited:
    """Правка узнанной задачи: `edit` для базы, ответ и кнопки под ним (§12.4–12.6).

    Форма `edit` — `techspec/03-schema.md` §3.4: `task_id`, `action`,
    `changes` (только отличия), `schedule` (готовый план) и `question`.

    Ответ — по частям, чтобы ответ о нескольких делах разложил их по своим
    абзацам (`techspec/23-several-tasks.md` §23.4): `head` — итог правки,
    `clash` — суть задач в ту же минуту, что новый срок (§15.5), `title` —
    суть правленой задачи для накладки с сутью. `unclear` — итог и есть
    вопрос неясной правки; `asks` — итог кончается вопросом о прошедшем
    часе (§12.8). `stays` — срок задачи после правки, если она остаётся в
    работе: с ним сравниваются новые дела того же сообщения. `icon` —
    значок итога (`techspec/29-icons.md` §29.1): правка — ✏️, «ничего не
    менял» — ⚠️; вопрос меняет значок на ❓ сам (`message_icon`).
    """

    edit: dict[str, Any]
    head: str
    buttons: tuple[Button, ...] = ()
    clash: tuple[str, ...] = ()
    title: str = ""
    unclear: bool = False
    asks: bool = False
    stays: tuple[datetime | None, str | None] | None = None
    icon: str = texts.ICON_EDIT

    @property
    def reply(self) -> str:
        """Ответ об одном деле — как был: итог и абзац накладки (§12.5, §15.5),
        со значком впереди (§29.2)."""
        return texts.iconed(
            message_icon(self.icon, asks=self.unclear or self.asks),
            paragraphs(self.head, texts.same_time(self.clash) if self.clash else None),
        )


@dataclass(frozen=True, slots=True)
class _Unfound:
    """Перенос ненайденной задачи новой задачей (§12.3): поля, план, строка
    ответа, суть задач на ту же минуту и сам срок для накладки."""

    task: dict[str, Any]
    planned: list[Planned]
    line: str
    clash: tuple[str, ...]
    minute: datetime | None


@dataclass(frozen=True, slots=True)
class _Top:
    """Верхние поля разбора по частям ответа (`techspec/23-several-tasks.md`
    §23.3–23.4).

    `head` — итог ответа на вопрос или правки, `clash` — суть задач в ту же
    минуту, что их новый срок, `title` — суть задачи ответа или правки.
    `question` — вопрос, который ответ задаёт последним: правки, выбора или
    «На когда перенести?»; `asks` — вопрос о прошедшем часе уже в `head`.
    `buttons` — «Вернуть» под итогом, `picks` — кнопки выбора под вопросом.
    `exclude` — задача ответа или правки: в накладку с базой она не входит,
    а её срок после правки (`minute`) сравнивается с новыми делами.
    `first` — верхние поля, когда они новое дело номер 1 (или дубль);
    `unfound` — перенос ненайденной задачи, тоже дело номер 1. `icon` —
    значок итога `head` (`techspec/29-icons.md` §29.1).
    """

    head: str | None = None
    icon: str | None = None
    title: str = ""
    clash: tuple[str, ...] = ()
    question: str | None = None
    asks: bool = False
    buttons: tuple[Button, ...] = ()
    picks: tuple[Button, ...] = ()
    amend: dict[str, Any] | None = None
    edit: dict[str, Any] | None = None
    exclude: str | None = None
    minute: datetime | None = None
    first: Understanding | None = None
    unfound: _Unfound | None = None


@dataclass(frozen=True, slots=True)
class _Line:
    """Новое дело для строки ответа: разбор, правило, план, хвост и вопрос."""

    item: Understanding
    rule: RuleOutcome
    planned: list[Planned]
    tail: str | None
    asks: bool


def same_minute(first: datetime, second: datetime) -> bool:
    """Одна минута — как сравнивает накладка базы (§15.5): секунды не в счёт."""
    return first.replace(second=0, microsecond=0) == second.replace(second=0, microsecond=0)


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


def record_icon(kinds: Iterable[str]) -> str:
    """Значок записи (`techspec/29-icons.md` §29.2): только идеи и желания —
    💡; есть среди записанного дело — ✅: дело ждёт действия, оно главнее."""
    listed = list(kinds)
    if listed and all(kind in IDEA_KINDS for kind in listed):
        return texts.ICON_IDEA
    return texts.ICON_RECORDED


def message_icon(*parts: str | None, asks: bool = False) -> str | None:
    """Значок ответа из нескольких абзацев (§29.2): одно сообщение — один значок.

    Ждёт ответа владельца (уточняющий вопрос, «какую задачу», неясная правка,
    «во сколько?», «На когда перенести?») — ❓; иначе — значок первого
    абзаца, какой есть. `parts` — значки абзацев по порядку, у отсутствующего
    абзаца — `None`.
    """
    if asks:
        return texts.ICON_QUESTION
    return next((icon for icon in parts if icon is not None), None)


def paragraphs(*parts: str | None) -> str:
    """Ответ абзацами: основная строка и то, что к ней добавилось (§14.4, §15.5)."""
    return "\n\n".join(part for part in parts if part)


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
        repeat_next: NextOccurrence | None = None,
        names: NameSource | None = None,
        batches: Batches[Pending] | None = None,
        start_search: SearchStarter | None = None,
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
        # Следующий раз повторяющейся задачи считает база (§13.2). Без неё
        # «сделал» и пропуск по такой задаче — честное «не смог записать».
        self._repeat_next = repeat_next or no_next_occurrence
        # Без источника имён голосовое слышится без подсказок, как до этапа
        # 014 (§9.5). Обычная сборка источник подключает.
        self._names = names
        # Без пачки каждое сообщение разбирается сразу, как до этапа 017: так
        # собираются тесты одного сообщения. Обычная сборка пачку подключает
        # (`techspec/18-forwarded.md` §18.1).
        self._batches = batches
        # Без него поиск завести некуда: на просьбу найти — «Не получилось
        # записать поиск» (§24.6). Обычная сборка его подключает.
        self._start_search = start_search
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
            forwarded_from: str | None = None,
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
                forwarded_from=forwarded_from,
            )

        async def record_understanding(
            *,
            message_id: str,
            owner_telegram_id: int,
            analysis: Mapping[str, Any] | None,
            ai_model: str | None,
            ai_input_tokens: int | None,
            ai_output_tokens: int | None,
            reply: str | None,
            tasks: Sequence[Mapping[str, Any]],
            facts: Sequence[Mapping[str, Any]],
            transcript: str | None = None,
            transcript_confidence: float | None = None,
            amend: Mapping[str, Any] | None = None,
            edit: Mapping[str, Any] | None = None,
            photo_text: str | None = None,
            same_task: str | None = None,
        ) -> list[Task]:
            return await db_tasks.record_understanding(
                db,
                message_id=message_id,
                owner_telegram_id=owner_telegram_id,
                analysis=analysis,
                ai_model=ai_model,
                ai_input_tokens=ai_input_tokens,
                ai_output_tokens=ai_output_tokens,
                reply=reply,
                tasks=tasks,
                facts=facts,
                transcript=transcript,
                transcript_confidence=transcript_confidence,
                amend=amend,
                edit=edit,
                photo_text=photo_text,
                same_task=same_task,
            )

        async def read_question(*, owner_telegram_id: int, since: datetime) -> OpenQuestion | None:
            return await db_tasks.open_question(
                db, owner_telegram_id=owner_telegram_id, since=since
            )

        async def repeat_next(
            *, repeat: Mapping[str, Any], occurrence_at: datetime, after: datetime
        ) -> datetime:
            return await db_reminders.repeat_next(
                db,
                repeat=repeat,
                occurrence_at=occurrence_at,
                after=after,
                timezone=settings.owner_timezone.key,
            )

        async def start_search(*, message_id: str, query: str) -> str:
            return await db_searches.start_search(
                db,
                owner_telegram_id=settings.owner_telegram_id,
                message_id=message_id,
                query=query,
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
            repeat_next=repeat_next,
            names=DatabaseNames(settings, db),
            batches=Batches(),
            start_search=start_search,
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
        sent_at: datetime | None = None,
        from_owner: bool = False,
    ) -> RecordOutcome:
        """Принять текстовое поручение и вернуть готовый ответ.

        `swipe` — сообщение, на которое ответили свайпом: подсказка модели,
        о какой задаче речь (§12.2). `sent_at` — время исходного сообщения,
        `from_owner` — переслано от самого владельца: они нужны строке
        переписки (§18.2). Записанное сообщение ждёт пачку (§18.1); пустой
        ответ — сообщение переписки, которое не голова.
        """
        try:
            saved = await self._record_message(
                owner_telegram_id=self._settings.owner_telegram_id,
                chat_id=chat_id,
                telegram_message_id=telegram_message_id,
                text=text,
                forwarded_from=forwarded_from,
            )
        except DatabaseError as error:
            # Инвариант 4: не отвечаем «Записал», пока база не подтвердила.
            logger.warning("Сообщение не записано: %s", error)
            return RecordOutcome(ok=False, message=texts.NOT_SAVED_MESSAGE)

        if saved.reply:
            return self._repeated(saved.id, saved.reply)

        line = Line(
            sent_at=sent_at or self._clock(),
            text=text,
            forwarded_from=forwarded_from,
            from_owner=from_owner,
        )
        pending = Pending(saved=saved, telegram_message_id=telegram_message_id, line=line)
        together = await self._as_conversation(chat_id, pending)
        if together is not None:
            return together
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
        sent_at: datetime | None = None,
        from_owner: bool = False,
    ) -> RecordOutcome:
        """Принять голосовое или кружок: сохранить, расслышать, дальше как текст (§9.3).

        Файл не качается, пока база не подтвердила, что сообщение записано:
        иначе отвечать было бы не о чем (инвариант 4). Повтор обновления виден
        там же — ответ уже есть, и ни скачивания, ни распознавания не будет.
        Записанное голосовое ждёт пачку, как текст (§18.1): в переписке его
        распознаёт голова вместе с остальными.
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
                forwarded_from=forwarded_from,
            )
        except DatabaseError as error:
            logger.warning("Голосовое не записано: %s", error)
            return RecordOutcome(ok=False, message=texts.NOT_SAVED_MESSAGE)

        if saved.reply:
            return self._repeated(saved.id, saved.reply)

        line = Line(
            sent_at=sent_at or self._clock(),
            forwarded_from=forwarded_from,
            from_owner=from_owner,
            speech=kind,
        )
        pending = Pending(
            saved=saved,
            telegram_message_id=telegram_message_id,
            line=line,
            load_audio=load_audio,
            duration=duration,
        )
        together = await self._as_conversation(chat_id, pending)
        if together is not None:
            return together

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

    async def record_from_photo(
        self,
        *,
        chat_id: int,
        telegram_message_id: int,
        file_id: str,
        media_type: ImageType,
        caption: str,
        load_image: ImageLoader,
        forwarded_from: str | None = None,
    ) -> RecordOutcome:
        """Принять снимок: сохранить, скачать, разобрать одним запросом (§14.2).

        Как у голоса: подпись и файл в базе до того, как кто-то посмотрел на
        снимок (инвариант 5), файл не качается, пока база не подтвердила
        запись, а повтор обновления отвечает сохранённым ответом — без
        скачивания и модели. Правки у снимка нет (§14.3), свайпа тоже;
        открытый вопрос модель видит, список задач — только для сверки
        дублей (`techspec/15-duplicates.md` §15.2).
        """
        try:
            saved = await self._record_message(
                owner_telegram_id=self._settings.owner_telegram_id,
                chat_id=chat_id,
                telegram_message_id=telegram_message_id,
                text=caption,
                kind="photo",
                telegram_file_id=file_id,
                forwarded_from=forwarded_from,
            )
        except DatabaseError as error:
            logger.warning("Снимок не записан: %s", error)
            return RecordOutcome(ok=False, message=texts.NOT_SAVED_MESSAGE)

        if saved.reply:
            return self._repeated(saved.id, saved.reply)

        image = await self._load_image(load_image)
        if image is None:
            return await self._unrecorded(saved, texts.PHOTO_NOT_OPENED, "снимок не скачан")

        asked, context = await asyncio.gather(self._open_question(), self._check_context())
        verdict = await self._analyst.analyze_photo(
            image,
            media_type=media_type,
            caption=caption,
            forwarded_from=forwarded_from,
            open_question=asked,
            tasks=context.tasks,
        )
        if not isinstance(verdict, PhotoAnalysis):
            said = caption.strip()
            if not said:
                # Записывать нечего: без разбора и задачи вопрос остаётся (§3.4).
                return await self._unrecorded(saved, texts.PHOTO_NOT_UNDERSTOOD, verdict.reason)
            # Подпись — слова человека: как текст при отказе модели (§5.4).
            decision = Decision(
                reply=as_is_reply(said),
                task=literal_fields(said),
                reminders=[],
            )
            return await self._write(saved, decision, analysis=None, facts=[], verdict=None)

        understanding = verdict.understanding
        dropped = [name for name in ("edit", "facts") if getattr(understanding, name)]
        if dropped:
            # Текст на снимке — данные, а не команда (инвариант 3): задачи
            # снимок не правит и память не пишет, что бы модель ни отдала.
            logger.info("У снимка %s отброшены: %s", saved.id, ", ".join(dropped))
        photo = understanding.model_copy(update={"edit": None, "facts": []})

        if (asked is None or not photo.answers_question) and photo.kind not in TASK_KINDS:
            # Поручения нет — задачи и подсказки тоже. Разбор записывается:
            # вопрос снимается, как любым другим сообщением (§10.3).
            reply = texts.iconed(texts.ICON_TROUBLE, texts.PHOTO_NO_ERRAND)
            if photo.kind == "about_me":
                reply = texts.iconed(texts.ICON_TROUBLE, texts.PHOTO_ABOUT_ME)
            elif photo.kind == SEARCH_KIND:
                # Снимок поиска не запускает (`techspec/24-search.md` §24.1).
                reply = texts.iconed(texts.ICON_SEARCH, texts.SEARCH_TEXT_ONLY)
            decision = Decision(reply=reply, task=None, reminders=[])
        else:
            try:
                decision = await self._decide(
                    photo, asked, self._clock(), context, telegram_message_id
                )
            except DatabaseError as error:
                logger.warning("Расписание не получено, разбор снимка не записан: %s", error)
                return RecordOutcome(ok=False, message=texts.NOT_SAVED_MESSAGE)
            if photo.more_tasks and any(
                part is not None for part in (decision.task, decision.amend, decision.same_task)
            ):
                more = texts.more_on_photo(photo.more_tasks)
                decision = replace(decision, reply=paragraphs(decision.reply, more))

        return await self._write(
            saved,
            decision,
            analysis=photo.model_dump(mode="json"),
            facts=[],
            verdict=verdict,
            photo_text=photo.photo_text,
        )

    async def _as_conversation(self, chat_id: int, pending: Pending) -> RecordOutcome | None:
        """Встать в пачку чата и, если она переписка, ответить за неё (§18.1).

        `None` — пачки нет или она не переписка: сообщение разбирается само
        по себе, как до этапа 017, только на секунду позже. Переписку
        разбирает её голова; остальные её сообщения получают пустой ответ.
        """
        if self._batches is None:
            return None
        closed = await self._batches.join(chat_id, pending)
        # В пачку встают в порядке записи в базу, а он плавает на доли
        # секунды; порядок в чате — номера сообщений Telegram.
        items = sorted(closed.items, key=lambda item: item.telegram_message_id)
        lines = [item.line for item in items]
        head = batches.head_of(lines)
        if head is None or not batches.is_conversation(lines):
            return None
        if items[head] is not pending:
            return SILENT
        return await self._conversation(items, head, closed.seconds)

    async def _conversation(
        self, items: Sequence[Pending], head: int, seconds: float
    ) -> RecordOutcome:
        """Переписка целиком — один разбор и один ответ (§18.2–18.4).

        Голосовые распознаются параллельно, расшифровки не голов ложатся в
        строки их сообщений (§18.3). Читать нечего — модель не зовётся.
        Отказ модели — одна задача «как есть» с именами собеседников.
        Разбор, ответ и задача ложатся на голову, как у одного сообщения.
        """
        saved = items[head].saved
        lines = [item.line for item in items]
        caption = any(not line.forwarded for line in lines)
        # Журнал — только числа (§18.3): тексты переписки в него не попадают.
        logger.info(
            "Переписка: сообщений %s, переслано %s, подпись %s, голосовых %s, "
            "собрана за %s мс, голова %s",
            len(lines),
            sum(line.forwarded for line in lines),
            "да" if caption else "нет",
            sum(line.speech is not None for line in lines),
            round(seconds * 1000),
            saved.id,
        )
        heard = await self._hear_conversation(items)
        lines = [self._heard(line, heard.get(index)) for index, line in enumerate(lines)]
        await asyncio.gather(
            *(
                self._write_transcript(items[index].saved, result)
                for index, result in heard.items()
                if index != head and isinstance(result, Transcript)
            )
        )
        spoken = heard.get(head)
        transcript = spoken if isinstance(spoken, Transcript) else None
        if not batches.has_words(lines):
            return await self._unrecorded(
                saved, texts.CONVERSATION_NOT_HEARD, "голосовые переписки не расслышаны"
            )

        text = batches.conversation_text(lines, self._clock(), self._settings.owner_timezone)
        asked, context, recent = await asyncio.gather(
            self._open_question(), self._check_context(), self._conversation_recent(items)
        )
        verdict = await self._analyst.analyze_conversation(
            text, open_question=asked, tasks=context.tasks, recent=recent
        )
        if not isinstance(verdict, ConversationAnalysis):
            # Отказ модели (§5.4): одна задача «как есть» — кто писал и подпись.
            title = batches.as_is_title(lines)
            decision = Decision(
                reply=as_is_reply(title),
                task=literal_fields(title),
                reminders=[],
            )
            return await self._write(
                saved, decision, analysis=None, facts=[], verdict=None, transcript=transcript
            )

        understanding = verdict.understanding
        dropped = [name for name in ("edit", "facts") if getattr(understanding, name)]
        if dropped:
            # Переписка — данные, а не команда (инвариант 3): задачи она не
            # правит и память не пишет, что бы модель ни отдала.
            logger.info("У переписки %s отброшены: %s", saved.id, ", ".join(dropped))
        read = understanding.model_copy(update={"edit": None, "facts": []})
        try:
            decision = await self._conversation_decision(
                read, asked, context, items[head].telegram_message_id, caption=caption
            )
        except DatabaseError as error:
            logger.warning("Расписание не получено, разбор переписки не записан: %s", error)
            return RecordOutcome(ok=False, message=texts.NOT_SAVED_MESSAGE)
        return await self._write(
            saved,
            decision,
            analysis=read.model_dump(mode="json"),
            facts=[],
            verdict=verdict,
            transcript=transcript,
        )

    async def _conversation_decision(
        self,
        read: ConversationUnderstanding,
        asked: OpenQuestion | None,
        context: EditContext,
        telegram_message_id: int,
        *,
        caption: bool,
    ) -> Decision:
        """Ответ на переписку (§18.4): как у снимка — одно дело и подсказка.

        Дел нет — короткая фраза, без задачи и подсказки; разбор всё равно
        записывается и снимает открытый вопрос (§10.3). С подписью вместо
        фразы уходит вопрос модели с её догадкой, если он есть: подпись
        была, а дело из неё не понять. Иначе обычные пути разбора — ответ на
        вопрос, дубль, запись — и абзац «В переписке ещё», если что-то
        записано, найдено или дополнено.
        """
        if (asked is None or not read.answers_question) and read.kind not in TASK_KINDS:
            if read.kind == "about_me":
                reply = texts.iconed(texts.ICON_TROUBLE, texts.CONVERSATION_ABOUT_ME)
            elif read.kind == SEARCH_KIND:
                # Переписка поиска не запускает (`techspec/24-search.md` §24.1).
                reply = texts.iconed(texts.ICON_SEARCH, texts.SEARCH_TEXT_ONLY)
            elif caption:
                reply = self._talk_reply(read.reply_hint, fallback=texts.CONVERSATION_NO_ERRAND)
            else:
                reply = texts.iconed(texts.ICON_TROUBLE, texts.CONVERSATION_NO_ERRAND)
            return Decision(reply=reply, task=None, reminders=[])
        decision = await self._decide(read, asked, self._clock(), context, telegram_message_id)
        if read.more_tasks and any(
            part is not None for part in (decision.task, decision.amend, decision.same_task)
        ):
            more = texts.more_in_conversation(read.more_tasks)
            decision = replace(decision, reply=paragraphs(decision.reply, more))
        return decision

    async def _conversation_recent(self, items: Sequence[Pending]) -> str | None:
        """Блок 6 у переписки (§18.2): тот же час, что у своего сообщения.

        Граница — первое сообщение пачки, чтобы сама переписка в блок не
        попала; времени у строк нет — часы бота. Без хранилища блока нет.
        """
        store = self._edits
        if store is None:
            return None
        before = min(
            (item.saved.received_at for item in items if item.saved.received_at is not None),
            default=self._clock(),
        )
        return await self._recent(store, self._clock() - edits.LAST_TASK_WINDOW, before)

    async def _hear_conversation(self, items: Sequence[Pending]) -> dict[int, TranscriptionResult]:
        """Голосовые переписки — параллельно, с одними подсказками имён (§18.2).

        Распознаются свои и из последних 30 пересланных. Имена читаются,
        пока файлы качаются, как у одного голосового (§9.5).
        """
        indexes = batches.to_hear([item.line for item in items])
        if not indexes:
            return {}
        names, sounds = await asyncio.gather(
            self._known_names(),
            asyncio.gather(*(self._fetch(items[index]) for index in indexes)),
        )
        results = await asyncio.gather(*(self._transcribe(sound, names) for sound in sounds))
        for index, result in zip(indexes, results, strict=True):
            item = items[index]
            if isinstance(result, NotTranscribed):
                logger.info("Голосовое %s не расслышано: %s", item.saved.id, result.reason)
            else:
                logger.info(
                    "Расслышано сообщение %s: %s с, знаков %s",
                    item.saved.id,
                    item.duration,
                    len(result.text),
                )
        return dict(zip(indexes, results, strict=True))

    async def _fetch(self, item: Pending) -> bytes | NotTranscribed:
        """Файл голосового из пачки; скачивать нечем — «не расслышал»."""
        if item.load_audio is None:
            return NotTranscribed(reason="download: no loader")
        return await self._download(item.load_audio)

    async def _transcribe(
        self, sound: bytes | NotTranscribed, names: Sequence[str]
    ) -> TranscriptionResult:
        if isinstance(sound, NotTranscribed):
            return sound
        return await self._transcriber.transcribe(sound, names)

    @staticmethod
    def _heard(line: Line, result: TranscriptionResult | None) -> Line:
        """Строка переписки после распознавания: расшифровка или «не расслышал»."""
        if result is None:
            return line
        if isinstance(result, NotTranscribed):
            return replace(line, heard=False)
        return replace(line, text=result.text)

    async def _write_transcript(self, saved: SavedMessage, transcript: Transcript) -> None:
        """Расшифровка голосового переписки — в строку его сообщения (§18.3).

        Без разбора, ответа и задачи: открытый вопрос такая запись не
        снимает (§3.4). Сбой — строка в журнал: ответ о задаче, а не о
        расшифровке.
        """
        try:
            await self._record_understanding(
                message_id=saved.id,
                owner_telegram_id=self._settings.owner_telegram_id,
                analysis=None,
                ai_model=None,
                ai_input_tokens=None,
                ai_output_tokens=None,
                reply=None,
                tasks=[],
                facts=[],
                transcript=transcript.text,
                transcript_confidence=transcript.confidence,
            )
        except DatabaseError as error:
            logger.warning("Расшифровка сообщения %s не записана: %s", saved.id, error)

    def _repeated(self, message_id: str, reply: str) -> RecordOutcome:
        """Повтор того же обновления: ответ уже давали, модель не зовём.

        Кнопок у повтора нет (§12.6): сохранён только текст ответа.
        """
        logger.info("Повтор сообщения %s: отвечаем сохранённым ответом", message_id)
        return RecordOutcome(ok=True, message=reply)

    async def _hear(self, load_audio: AudioLoader) -> TranscriptionResult:
        """Скачать файл и распознать с подсказками. Отказ скачивания — «не расслышал».

        Имена читаются, пока файл качается (§9.5): ожидание не растёт, а
        распознавание получает и файл, и подсказки сразу.
        """
        audio, names = await asyncio.gather(self._download(load_audio), self._known_names())
        if isinstance(audio, NotTranscribed):
            return audio
        return await self._transcriber.transcribe(audio, names)

    @staticmethod
    async def _download(load_audio: AudioLoader) -> bytes | NotTranscribed:
        """Файл голосового в память — или причина, почему не скачался.

        Скачивание — граница с Telegram, и какие исключения оттуда придут,
        сервис не знает и знать не должен: любое из них — причина в журнал,
        человеку честный ответ, сообщение с `file_id` уже в базе.

        В журнал — только тип ошибки, без текста: aiohttp кладёт в текст
        адрес файла, а в адресе у Telegram — токен бота (инвариант 1).
        """
        try:
            return await load_audio()
        except Exception as error:  # noqa: BLE001 - граница Telegram, см. доккомментарий
            logger.warning("Файл не скачан из Telegram: %s", type(error).__name__)
            return NotTranscribed(reason=f"download: {type(error).__name__}")

    async def _known_names(self) -> list[str]:
        """Имена для подсказок: сначала из памяти, затем из задач (§9.5).

        Подсказка не важнее поручения (инвариант 5): база не ответила —
        голосовое распознаётся без подсказок, отказ уходит в журнал.
        """
        if self._names is None:
            return []
        try:
            memory = await self._names.memory_texts(NAME_FACTS_LIMIT)
            people = await self._names.task_people(NAME_TASKS_LIMIT)
        except DatabaseError as error:
            logger.warning("Имена для подсказок не прочитаны, распознавание без них: %s", error)
            return []
        return known_names(memory, people)

    async def _load_image(self, load_image: ImageLoader) -> bytes | None:
        """Скачать снимок в память; отказ скачивания — `None`, причина в журнал.

        Граница с Telegram, как у голоса (`_download`): какие исключения
        оттуда придут, сервис не знает, и любое из них — «не смог открыть».
        В журнал — только тип ошибки: в её тексте бывает токен бота.
        """
        try:
            return await load_image()
        except Exception as error:  # noqa: BLE001 - граница Telegram, см. `_download`
            logger.warning("Снимок не скачан из Telegram: %s", type(error).__name__)
            return None

    async def _not_heard(self, saved: SavedMessage, result: NotTranscribed) -> RecordOutcome:
        """Расшифровки нет: ответ в `reply`, задачи и разбора нет (§9.3)."""
        return await self._unrecorded(saved, texts.NOT_HEARD, result.reason)

    async def _unrecorded(self, saved: SavedMessage, reply: str, reason: str) -> RecordOutcome:
        """Разбирать нечего: ответ в `reply`, ни разбора, ни задачи (§9.3, §14.2).

        Так кончаются нерасслышанный голос, нескачанный снимок и снимок без
        подписи, который модель не разобрала. Открытый вопрос остаётся:
        запись без разбора, задачи и поправки база его не снимает (§3.4), и
        повтор, о котором бот просит, дойдёт до модели вместе с вопросом
        (§10.3). Ответ — со значком «не получилось» (`techspec/29-icons.md`).
        """
        reply = texts.iconed(texts.ICON_TROUBLE, reply)
        try:
            await self._record_understanding(
                message_id=saved.id,
                owner_telegram_id=self._settings.owner_telegram_id,
                analysis=None,
                ai_model=None,
                ai_input_tokens=None,
                ai_output_tokens=None,
                reply=reply,
                tasks=[],
                facts=[],
            )
        except DatabaseError as error:
            # Сообщение с файлом уже в базе, и «сохранил» — правда. Без
            # записанного ответа повтор обновления разберёт заново; это не
            # потеря, а лишний запрос.
            logger.warning("Ответ без разбора не записан: %s", error)
        logger.info("Сообщение %s без разбора: %s", saved.id, reason)
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
        подсказки для правки словом (§12.2) и недавний разговор (§17.3).

        Своё сообщение ведёт разговор: его `chat` отвечает текстом модели
        (§17.2). Пересланное — нет: блока 6 у него нет, а ответ разговора
        не слушается.

        Поиск (`techspec/24-search.md` §24.3) заводится до решения: от того,
        записалась ли его строка, зависит абзац ответа — «Ищу» или «Не
        получилось записать поиск». Ответ записан — его id уходит наружу, и
        обработчик запустит поиск.
        """
        spoken: SpeechQuality | None = None
        if transcript is not None:
            spoken = "low" if transcript.low_confidence else "fine"
        talk = forwarded_from is None
        # Граница блока 6 — само сообщение: ни оно, ни пришедшие после него в
        # блок не попадают (§17.3). Время записи даёт база; нет его — часы.
        before = saved.received_at or self._clock()

        asked, context = await asyncio.gather(
            self._open_question(), self._edit_context(chat_id, forwarded_from, swipe, before)
        )
        verdict = await self._analyst.analyze(
            text,
            forwarded_from=forwarded_from,
            spoken=spoken,
            open_question=asked,
            tasks=context.tasks,
            last_tasks=context.last_tasks,
            swipe=context.swipe,
            recent=context.recent,
        )
        if isinstance(verdict, Analysis):
            understanding = verdict.understanding
            note, search_id = await self._search_note(saved, understanding, talk=talk)
            now = self._clock()
            try:
                decision = await self._decide(
                    understanding,
                    asked,
                    now,
                    context,
                    telegram_message_id,
                    talk=talk,
                    search_note=note,
                )
            except DatabaseError as error:
                # Без плана «Напомню» было бы неправдой, а задача без
                # напоминаний — тихой потерей: честнее не записать (§11.3).
                logger.warning("Расписание не получено, разбор не записан: %s", error)
                return RecordOutcome(ok=False, message=texts.NOT_SAVED_MESSAGE)
            outcome = await self._write(
                saved,
                decision,
                analysis=understanding.model_dump(mode="json"),
                facts=fact_rows(understanding),
                verdict=verdict,
                transcript=transcript,
            )
            if outcome.ok and search_id is not None:
                return replace(outcome, search_id=search_id)
            return outcome
        # Разбора не случилось: записываем буквально и говорим об этом.
        # Срока у такой задачи нет, значит и напоминать не о чем.
        decision = Decision(
            reply=as_is_reply(text),
            task=literal_fields(text),
            reminders=[],
        )
        return await self._write(
            saved, decision, analysis=None, facts=[], verdict=None, transcript=transcript
        )

    async def _search_note(
        self, saved: SavedMessage, understanding: Understanding, *, talk: bool
    ) -> tuple[str | None, str | None]:
        """Абзац ответа о поиске и id заведённого поиска (§24.3–24.4).

        Поисков нет — ничего. Пересланное поиска не запускает (§24.1):
        абзац — `SEARCH_TEXT_ONLY`. Своё — заводится первый поиск: строка в
        базе — «Ищу: …», не записалась — `SEARCH_NOT_SAVED`, и «Ищу» не
        звучит (§24.6); остальные поиски сообщения не запускаются — «Ищу по
        одному». В журнал — только числа и id.
        """
        queries = searches_of(understanding)
        if not queries:
            return None, None
        if not talk:
            logger.info("Поиск в пересланном не запускается: поисков %s", len(queries))
            return texts.SEARCH_TEXT_ONLY, None
        first, rest = queries[0], queries[1:]
        search_id = await self._start(saved.id, first)
        said = texts.searching(first) if search_id is not None else texts.SEARCH_NOT_SAVED
        if rest:
            logger.info("Поиск по одному: ещё поисков в сообщении %s", len(rest))
        return paragraphs(said, texts.one_at_a_time(rest) if rest else None), search_id

    async def _start(self, message_id: str, query: str) -> str | None:
        """Строка поиска в базе (§24.3, шаг 1) или `None`, если не записалась."""
        if self._start_search is None:
            logger.warning("Поиск по сообщению %s некуда записать", message_id)
            return None
        try:
            search_id = await self._start_search(message_id=message_id, query=query)
        except DatabaseError as error:
            logger.warning("Поиск по сообщению %s не записан: %s", message_id, error)
            return None
        logger.info("Поиск %s заведён по сообщению %s", search_id, message_id)
        return search_id

    async def _write(
        self,
        saved: SavedMessage,
        decision: Decision,
        *,
        analysis: Mapping[str, Any] | None,
        facts: Sequence[Mapping[str, Any]],
        verdict: Analysis | PhotoAnalysis | ConversationAnalysis | None,
        transcript: Transcript | None = None,
        photo_text: str | None = None,
    ) -> RecordOutcome:
        """Второй шаг и ответ — общий хвост текста, голоса, снимка и переписки.

        `verdict` — ответ модели, из него модель и токены (§3.2); `None` —
        разбора не было, и записан текст «как есть».
        """
        try:
            recorded = await self._record_understanding(
                message_id=saved.id,
                owner_telegram_id=self._settings.owner_telegram_id,
                analysis=analysis,
                ai_model=verdict.model if verdict is not None else None,
                ai_input_tokens=verdict.input_tokens if verdict is not None else None,
                ai_output_tokens=verdict.output_tokens if verdict is not None else None,
                reply=decision.reply,
                tasks=decision.task_rows(),
                facts=facts,
                transcript=transcript.text if transcript is not None else None,
                transcript_confidence=transcript.confidence if transcript is not None else None,
                amend=decision.amend,
                edit=decision.edit,
                photo_text=photo_text,
                same_task=decision.same_task,
            )
        except DatabaseError as error:
            # Правку и дубль база отклоняет и тогда, когда задачу закрыли или
            # удалили, пока модель думала (§12.3, §15.3): откат целиком,
            # честное «не смог».
            logger.warning("Разбор не записан: %s", error)
            return RecordOutcome(ok=False, message=texts.NOT_SAVED_MESSAGE)

        if facts:
            logger.info("Записано сведений о владельце: %s", len(facts))

        # Задачи сообщения: поправленная ответом или правкой — первая (§23.6).
        new = recorded[1:] if decision.amend is not None or decision.edit is not None else recorded
        if decision.edit is not None:
            logger.info(
                "Правка словом: %s задачи %s", decision.edit["action"], decision.edit["task_id"]
            )
        elif decision.same_task is not None:
            logger.info("Дубль задачи %s: новой задачи нет", decision.same_task)
        elif decision.amend is not None and recorded:
            logger.info("Ответ на вопрос дополнил задачу %s", recorded[0].id)
        if decision.same_task is None and len(new) == 1:
            logger.info("Записана задача %s", new[0].id)
        elif decision.same_task is None and new:
            logger.info("Записано задач из сообщения: %s", len(new))
        elif not recorded:
            logger.info("Задачи нет: сообщение %s сохранено с разбором", saved.id)
        return RecordOutcome(ok=True, message=decision.reply, buttons=decision.buttons)

    async def _decide(
        self,
        understanding: Understanding,
        asked: OpenQuestion | None,
        now: datetime,
        context: EditContext,
        telegram_message_id: int,
        *,
        talk: bool = False,
        search_note: str | None = None,
    ) -> Decision:
        """Пять путей разбора: ответ на вопрос, правка словом, дубль, запись
        с вопросом, обычная запись.

        Ответ дополняет задачу, по которой спрашивали (§10.2): напоминания
        планируются заново по сроку, какой у неё станет, и уходят в `amend`, а
        не новой задачей. Вопрос — только у задачи (§10.1): она записывается
        сразу, с пометкой и текстом вопроса. «Ответ» без открытого вопроса
        отвечать не на что — это обычная запись. Ответ, давший срок, и новая
        задача получают абзац накладки (§15.5).

        Правка — только при блоке 5 в промпте (§12.2): без него `edit` модель
        отдать не могла, а если отдала, бот её не слушает; у пересланного и
        снимка список есть, но правки нет всё равно (§15.2). Ответ на
        открытый вопрос главнее правки (§12.1), кроме `done` и `cancel` той
        задачи, о которой вопрос (§19.5), а при вопросах о прошедшем деле —
        кроме любой её правки (§22.5); оба главнее дубля (§15.3).

        Ответ без срока на свой вопрос о деле без срока (§19.5) — «Хорошо,
        спрошу через неделю.»: срока нет, а изменённые поля ложатся, как в
        §10.2. Ответ без нового срока на «Получилось?» — «На когда
        перенести?» и новый вопрос той же задачи (`amend.question`, §22.4),
        на «На когда перенести?» — «Хорошо, спрошу через неделю.». Свой
        вопрос узнаётся по тексту — константам из `texts.py`.

        План берётся у базы, только когда есть что планировать — задача или
        поправка; у разговора и сведения о себе задачи нет, и звать базу
        незачем. Отказ базы выходит наружу `DatabaseError`.

        `talk` — своё сообщение: разговор отвечает текстом модели (§17.2).
        У пересланного и снимка ответ разговора прежний.

        Есть в разборе дела `also` (`techspec/23-several-tasks.md` §23.3) —
        сообщение о нескольких делах, путь `_decide_several`; без них ответ
        и запись прежние. Тем же путём идёт сообщение с поиском
        (`techspec/24-search.md` §24.4): `search_note` — его абзац, он встаёт
        после записи и перед вопросом, а поиск задачей не становится.
        """
        numbered, beyond = several_items(also_of(understanding))
        if numbered or beyond or search_note is not None:
            return await self._decide_several(
                understanding,
                asked,
                now,
                context,
                telegram_message_id,
                numbered,
                beyond,
                search_note=search_note,
            )
        top = await self._top(understanding, asked, now, context, telegram_message_id)
        if top.first is None:
            unfound = top.unfound
            icon = message_icon(
                top.icon if top.head else None, asks=top.question is not None or top.asks
            )
            return Decision(
                reply=texts.iconed(
                    icon,
                    paragraphs(
                        top.head, texts.same_time(top.clash) if top.clash else None, top.question
                    ),
                ),
                task=unfound.task if unfound is not None else None,
                reminders=unfound.planned if unfound is not None else [],
                amend=top.amend,
                edit=top.edit,
                buttons=top.buttons + top.picks,
            )

        same = self._duplicate_of(understanding, context.tasks)
        if same is not None:
            reply = texts.iconed(
                texts.ICON_RECORDED,
                texts.duplicate_reply(
                    title=same.title,
                    due=self._due_words(same.due_at, same.due_precision),
                    repeat=rule_words(same.repeat),
                ),
            )
            apart = Button(text=texts.APART_BUTTON, data=edits.apart_data(telegram_message_id))
            return Decision(
                reply=reply, task=None, reminders=[], buttons=(apart,), same_task=same.id
            )

        return await self._new_task(understanding, now, talk=talk)

    async def _top(
        self,
        understanding: Understanding,
        asked: OpenQuestion | None,
        now: datetime,
        context: EditContext,
        telegram_message_id: int,
    ) -> _Top:
        """Верхние поля разбора (§23.3): ответ на вопрос, правка — или дело номер 1.

        Ответ на открытый вопрос главнее правки (§12.1), кроме тех правок той
        же задачи, что его побеждают (§19.5, §22.5). Правка — только при
        блоке 5 в промпте (§12.2). Остальное — новое дело номер 1 или дубль:
        их решает вызывающий.
        """
        beats = context.edits and edit_beats_answer(understanding.edit, asked, context.tasks)
        if asked is not None and understanding.answers_question and not beats:
            return await self._answer(understanding, asked, now)
        if understanding.edit is not None and context.tasks is not None and context.edits:
            return await self._edit_top(
                understanding, understanding.edit, context.tasks, now, telegram_message_id
            )
        return _Top(first=understanding)

    async def _answer(
        self, understanding: Understanding, asked: OpenQuestion, now: datetime
    ) -> _Top:
        """Ответ на открытый вопрос (§10.2): поправка задачи и итог по частям.

        Напоминания планируются заново по сроку, какой у задачи станет, и
        уходят в `amend`. Ответ, давший срок, получает накладку (§15.5).
        «Пока не знаю» на свой вопрос о деле без срока (§19.5) — «Хорошо,
        спрошу через неделю.»; ответ без нового срока на «Получилось?» —
        вопрос «На когда перенести?» той же задаче (`amend.question`, §22.4),
        на «На когда перенести?» — «Хорошо, спрошу через неделю.». Свой
        вопрос узнаётся по тексту — константам из `texts.py`.
        """
        changed = amendment(asked, understanding)
        planned = await self._planner(
            due_at=changed.due_at,
            due_precision=changed.due_precision,
            kind=changed.kind,
            now=now,
        )
        clash: list[str] = []
        if "due_at" in changed.fields:
            clash = await self._same_minute_titles(
                changed.due_at, changed.due_precision, exclude=asked.task_id
            )
        head: str | None = texts.understood_reply(
            title=changed.title,
            due=self._due_words(changed.due_at, changed.due_precision),
            review_reason=review_reason(understanding, changed.rule),
            # Срочность звучит, только если её изменил сам ответ.
            priority=changed.priority if "priority" in changed.fields else "normal",
            remind_at=self._remind_words(planned, now),
            repeat=rule_words(changed.repeat),
        )
        amend = {
            "task_id": asked.task_id,
            "fields": changed.fields,
            "reminders": [item.as_row() for item in planned],
        }
        question = None
        if asked.question == texts.UNDATED_QUESTION and changed.due_at is None:
            # «Пока не знаю»: срока нет — спросит через неделю (§19.1).
            head = texts.ASK_LATER
        elif asked.question in overdue.OVERDUE_QUESTIONS and "due_at" not in changed.fields:
            # «Не успел» — спросить, на когда; «пока не знаю» — через неделю (§22.5).
            if asked.question == texts.OVERDUE_QUESTION:
                head = None
                question = texts.OVERDUE_MOVE_QUESTION
                amend["question"] = texts.OVERDUE_MOVE_QUESTION
            else:
                head = texts.ASK_LATER
        return _Top(
            head=head,
            # «Понял» и «спрошу через неделю» — ответ записан (§29.1).
            icon=record_icon([changed.kind]),
            title=changed.title,
            clash=tuple(clash),
            question=question,
            amend=amend,
            exclude=asked.task_id,
            minute=self._minute(changed.due_at, changed.due_precision),
        )

    async def _edit_top(
        self,
        understanding: Understanding,
        edit: TaskEdit,
        tasks: Sequence[TaskDetails],
        now: datetime,
        telegram_message_id: int,
    ) -> _Top:
        """Правка словом (§12.3): задача узнана, кандидаты или не найдено.

        Номер модели переводится в задачу по тому же списку, что ушёл в
        промпт. Кандидаты — вопрос с кнопками, до выбора ничего не меняется.
        Не найдено: перенос записывается новой задачей (инвариант 5) — это
        дело номер 1 (§23.3), остальное — не записывается ничего.
        """
        task = edits.task_by_number(tasks, edit.task)
        if task is not None:
            edited = await self._edit_known(understanding, edit, task, now)
            return _Top(
                head=None if edited.unclear else edited.head,
                icon=edited.icon,
                title=edited.title,
                clash=edited.clash,
                question=edited.head if edited.unclear else None,
                asks=edited.asks,
                buttons=edited.buttons,
                edit=edited.edit,
                exclude=task.id,
                minute=self._minute(*edited.stays) if edited.stays is not None else None,
            )
        candidates = edits.candidates_of(tasks, edit.candidates)
        if candidates:
            timezone = self._settings.owner_timezone
            picks = tuple(
                Button(
                    text=edits.candidate_label(item, timezone),
                    data=edits.pick_data(telegram_message_id, item.id),
                )
                for item in candidates
            )
            return _Top(question=edits.pick_question(edit, now, timezone), picks=picks)
        if edit.action == "change" and edit.due_at is not None and understanding.kind in TASK_KINDS:
            unfound = await self._unfound_task(understanding, edit.due_at, edit.due_precision, now)
            return _Top(
                head=unfound.line,
                # «Не нашёл открытой задачи — записал новую» — запись (§29.1).
                icon=record_icon([understanding.kind]),
                title=understanding.title,
                clash=unfound.clash,
                minute=unfound.minute,
                unfound=unfound,
            )
        return _Top(head=texts.NOT_FOUND.format(title=understanding.title), icon=texts.ICON_TROUBLE)

    async def _decide_several(
        self,
        understanding: Understanding,
        asked: OpenQuestion | None,
        now: datetime,
        context: EditContext,
        telegram_message_id: int,
        numbered: Sequence[tuple[int, TaskItem]],
        beyond: Sequence[str],
        *,
        search_note: str | None = None,
    ) -> Decision:
        """Сообщение о нескольких делах (`techspec/23-several-tasks.md` §23.3).

        Верх — как у одного дела: ответ на вопрос, правка или дело номер 1;
        болтовня и сведение о себе абзаца не получают. Дальше — дела по
        номерам: дубль не связывается, а получает абзац и кнопку «Записать
        отдельно» со своим номером; остальные — новые задачи, каждая со
        своим планом. Вопрос — один: правки или выбора, ответа, а нет их —
        первого нового дела, у которого он есть; у остальных спрошенных —
        пометка и хвост «перепроверьте». Накладка нового дела — с базой (без
        задачи ответа или правки) и с делами выше по номеру, включая срок
        задачи ответа или правки.

        Абзацы — §23.4: итог правки или ответа, запись (одна строка или
        список), дубли, накладки, дела сверх десяти, поиск (`search_note`,
        `techspec/24-search.md` §24.4), вопрос — последним. Вопрос встаёт в
        строку записи, только когда весь ответ — одна эта строка. Поиск в
        верхних полях задачей не становится: его вид не дело.
        """
        top = await self._top(understanding, asked, now, context, telegram_message_id)
        question = top.question
        if question is not None and top.amend is not None:
            # «На когда перенести?» рядом с другими делами называет задачу.
            question = texts.unclear_edit(top.title, question)
        asking = question is not None or top.asks
        clashes = [texts.same_time(top.clash, title=top.title)] if top.clash else []
        above: list[tuple[datetime, str]] = []
        if top.minute is not None:
            above.append((top.minute, top.title))

        items: list[tuple[int, Understanding]] = []
        if top.first is not None and top.first.kind in TASK_KINDS:
            items.append((1, top.first))
        items.extend((number, item.as_understanding()) for number, item in numbered)
        dups: list[tuple[int, str, str]] = []
        fresh: list[tuple[int, Understanding]] = []
        for number, item in items:
            same = self._duplicate_of(item, context.tasks)
            if same is None:
                fresh.append((number, item))
                continue
            said = texts.duplicate_reply(
                title=same.title,
                due=self._due_words(same.due_at, same.due_precision),
                repeat=rule_words(same.repeat),
            )
            dups.append((number, item.title, said))

        plans = await asyncio.gather(
            *(self._plan_item(item, now, top.exclude) for _, item in fresh)
        )
        lines: list[_Line] = []
        rows: list[NewTask] = []
        for (number, item), (planned, titles) in zip(fresh, plans, strict=True):
            rule = rule_of(item, item.due_at)
            row = task_fields(item, rule)
            tail = review_reason(item, rule)
            own = question_of(item)
            asks = own is not None and not asking
            if own is not None and asks:
                asking = True
                question = own
                row = {**row, "needs_review": True, "open_question": own}
                tail = texts.REPEAT_DROPPED if rule.malformed else None
            elif own is not None:
                # Вопрос не задан — пометка и хвост «перепроверьте» (§23.3).
                row = {**row, "needs_review": True}
                reason = (item.review_reason or "").strip() or texts.REVIEW_DEFAULT
                tail = malformed_reason(reason) if rule.malformed else reason
            minute = self._minute(item.due_at, item.due_precision)
            if minute is not None:
                titles = [*titles, *(title for at, title in above if same_minute(at, minute))]
                above.append((minute, item.title))
            if titles:
                clashes.append(texts.same_time(titles, title=item.title))
            lines.append(_Line(item=item, rule=rule, planned=planned, tail=tail, asks=asks))
            rows.append(NewTask(item=number, task=row, reminders=planned))

        more = texts.more_in_message(beyond) if beyond else None
        record = None
        if len(lines) == 1:
            line = lines[0]
            alone = (
                top.head is None
                and not dups
                and not clashes
                and more is None
                and search_note is None
            )
            if alone and line.asks and question is not None:
                record = self._asked_line(line, question, now)
                question = None
            else:
                record = self._record_line(line, now)
        elif lines:
            record = texts.listed_reply(
                [self._record_line(line, now, listed=True) for line in lines]
            )

        several_dups = len(dups) > 1
        apart = tuple(
            Button(
                text=edits.apart_label(title) if several_dups else texts.APART_BUTTON,
                data=edits.apart_data(telegram_message_id, number),
            )
            for number, title, _ in dups
        )
        first: NewTask | None = next((row for row in rows if row.item == 1), None)
        if top.unfound is not None:
            first = NewTask(item=1, task=top.unfound.task, reminders=top.unfound.planned)
        logger.info(
            "Несколько дел: новых %s, дублей %s, сверх десяти %s",
            len(rows) + (top.unfound is not None),
            len(dups),
            len(beyond),
        )
        # Один значок на всё сообщение (`techspec/29-icons.md` §29.2): ждёт
        # ответа — ❓, иначе значок первого абзаца.
        icon = message_icon(
            top.icon if top.head else None,
            record_icon(line.item.kind for line in lines) if lines else None,
            texts.ICON_RECORDED if dups else None,
            texts.ICON_SEARCH if search_note else None,
            asks=asking,
        )
        return Decision(
            reply=texts.iconed(
                icon,
                paragraphs(
                    top.head,
                    record,
                    *(said for _, _, said in dups),
                    *clashes,
                    more,
                    search_note,
                    question,
                ),
            ),
            task=first.task if first is not None else None,
            reminders=first.reminders if first is not None else [],
            amend=top.amend,
            edit=top.edit,
            buttons=tuple(
                Button(
                    text=button.text, data=edits.bound_to_message(button.data, telegram_message_id)
                )
                for button in top.buttons
            )
            + apart
            + top.picks,
            more=tuple(row for row in rows if row.item != 1),
        )

    async def _plan_item(
        self, item: Understanding, now: datetime, exclude: str | None
    ) -> tuple[list[Planned], list[str]]:
        """План напоминаний нового дела (§6.1) и суть задач базы на ту же минуту."""
        planned, titles = await asyncio.gather(
            self._planner(
                due_at=item.due_at, due_precision=item.due_precision, kind=item.kind, now=now
            ),
            self._same_minute_titles(item.due_at, item.due_precision, exclude),
        )
        return planned, titles

    def _record_line(self, line: _Line, now: datetime, *, listed: bool = False) -> str:
        """Строка записи нового дела: «Записал: …» или строка списка (§23.4)."""
        item = line.item
        due = self._due_words(item.due_at, item.due_precision)
        remind_at = self._remind_words(line.planned, now)
        repeat = rule_words(line.rule.rule)
        if listed:
            return texts.listed_line(
                kind=item.kind,
                title=item.title,
                due=due,
                review_reason=line.tail,
                priority=item.priority,
                remind_at=remind_at,
                repeat=repeat,
            )
        return texts.recorded_reply(
            kind=item.kind,
            title=item.title,
            due=due,
            review_reason=line.tail,
            priority=item.priority,
            remind_at=remind_at,
            repeat=repeat,
        )

    def _asked_line(self, line: _Line, question: str, now: datetime) -> str:
        """Запись с вопросом одной строкой (§10.1): ответ — только она."""
        item = line.item
        said = f"{texts.REPEAT_DROPPED}. {question}" if line.rule.malformed else question
        return texts.asked_reply(
            title=item.title,
            question=said,
            due=self._due_words(item.due_at, item.due_precision),
            remind_at=self._remind_words(line.planned, now),
            repeat=rule_words(line.rule.rule),
        )

    async def _new_task(
        self, understanding: Understanding, now: datetime, *, talk: bool = False
    ) -> Decision:
        """Запись с вопросом или обычная запись — сообщение заводит своё.

        Ею же «Записать отдельно» заводит задачу из дубля (§15.4): так, как
        её завёл бы обычный путь, с планом и накладкой на момент нажатия.
        Абзац накладки (§15.5) — после основной строки: «На снимке ещё»
        снимок добавит уже за ним.
        """
        rule = rule_of(understanding, understanding.due_at)
        task_row = task_fields(understanding, rule) if understanding.kind in TASK_KINDS else None
        planned = []
        clash = None
        if task_row is not None:
            planned = await self._planner(
                due_at=understanding.due_at,
                due_precision=understanding.due_precision,
                kind=understanding.kind,
                now=now,
            )
            clash = await self._same_time(understanding.due_at, understanding.due_precision)
        question = question_of(understanding)
        if question is not None:
            # Правило не по форме — причина перед вопросом: пометка стоит и так.
            said = f"{texts.REPEAT_DROPPED}. {question}" if rule.malformed else question
            reply = texts.asked_reply(
                title=understanding.title,
                question=said,
                due=self._due_words(understanding.due_at, understanding.due_precision),
                remind_at=self._remind_words(planned, now),
                repeat=rule_words(rule.rule),
                heads=self._heads(understanding),
            )
            # Открытый вопрос задачи хранится без значка: по тексту бот и
            # модель узнают его в ответе (`techspec/29-icons.md` §29.2).
            task = {
                **task_fields(understanding, rule),
                "needs_review": True,
                "open_question": question,
            }
            return Decision(
                reply=texts.iconed(texts.ICON_QUESTION, paragraphs(reply, clash)),
                task=task,
                reminders=planned,
            )

        return Decision(
            reply=paragraphs(self._reply_for(understanding, planned, now, rule, talk=talk), clash),
            task=task_row,
            reminders=planned,
        )

    async def _edit_context(
        self, chat_id: int, forwarded_from: str | None, swipe: Swipe | None, before: datetime
    ) -> EditContext:
        """Подсказки для правки (§12.2) и недавний разговор (§17.3) — или их
        отсутствие.

        Пересланное правкой не бывает и разговора не ведёт: список — только
        для сверки дублей (§15.2), свайп, последняя задача и разговор не
        читаются. Без хранилища — пустой список и блока 6 нет. Не прочитался
        список — блока 5 нет, и разбор идёт как до правки словом (поручение
        важнее контекста); не прочиталась последняя задача, свайп или
        разговор — нет только этой части. `before` — время самого сообщения,
        граница блока 6.
        """
        if forwarded_from is not None:
            return await self._check_context()
        store = self._edits
        if store is None:
            return EditContext(tasks=[], last_tasks=(), swipe=None)
        now = self._clock()
        since = now - edits.LAST_TASK_WINDOW
        tasks, events, swiped, recent = await asyncio.gather(
            self._open_tasks(store),
            self._last_events(store, since),
            self._swiped(store, chat_id, swipe),
            self._recent(store, since, before),
        )
        if tasks is None:
            return replace(NO_EDIT, recent=recent)
        last_tasks = tuple(edits.last_task_numbers(events, tasks, now))
        line = None
        if swiped is not None:
            numbers = [edits.number_of(tasks, task_id) for task_id in swiped.task_ids]
            known = [number for number in numbers if number is not None]
            line = edits.swipe_line(swiped.target, known, swiped.text)
        logger.info(
            "Контекст правки: задач %s, последние %s, свайп %s",
            len(tasks),
            list(last_tasks),
            line is not None,
        )
        return EditContext(tasks=tasks, last_tasks=last_tasks, swipe=line, recent=recent)

    async def _recent(self, store: EditStore, since: datetime, before: datetime) -> str | None:
        """Блок 6 «Недавний разговор» (§17.3) или `None`, если блока нет.

        Окно — тот же час, что у последней задачи в разговоре (§12.2). База не
        ответила — блока нет, разбор идёт без него. В журнал — только число
        сообщений, тексты — никогда.
        """
        try:
            messages = await store.recent_messages(since, before, conversation.RECENT_LIMIT)
        except DatabaseError as error:
            logger.warning("Недавний разговор не прочитан, разбор без него: %s", error)
            return None
        talk = conversation.recent_block(messages, self._settings.owner_timezone)
        logger.info("Недавний разговор: сообщений %s", 0 if talk is None else talk.count)
        return None if talk is None else talk.text

    async def _check_context(self) -> EditContext:
        """Список для сверки дублей без правки — пересланное и снимок (§15.2).

        Без хранилища — пустой список, и блока у них нет; сбой чтения — тоже
        нет блока, и дубль не ищется.
        """
        store = self._edits
        if store is None:
            return EditContext(tasks=[], last_tasks=(), swipe=None, edits=False)
        tasks = await self._open_tasks(store)
        if tasks is None:
            return NO_EDIT
        logger.info("Список для сверки дублей: задач %s", len(tasks))
        return EditContext(tasks=tasks, last_tasks=(), swipe=None, edits=False)

    async def _open_tasks(self, store: EditStore) -> list[TaskDetails] | None:
        """Открытые задачи по номерам; база не ответила — `None`, блока нет."""
        try:
            return edits.number_tasks(await store.open_tasks(edits.TASK_LIMIT))
        except DatabaseError as error:
            logger.error("Открытые задачи не прочитаны, разбор без правки и дубля: %s", error)
            return None

    @staticmethod
    def _duplicate_of(
        understanding: Understanding, tasks: Sequence[TaskDetails] | None
    ) -> TaskDetails | None:
        """Задача, которую повторяет сообщение (§15.3), — или `None`.

        Номер модели переводится в задачу по тому же списку, что ушёл в
        промпт. Номер вне списка, списка нет или вид не поручение — `same_as`
        не слушается: строка в журнал, сообщение идёт обычным путём.
        """
        number = understanding.same_as
        if number is None:
            return None
        task = edits.task_by_number(tasks, number) if tasks is not None else None
        if task is None or understanding.kind not in TASK_KINDS:
            logger.info(
                "Дубль №%s не принят: вид %s, задач в списке %s",
                number,
                understanding.kind,
                len(tasks) if tasks is not None else "нет",
            )
            return None
        return task

    async def _same_time(
        self, due_at: datetime | None, precision: str | None, exclude: str | None = None
    ) -> str | None:
        """Абзац «В это же время у вас» (`techspec/15-duplicates.md` §15.5) или `None`."""
        titles = await self._same_minute_titles(due_at, precision, exclude)
        return texts.same_time(titles) if titles else None

    async def _same_minute_titles(
        self, due_at: datetime | None, precision: str | None, exclude: str | None = None
    ) -> list[str]:
        """Суть задач базы на ту же минуту (§15.5) — пусто, если накладки нет.

        Сравниваются только сроки со временем: у срока «на день» 18:00 —
        условность, базу о нём не спрашивают. `exclude` — сама задача, когда
        меняется её срок. Запрос — до записи: абзац входит в ответ, который
        ложится в базу вместе с задачей (инвариант 4). Сбой запроса — строка в
        журнал и ответ без абзаца: запись важнее предупреждения.
        """
        store = self._edits
        minute = self._minute(due_at, precision)
        if store is None or minute is None:
            return []
        try:
            titles = await store.same_minute(minute, exclude)
        except DatabaseError as error:
            logger.warning("Накладка не проверена, ответ без абзаца: %s", error)
            return []
        if titles:
            logger.info("Накладка: в ту же минуту ещё задач %s", len(titles))
        return titles

    def _minute(self, due_at: datetime | None, precision: str | None) -> datetime | None:
        """Срок со временем в поясе владельца — то, что сравнивает накладка; у
        срока «на день» и у задачи без срока — `None`."""
        if due_at is None or precision != db_tasks.TIME_PRECISION:
            return None
        if due_at.tzinfo is None:
            return due_at.replace(tzinfo=self._settings.owner_timezone)
        return due_at

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
                if task_id is None:
                    return _Swiped(target="bot", task_ids=(), text=swipe.text)
                return _Swiped(target="reminder", task_ids=(task_id,), text=swipe.text)
            stored = await store.message(chat_id, swipe.telegram_message_id)
        except DatabaseError as error:
            logger.warning("Свайп не прочитан: %s", error)
            return None
        text = swipe.text
        if not text and stored is not None:
            # Голосовое: в Telegram текста нет, расшифровка — в базе.
            text = stored.text
        return _Swiped(target="own", task_ids=stored.tasks if stored is not None else (), text=text)

    async def _unfound_task(
        self,
        understanding: Understanding,
        edit_due_at: datetime,
        edit_precision: str | None,
        now: datetime,
    ) -> _Unfound:
        """Задача из переноса, которой нет в списке (§12.3): поля, план, строка
        ответа и суть задач на ту же минуту.

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
        rule = rule_of(understanding, due_at)
        task = {
            **task_fields(understanding, rule),
            "due_at": due_at.isoformat(),
            "due_precision": precision,
        }
        line = texts.not_found_reply(
            title=understanding.title,
            due=self._due_words(due_at, precision),
            review_reason=review_reason(understanding, rule),
            priority=understanding.priority,
            remind_at=self._remind_words(planned, now),
            repeat=rule_words(rule.rule),
        )
        clash = await self._same_minute_titles(due_at, precision)
        return _Unfound(
            task=task,
            planned=planned,
            line=line,
            clash=tuple(clash),
            minute=self._minute(due_at, precision),
        )

    async def _edit_known(
        self, understanding: Understanding, edit: TaskEdit, task: TaskDetails, now: datetime
    ) -> Edited:
        """Правка узнанной задачи (§12.3, §12.5): что записать и что ответить.

        Её же строит нажатие кнопки кандидата — с разбором из базы, прежним
        часом выбранной задачи и планом на момент нажатия (§12.6, §12.8):
        «уже прошло» считается по `now`. Непонятное значение — вопрос верхнего уровня:
        ничего не меняется, даже понятное, задача получает пометку и вопрос.
        План берётся у базы, только если срок сменился и не снят: иначе
        напоминания задачи остаются как есть или снимаются целиком. Тогда же
        спрашивается накладка (§15.5) — без самой задачи.

        Повторяющаяся задача (§13.3): «сделал» и пропуск переводят её на
        следующий раз, «убрать» убирает серию. У разовой пропуск — то же, что
        «убрать»: в базу он уходит пропуском, и база решает по задаче, какой
        она будет в момент записи.
        """
        if edit.action in ("done", "skip") and task.repeat is not None:
            return await self._advance(task, edit.action, task.repeat, now)
        if edit.action in ("done", "cancel", "skip"):
            if edit.action == "done":
                head = texts.CLOSED
            elif task.repeat is not None:
                head = texts.CANCELLED_SERIES
            else:
                head = texts.CANCELLED
            back = Button(text=texts.REOPEN_BUTTON, data=edits.reopen_data(task.id))
            return Edited(
                edit=edit_row(task, edit.action),
                head=head.format(title=task.title),
                buttons=(back,),
                title=task.title,
            )
        question = (understanding.question or "").strip()
        change = edits.edit_changes(task, edit, self._settings.owner_timezone, now)
        if not question and change.needs_start:
            question = texts.REPEAT_START
        stays = (change.due_at, change.due_precision)
        if question:
            return Edited(
                edit=edit_row(task, "change", question=question),
                head=texts.unclear_edit(task.title, question),
                title=task.title,
                unclear=True,
                stays=(task.due_at, task.due_precision),
            )
        if not change.changes:
            # Задача всё равно пишется в `edit`: база проверит, что она
            # активна, и сообщение станет «о ней» (решение 6 плана). Правка
            # что-то назвала — «Так и записано» тем же видом, что «Поправил»,
            # без «Напомню»: напоминания не трогались (§12.8).
            icon = texts.ICON_EDIT
            if not change.named:
                reply = texts.NOTHING_TO_CHANGE.format(title=task.title)
                icon = texts.ICON_TROUBLE
            else:
                reply = texts.edited_reply(
                    texts.SAME_AS_RECORDED.format(title=task.title),
                    self._due_words(change.due_at, change.due_precision),
                    priority=change.priority if edit.priority is not None else None,
                    people=change.people if edit.people is not None else None,
                    repeat=rule_words(change.repeat),
                )
            return Edited(
                edit=edit_row(task, "change"),
                head=reply,
                title=task.title,
                stays=stays,
                icon=icon,
            )
        priority = change.priority if "priority" in change.changes else None
        people = change.people if "people" in change.changes else None
        planned: list[Planned] = []
        clash: list[str] = []
        asked = None
        if change.due_changed and change.due_at is None:
            # Срок снят — снято и правило: повторять нечего (§13.5).
            removed = texts.DUE_AND_REPEAT_REMOVED if task.repeat else texts.DUE_REMOVED
            reply = removed.format(title=change.title)
        else:
            remind_at = None
            if change.due_changed:
                planned = await self._planner(
                    due_at=change.due_at,
                    due_precision=change.due_precision,
                    kind=task.kind,
                    now=now,
                )
                remind_at = self._remind_words(planned, now)
                clash = await self._same_minute_titles(
                    change.due_at, change.due_precision, exclude=task.id
                )
            if change.repeat_removed:
                head = texts.REPEAT_REMOVED
            elif change.due_changed and not change.repeat_changed:
                head = texts.MOVED_BY_WORD
            else:
                head = texts.FIXED
            if change.lost_at is not None and change.lost_precision is not None:
                timezone = self._settings.owner_timezone
                asked = texts.passed_question(
                    change.lost_at.astimezone(timezone), change.lost_precision
                )
            reply = texts.edited_reply(
                head.format(title=change.title),
                self._due_words(change.due_at, change.due_precision),
                remind_at,
                priority,
                people,
                repeat=rule_words(change.repeat),
                question=asked,
            )
        return Edited(
            edit=edit_row(task, "change", changes=change.changes, schedule=planned),
            head=reply,
            clash=tuple(clash),
            title=change.title,
            asks=asked is not None,
            stays=stays,
        )

    async def _advance(
        self, task: TaskDetails, action: str, rule: Mapping[str, Any], now: datetime
    ) -> Edited:
        """«Сделал» или пропуск повторяющейся задачи — переход на следующий раз (§13.3).

        Следующий раз и его план бот берёт у базы заранее и передаёт готовыми,
        как расписание правки (§12.4): ответ называет ровно записанное. Раз, от
        которого считал бот, уходит в `edit`: задача за время разбора ушла на
        другой — база откажет целиком. Кнопка «Вернуть» несёт оба раза.
        """
        occurrence = task.occurrence_at or task.due_at
        if occurrence is None:
            raise DatabaseError(f"repeating task {task.id} has no occurrence")
        next_at = await self._repeat_next(
            repeat=rule, occurrence_at=occurrence, after=max(occurrence, now)
        )
        precision = series_precision(rule)
        planned = await self._planner(
            due_at=next_at, due_precision=precision, kind=task.kind, now=now
        )
        moved_from = occurrence_seconds(occurrence)
        edit = edit_row(task, action, schedule=planned)
        edit["occurrence"] = moved_from
        edit["next_at"] = next_at.isoformat()
        head = texts.DONE_REPEAT if action == "done" else texts.SKIPPED
        reply = texts.advanced_reply(
            head.format(title=task.title),
            self._due_words(next_at, precision),
            self._remind_words(planned, now),
        )
        back = Button(
            text=texts.REOPEN_BUTTON,
            data=edits.back_data(task.id, moved_from, occurrence_seconds(next_at)),
        )
        return Edited(edit=edit, head=reply, buttons=(back,), title=task.title)

    async def pick(self, *, chat_id: int, telegram_message_id: int, task_id: str) -> PressOutcome:
        """Кнопка кандидата (§12.6): та же правка для выбранной задачи.

        Разбор берётся из базы — из сообщения владельца, под которым задан
        вопрос; расписание — у `reminder_plan` на момент нажатия. Пишет
        `pick_task` одной транзакцией, и по её ответу видно, чья правка
        легла: своя — ответ и кнопка «Вернуть», прежняя (второе нажатие,
        другая кнопка) — сохранённый текст без кнопок. Отказ базы — всплывающий
        ответ, вопрос с кнопками остаётся (решение 10 плана).

        Под ответом о нескольких делах (`techspec/23-several-tasks.md` §23.5)
        итог — новым сообщением, а в `messages.reply` он ложится абзацем к
        прежнему ответу; второе нажатие — подсказка, что правка уже сделана.
        """
        store = self._edits
        if store is None:
            return PressOutcome(message=texts.NOT_PICKED, replace=False)
        try:
            stored = await store.message(chat_id, telegram_message_id)
            if stored is None:
                return PressOutcome(message=texts.DONE_UNKNOWN, replace=False)
            understanding = self._stored_understanding(stored)
            several = understanding is not None and is_several(understanding)
            if stored.task_id is not None:
                return self._pressed_before() if several else self._picked_before(stored.reply)
            if understanding is None or understanding.edit is None:
                return PressOutcome(message=texts.DONE_UNKNOWN, replace=False)
            task = await store.task(task_id)
            if task is None or task.status != db_tasks.ACTIVE_STATUS:
                return PressOutcome(message=texts.PICKED_GONE, replace=False)
            edited = await self._edit_known(understanding, understanding.edit, task, self._clock())
            reply = paragraphs(stored.reply, edited.reply) if several else edited.reply
            picked = await store.pick(stored.id, edited.edit, reply)
        except DatabaseError as error:
            logger.warning("Выбор задачи не записан: %s", error)
            return PressOutcome(message=texts.NOT_PICKED, replace=False)
        if picked.task_id is None:
            return PressOutcome(message=texts.PICKED_GONE, replace=False)
        if picked.task_id == task.id and picked.reply == reply:
            logger.info("Выбрана задача %s: %s", task.id, edited.edit["action"])
            return PressOutcome(
                message=edited.reply, replace=not several, buttons=edited.buttons, follow_up=several
            )
        return self._pressed_before() if several else self._picked_before(picked.reply)

    @staticmethod
    def _pressed_before() -> PressOutcome:
        """Нажатие под ответом о нескольких делах уже записано (§23.5): ответ
        не переписывается — подсказка."""
        logger.info("Нажатие под ответом о нескольких делах уже записано: второй раз не пишем")
        return PressOutcome(message=texts.PRESSED_BEFORE, replace=False)

    @staticmethod
    def _picked_before(reply: str | None) -> PressOutcome:
        """Правка по сообщению уже сделана: сохранённый ответ, без кнопок."""
        logger.info("Правка по сообщению уже записана: второй раз не пишем")
        if not reply:
            return PressOutcome(message=texts.DONE_UNKNOWN, replace=False)
        return PressOutcome(message=reply, replace=True)

    async def apart(self, *, chat_id: int, telegram_message_id: int, item: int = 1) -> PressOutcome:
        """Кнопка «Записать отдельно» под дублем (`techspec/15-duplicates.md` §15.4).

        Разбор берётся из базы — из сообщения владельца, которое бот счёл
        дублем; задача строится так, как её завёл бы обычный путь, расписание
        — у `reminder_plan` на момент нажатия. Пишет `record_separately` одной
        транзакцией и возвращает ответ той записи, что легла: этого нажатия
        или прежнего, — второе нажатие ничего не пишет. Отказ базы или плана —
        подсказка, кнопка остаётся.

        `item` — номер дела в сообщении (`techspec/23-several-tasks.md` §23.5):
        дело берётся из сохранённого разбора по номеру. Под ответом о
        нескольких делах итог — новым сообщением, абзацем к прежнему ответу в
        `messages.reply`; второе нажатие — подсказка, текст не переписывается.
        """
        store = self._edits
        if store is None:
            return PressOutcome(message=texts.NOT_SAVED, replace=False)
        try:
            stored = await store.message(chat_id, telegram_message_id)
            understanding = self._stored_understanding(stored) if stored is not None else None
            chosen = item_of(understanding, item) if understanding is not None else None
            if stored is None or understanding is None or chosen is None:
                return PressOutcome(message=texts.MESSAGE_UNKNOWN, replace=False)
            if chosen.kind not in TASK_KINDS:
                return PressOutcome(message=texts.MESSAGE_UNKNOWN, replace=False)
            several = is_several(understanding)
            decision = await self._new_task(chosen, self._clock())
            if decision.task is None:
                return PressOutcome(message=texts.MESSAGE_UNKNOWN, replace=False)
            reply = decision.reply
            if isinstance(understanding, PhotoUnderstanding) and understanding.more_tasks:
                reply = paragraphs(reply, texts.more_on_photo(understanding.more_tasks))
            if isinstance(understanding, ConversationUnderstanding) and understanding.more_tasks:
                reply = paragraphs(reply, texts.more_in_conversation(understanding.more_tasks))
            written = paragraphs(stored.reply, reply) if several else reply
            picked = await store.record_separately(
                stored.id, decision.task, decision.reminders, written, item
            )
        except DatabaseError as error:
            logger.warning("Задача из дубля не записана: %s", error)
            return PressOutcome(message=texts.NOT_SAVED, replace=False)
        if several:
            if picked.reply != written:
                return self._pressed_before()
            logger.info("Записано отдельно: дело %s сообщения %s", item, stored.id)
            return PressOutcome(message=reply, replace=False, follow_up=True)
        if not picked.reply:
            return PressOutcome(message=texts.MESSAGE_UNKNOWN, replace=False)
        if picked.reply == reply:
            logger.info("Записано отдельно: задача %s", picked.task_id)
        else:
            logger.info("Задача по сообщению %s уже заведена: второй раз не пишем", stored.id)
        return PressOutcome(message=picked.reply, replace=True)

    @staticmethod
    def _stored_understanding(stored: StoredMessage) -> Understanding | None:
        """Разбор из `messages.analysis`; не читается — `None`, правка не угадывается.

        Разбор снимка узнаётся по `photo_text`, переписки — по `more_tasks` без
        него; оба читаются своей моделью: они нужны ответу «Записать
        отдельно» (§15.4, §18.4). Разбор текста и голоса с `also` — своей
        моделью, без него (до этапа 023) — как разбор без других дел
        (`techspec/23-several-tasks.md` §23.2). В разборе, записанном до этапа
        013, нет `same_as` — он читается как «не дубль»; в правке до этапа 021
        нет `time_removed` — она читается как «час не снимали» (§12.8).
        """
        if stored.analysis is None:
            return None
        analysis = {"same_as": None, **stored.analysis}
        if isinstance(analysis.get("edit"), dict):
            analysis["edit"] = {"time_removed": False, **analysis["edit"]}
        model: type[Understanding] = Understanding
        if "photo_text" in analysis:
            model = PhotoUnderstanding
        elif "more_tasks" in analysis:
            model = ConversationUnderstanding
        elif "also" in analysis:
            model = MessageUnderstanding
        try:
            return model.model_validate(analysis)
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
            repeat=rule_words(reopened.repeat),
        )
        # «Вернул в работу» — правка, значок тот же, что у «Закрыл» (§29.2).
        return PressOutcome(message=texts.iconed(texts.ICON_EDIT, reply), replace=True)

    async def reopen_in_message(self, *, chat_id: int, telegram_message_id: int) -> PressOutcome:
        """«Вернуть» под ответом о нескольких делах (`techspec/23-several-tasks.md`
        §23.5): задача — правки этого сообщения, `messages.task_id`.

        Возврат — как у `reopen`; итог приходит новым сообщением и дописывается
        абзацем к ответу сообщения, а список дел в ответе остаётся.
        """
        found = await self._message_with_task(chat_id, telegram_message_id)
        if isinstance(found, PressOutcome):
            return found
        message_id, task_id = found
        return await self._follow_up(message_id, await self.reopen(task_id=task_id))

    async def back_in_message(
        self, *, chat_id: int, telegram_message_id: int, back_to: int, moved_from: int
    ) -> PressOutcome:
        """«Вернуть» повторяющейся под ответом о нескольких делах (§23.5):
        как `back`, итог — новым сообщением и абзацем к ответу."""
        found = await self._message_with_task(chat_id, telegram_message_id)
        if isinstance(found, PressOutcome):
            return found
        message_id, task_id = found
        outcome = await self.back(task_id=task_id, back_to=back_to, moved_from=moved_from)
        return await self._follow_up(message_id, outcome)

    async def _message_with_task(
        self, chat_id: int, telegram_message_id: int
    ) -> tuple[str, str] | PressOutcome:
        """Сообщение владельца под кнопкой и его задача (`messages.id`,
        `messages.task_id`) — или ответ на нажатие."""
        store = self._edits
        if store is None:
            return PressOutcome(message=texts.NOT_REOPENED, replace=False)
        try:
            stored = await store.message(chat_id, telegram_message_id)
        except DatabaseError as error:
            logger.warning("Сообщение под «Вернуть» не прочитано: %s", error)
            return PressOutcome(message=texts.NOT_REOPENED, replace=False)
        if stored is None or stored.task_id is None:
            return PressOutcome(message=texts.DONE_UNKNOWN, replace=False)
        return stored.id, stored.task_id

    async def _follow_up(self, message_id: str, outcome: PressOutcome) -> PressOutcome:
        """Итог нажатия под ответом о нескольких делах (§23.5): новым сообщением,
        а в `messages.reply` — абзацем к прежнему ответу.

        Задача уже вернулась, и дописать абзац не вышло — владельцу об этом
        знать незачем: итог он видит, теряется только строка недавнего
        разговора (§17.3).
        """
        if not outcome.replace:
            return outcome
        store = self._edits
        if store is not None:
            try:
                await store.append_reply(message_id, outcome.message)
            except DatabaseError as error:
                logger.warning("Итог нажатия не дописан к ответу %s: %s", message_id, error)
        return PressOutcome(
            message=outcome.message, replace=False, buttons=outcome.buttons, follow_up=True
        )

    async def back(self, *, task_id: str, back_to: int, moved_from: int) -> PressOutcome:
        """«Вернуть» под «Отметил» и «Пропускаю» (§13.3): задача — снова на разе `back_to`.

        Раз возвращается в час и точность серии; план — у `reminder_plan` на
        момент нажатия. База возвращает, только если задача активна,
        повторяется и стоит на разе `moved_from`, — исход виден по разу в
        ответе. Задача ушла дальше — ничего не меняется и подсказка;
        удалена — «Не нашёл»; отказ базы — «Не смог вернуть».
        """
        store = self._edits
        if store is None:
            return PressOutcome(message=texts.NOT_REOPENED, replace=False)
        now = self._clock()
        try:
            task = await store.task(task_id)
            if task is None:
                return PressOutcome(message=texts.DONE_UNKNOWN, replace=False)
            if task.repeat is None:
                return PressOutcome(message=texts.GONE_FURTHER, replace=False)
            precision = series_precision(task.repeat)
            planned = await self._planner(
                due_at=moment_of(back_to), due_precision=precision, kind=task.kind, now=now
            )
            returned = await store.return_occurrence(task_id, back_to, moved_from, planned)
        except DatabaseError as error:
            logger.warning("Раз задачи не возвращён: %s", error)
            return PressOutcome(message=texts.NOT_REOPENED, replace=False)
        if returned is None:
            return PressOutcome(message=texts.DONE_UNKNOWN, replace=False)
        stands = returned.occurrence_at is not None and (
            occurrence_seconds(returned.occurrence_at) == back_to
        )
        if not stands or returned.status != db_tasks.ACTIVE_STATUS or returned.repeat is None:
            logger.info("Задача %s уже ушла дальше — раз не возвращён", returned.id)
            return PressOutcome(message=texts.GONE_FURTHER, replace=False)
        logger.info("Задача %s возвращена на прежний раз", returned.id)
        reply = texts.edited_reply(
            texts.REOPENED.format(title=returned.title),
            self._due_words(returned.due_at, returned.due_precision),
            self._remind_words(planned, now),
            repeat=rule_words(returned.repeat),
        )
        return PressOutcome(message=texts.iconed(texts.ICON_EDIT, reply), replace=True)

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
        self,
        understanding: Understanding,
        planned: list[Planned],
        now: datetime,
        rule: RuleOutcome,
        *,
        talk: bool = False,
    ) -> str:
        """Ответ человеку по видам, со значком (`techspec/29-icons.md`). Дословно
        из модели — причина, текст записи и ответ разговора.

        Строка «Напомню» берётся из того же плана, который уходит в базу
        (§6.4): бот обещает ровно то, что записал, — и ничего сверх того
        (инвариант 4). Сведение о себе подтверждается словами «Запомнил: …»
        (`techspec/08-memory.md` §8.2); предположения из поручения в ответ
        не попадают — они видны в приложении. Разговор своего сообщения
        (`talk`) отвечает текстом модели (§17.2) без значка, у других видов
        поле не слушается.
        """
        if understanding.kind == "about_me":
            if understanding.facts:
                said = texts.remembered([item.text for item in understanding.facts])
                return texts.iconed(texts.ICON_RECORDED, said)
            # Сведение есть, а нового нет — значит, оно уже в памяти (§8.2).
            return texts.iconed(texts.ICON_RECORDED, texts.ALREADY_KNOWN)
        if understanding.kind not in TASK_KINDS:
            if talk:
                return self._talk_reply(understanding.reply_hint)
            return texts.iconed(texts.ICON_TROUBLE, texts.NO_ERRAND)
        return texts.iconed(
            record_icon([understanding.kind]),
            texts.recorded_reply(
                kind=understanding.kind,
                title=understanding.title,
                due=self._due_words(understanding.due_at, understanding.due_precision),
                review_reason=review_reason(understanding, rule),
                priority=understanding.priority,
                remind_at=self._remind_words(planned, now),
                repeat=rule_words(rule.rule),
                heads=self._heads(understanding),
            ),
        )

    @staticmethod
    def _heads(understanding: Understanding) -> Mapping[str, str]:
        """Первые слова записи по видам: у переписки — «Из переписки записал» (§18.4)."""
        if isinstance(understanding, ConversationUnderstanding):
            return texts.CONVERSATION_BY_KIND
        return texts.RECORDED_BY_KIND

    @staticmethod
    def _talk_reply(hint: str | None, fallback: str = texts.NO_ERRAND) -> str:
        """Ответ разговора (§17.2): текст модели без пробелов по краям, не
        длиннее предела. Пусто — `fallback`: у своего сообщения прежний
        `NO_ERRAND`, у переписки — своя фраза (§18.4).

        Разговор ничего не меняет: ответ, который сообщает о сделанном,
        заменяется на `fallback` (инвариант 4). В журнал — только длина.

        Ответ разговора — без значка: бот отвечает как человек; `fallback` —
        «ничего не записал», со значком «не получилось» (`techspec/29-icons.md`).
        """
        reply = conversation.reply_text(hint)
        if reply is None:
            return texts.iconed(texts.ICON_TROUBLE, fallback)
        if conversation.reports_action(reply):
            logger.warning("Ответ разговора говорит о действии — заменён: знаков %s", len(reply))
            return texts.iconed(texts.ICON_TROUBLE, fallback)
        return reply
