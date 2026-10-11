"""Разобрать позже (`techspec/33-retry-later.md` §33.2): приём, разбор позже и очередь.

Модель подменена: 503, таймаут и успех задаёт тест, база — фейками в памяти,
которые ведут себя как функции §3.2. Часы владельца — +05:00 круглый год.
Имена в переписках выдуманные.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from solomon import texts
from solomon.db.later import DeferredMessage, deferred_from_row
from solomon.db.rpc import DatabaseError
from solomon.db.tasks import RecentMessage, SavedMessage
from solomon.services.batches import Batches
from solomon.services.later import STALE_AFTER, LaterService
from solomon.services.tasks import (
    Button,
    FileLoader,
    RecordOutcome,
    Swipe,
    TaskService,
    changed_since,
    clock_time,
    swipe_input,
    swipe_of,
)
from solomon.services.transcription import Transcript
from solomon.services.understanding import (
    Analysis,
    NotUnderstood,
    UnderstandingService,
    format_moment,
)
from tests.conftest import (
    IMAGE,
    OWNER_TIMEZONE,
    FakeAnalyst,
    FakeEdits,
    FakeMessages,
    FakePlanner,
    FakeQuestions,
    FakeTranscriber,
    FakeUnderstandings,
    load_audio,
    load_image,
    make_conversation_understanding,
    make_details,
    make_message_understanding,
    make_photo_understanding,
    make_settings,
)
from tests.test_edits import make_edit

SETTINGS = make_settings()
ZONE = ZoneInfo(OWNER_TIMEZONE)
# Сообщение пришло в 16:18 по Екатеринбургу — 11:18 UTC: база отдаёт время в
# UTC, а ответ называет его в поясе владельца.
RECEIVED = datetime(2026, 10, 11, 11, 18, tzinfo=UTC)
NOW = RECEIVED.astimezone(ZONE) + timedelta(seconds=5)
TEXT = "завтра позвонить Олегу"
TAKEN = "⏳ Принял. Сейчас не получается разобрать — разберу, как только смогу, и напишу."
DOWN = NotUnderstood(reason="модель ответила 503", temporary=True)
TIMEOUT = NotUnderstood(reason="модель недоступна: APITimeoutError", temporary=True)
REFUSED = NotUnderstood(reason="модель ответила 400")
# Ответ на исходное после разбора позже: время получения в поясе владельца.
PARSED = (
    "✅ Разобрал ваше сообщение от 16:18:\nЗаписал: позвонить Олегу. Срок: понедельник, 12 октября"
)


class Clock:
    """Часы, которые тест двигает сам."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class FakeDefer:
    """Вместо `defer_message`: что просили отложить. `answer` — ответ базы."""

    def __init__(self, *, broken: bool = False, answer: bool = True) -> None:
        self.broken = broken
        self.answer = answer
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self,
        *,
        message_id: str,
        retry_at: datetime,
        attempts: int,
        retry_input: Mapping[str, Any] | None = None,
        transcript: str | None = None,
        transcript_confidence: float | None = None,
    ) -> bool:
        if self.broken:
            raise DatabaseError("ConnectTimeout: timed out")
        self.calls.append(
            {
                "message_id": message_id,
                "retry_at": retry_at,
                "attempts": attempts,
                "retry_input": None if retry_input is None else dict(retry_input),
                "transcript": transcript,
                "transcript_confidence": transcript_confidence,
            }
        )
        return self.answer


class FakeQueue:
    """Вместо `deferred_waiting`: ждёт ли другое сообщение."""

    def __init__(self, waiting: bool = False, *, broken: bool = False) -> None:
        self.waiting = waiting
        self.broken = broken
        self.calls: list[str] = []

    async def __call__(self, *, message_id: str) -> bool:
        self.calls.append(message_id)
        if self.broken:
            raise DatabaseError("ConnectTimeout: timed out")
        return self.waiting


def build(
    analyst: FakeAnalyst,
    *,
    messages: FakeMessages | None = None,
    understandings: FakeUnderstandings | None = None,
    edits: FakeEdits | None = None,
    defer: Any = None,
    queue: FakeQueue | None = None,
    clock: Any = None,
    questions: FakeQuestions | None = None,
    transcriber: FakeTranscriber | None = None,
    batches: Batches[Any] | None = None,
) -> TaskService:
    """Сервис приёма с отложенными сообщениями; без `defer` — новый `FakeDefer`."""
    return TaskService(
        settings=SETTINGS,
        record_message=messages
        or FakeMessages(SavedMessage(id="m1", reply=None, received_at=RECEIVED)),
        record_understanding=understandings or FakeUnderstandings(),
        analyst=analyst,
        transcriber=transcriber or FakeTranscriber(),
        planner=FakePlanner(),
        clock=clock or (lambda: NOW),
        open_question=questions,
        edit_store=edits,
        defer=defer if defer is not None else FakeDefer(),
        queue=queue or FakeQueue(),
        batches=batches,
    )


def written(understandings: FakeUnderstandings, index: int = 0) -> Any:
    """Запись разбора по порядку вызова: поля — как их видит база."""
    return understandings.calls[index]


def row(**fields: Any) -> DeferredMessage:
    """Ждущее сообщение владельца, как его отдаёт `take_deferred`."""
    base: dict[str, Any] = {
        "id": "m1",
        "chat_id": 42,
        "telegram_message_id": 501,
        "kind": "text",
        "text": TEXT,
        "received_at": RECEIVED,
        "attempts": 2,
    }
    return DeferredMessage(**{**base, **fields})


def recorded(title: str = "позвонить Олегу") -> FakeAnalyst:
    """Модель разобрала поручение: дело на завтра, как назвал владелец."""
    return FakeAnalyst(
        make_message_understanding(
            title=title,
            due_at=datetime(2026, 10, 12, 18, 0, tzinfo=ZONE),
            due_precision="day",
        )
    )


async def text_message(service: TaskService, swipe: Swipe | None = None) -> RecordOutcome:
    return await service.record_from_message(
        chat_id=42, telegram_message_id=501, text=TEXT, swipe=swipe
    )


# --- Чистые правила ------------------------------------------------------------------


def test_time_of_the_message_is_in_the_owner_zone() -> None:
    assert clock_time(RECEIVED, ZONE) == "16:18"


def test_parsed_later_keeps_the_icon_first() -> None:
    # Значок — первый символ сообщения (§29.2), заголовок — сразу за ним.
    assert texts.parsed_later("16:18", "✅ Записал: позвонить Олегу") == (
        "✅ Разобрал ваше сообщение от 16:18:\nЗаписал: позвонить Олегу"
    )
    assert texts.parsed_later("16:18", "❓ Записал: созвон. Во сколько?") == (
        "❓ Разобрал ваше сообщение от 16:18:\nЗаписал: созвон. Во сколько?"
    )


def test_parsed_later_without_an_icon_is_just_the_heading() -> None:
    # Ответ разговора значка не несёт (§29.1).
    assert texts.parsed_later("09:05", "В четверг у вас созвон.") == (
        "Разобрал ваше сообщение от 09:05:\nВ четверг у вас созвон."
    )


def test_swipe_survives_the_round_trip_through_retry_input() -> None:
    swipe = Swipe(telegram_message_id=77, from_bot=True, text="📅 Срок: сегодня, 15:00")

    assert swipe_of(swipe_input(swipe)) == swipe
    assert swipe_input(None) == {}
    assert swipe_of({}) is None
    assert swipe_of({"swipe": {"telegram_message_id": "77"}}) is None


def test_task_changed_after_the_message_is_changed_since() -> None:
    task = make_details(
        created_at=RECEIVED - timedelta(days=1), updated_at=RECEIVED + timedelta(minutes=30)
    )

    assert changed_since(task, RECEIVED)
    assert not changed_since(replace(task, updated_at=RECEIVED - timedelta(minutes=5)), RECEIVED)
    assert not changed_since(task, None)
    assert not changed_since(replace(task, updated_at=None), RECEIVED)


def test_task_made_after_the_message_is_not_changed_since() -> None:
    # Её завело более раннее сообщение той же очереди: владелец говорил о ней такой.
    task = make_details(
        created_at=RECEIVED + timedelta(minutes=20), updated_at=RECEIVED + timedelta(minutes=20)
    )

    assert not changed_since(task, RECEIVED)


def test_deferred_row_is_read_from_the_messages_row() -> None:
    parsed = deferred_from_row(
        {
            "id": "m1",
            "chat_id": 42,
            "telegram_message_id": 501,
            "kind": "voice",
            "text": "в пятницу расчёт Игорю",
            "telegram_file_id": "voice-501",
            "transcript_confidence": "0.92",
            "forwarded_from": None,
            "received_at": "2026-10-11T11:18:00+00:00",
            "reply": None,
            "retry_attempts": 3,
            "retry_input": None,
        }
    )

    assert parsed == row(
        kind="voice",
        text="в пятницу расчёт Игорю",
        telegram_file_id="voice-501",
        transcript_confidence=0.92,
        attempts=3,
    )


def test_deferred_row_without_chat_is_a_refusal() -> None:
    with pytest.raises(DatabaseError):
        deferred_from_row({"id": "m1", "telegram_message_id": 501, "received_at": None})


# --- Приём: модель недоступна (§33.2, шаг 1) ------------------------------------------


@pytest.mark.parametrize("verdict", [DOWN, TIMEOUT], ids=["503", "timeout"])
async def test_unavailable_model_takes_the_message_instead_of_as_is(
    verdict: NotUnderstood,
) -> None:
    understandings = FakeUnderstandings()
    defer = FakeDefer()
    service = build(FakeAnalyst(verdict), understandings=understandings, defer=defer)

    outcome = await text_message(service)

    assert outcome.message == TAKEN
    assert outcome.later
    # «Как есть» не записано: задачи нет, ответа в базе нет.
    assert understandings.calls == []
    [deferred] = defer.calls
    assert deferred["message_id"] == "m1"
    assert deferred["attempts"] == 1
    assert deferred["retry_at"] == NOW + timedelta(minutes=1)


async def test_permanent_refusal_goes_as_is_at_once() -> None:
    understandings = FakeUnderstandings()
    defer = FakeDefer()
    service = build(FakeAnalyst(REFUSED), understandings=understandings, defer=defer)

    outcome = await text_message(service)

    assert outcome.message == f"⚠️ Записал как есть: «{TEXT}». Разобрать сейчас не смог."
    assert not outcome.later
    assert defer.calls == []
    assert written(understandings)["task"]["title"] == TEXT


async def test_without_a_deferrer_a_temporary_refusal_goes_as_is() -> None:
    # Сборка без очереди ведёт себя как до этапа 037.
    service = TaskService(
        settings=SETTINGS,
        record_message=FakeMessages(),
        record_understanding=FakeUnderstandings(),
        analyst=FakeAnalyst(DOWN),
        transcriber=FakeTranscriber(),
        planner=FakePlanner(),
        clock=lambda: NOW,
    )

    outcome = await text_message(service)

    assert outcome.message.startswith("⚠️ Записал как есть")


async def test_defer_failure_falls_back_to_as_is() -> None:
    # Отложить не вышло — поручение всё равно не теряется (инвариант 5).
    understandings = FakeUnderstandings()
    service = build(FakeAnalyst(DOWN), understandings=understandings, defer=FakeDefer(broken=True))

    outcome = await text_message(service)

    assert outcome.message.startswith("⚠️ Записал как есть")
    assert len(understandings.calls) == 1


async def test_new_message_joins_a_waiting_queue_without_calling_the_model() -> None:
    analyst = recorded()
    defer = FakeDefer()
    service = build(analyst, defer=defer, queue=FakeQueue(waiting=True))

    outcome = await text_message(service)

    assert outcome.message == TAKEN
    assert analyst.calls == []
    [deferred] = defer.calls
    assert deferred["attempts"] == 0
    assert deferred["retry_at"] == NOW


async def test_unread_queue_does_not_stop_the_message() -> None:
    # Поручение важнее порядка: база не ответила — разбор как обычно.
    analyst = recorded()
    service = build(analyst, queue=FakeQueue(broken=True))

    outcome = await text_message(service)

    assert outcome.message.startswith("✅ Записал")
    assert len(analyst.calls) == 1


async def test_repeat_of_a_waiting_message_takes_it_again_without_the_model() -> None:
    analyst = recorded()
    defer = FakeDefer()
    saved = SavedMessage(id="m1", reply=None, received_at=RECEIVED, deferred=True)
    service = build(analyst, messages=FakeMessages(saved), defer=defer)

    outcome = await text_message(service)

    assert outcome.message == TAKEN
    assert analyst.calls == []
    assert defer.calls == []


async def test_swipe_is_kept_for_the_later_parse() -> None:
    defer = FakeDefer()
    service = build(FakeAnalyst(DOWN), defer=defer)
    swipe = Swipe(telegram_message_id=77, from_bot=True, text="📅 Срок: сегодня, 15:00")

    await text_message(service, swipe=swipe)

    assert defer.calls[0]["retry_input"] == swipe_input(swipe)


async def test_voice_waits_with_its_transcript() -> None:
    defer = FakeDefer()
    heard = Transcript(text="в пятницу отправить расчёт Игорю", confidence=0.91)
    service = build(FakeAnalyst(DOWN), defer=defer, transcriber=FakeTranscriber(heard))

    outcome = await service.record_from_voice(
        chat_id=42,
        telegram_message_id=501,
        kind="voice",
        file_id="voice-501",
        duration=4,
        load_audio=load_audio,
    )

    assert outcome.message == TAKEN
    [deferred] = defer.calls
    assert deferred["transcript"] == "в пятницу отправить расчёт Игорю"
    assert deferred["transcript_confidence"] == 0.91


async def test_photo_waits_with_its_image_type() -> None:
    defer = FakeDefer()
    analyst = FakeAnalyst(recorded().verdict, photo=DOWN)
    service = build(analyst, defer=defer)

    outcome = await service.record_from_photo(
        chat_id=42,
        telegram_message_id=501,
        file_id="photo-501",
        media_type="image/png",
        caption="счёт за дачу",
        load_image=load_image,
    )

    assert outcome.message == TAKEN
    assert defer.calls[0]["retry_input"] == {"media_type": "image/png"}


async def test_conversation_waits_with_its_text_title_and_caption() -> None:
    defer = FakeDefer()
    analyst = FakeAnalyst(REFUSED, conversation=DOWN)
    at = datetime(2026, 10, 11, 15, 0, tzinfo=ZONE)
    messages = FakeMessages(numbered=True, received={10: RECEIVED, 11: RECEIVED, 12: RECEIVED})
    service = build(
        analyst, messages=messages, defer=defer, batches=Batches(window=0.05, limit=1.0)
    )

    replies = await asyncio.gather(
        service.record_from_message(chat_id=42, telegram_message_id=10, text="напомни в пятницу"),
        service.record_from_message(
            chat_id=42,
            telegram_message_id=11,
            text="Пришлёшь расчёт в пятницу?",
            forwarded_from="Игорь",
            sent_at=at,
        ),
        service.record_from_message(
            chat_id=42,
            telegram_message_id=12,
            text="И мне копию",
            forwarded_from="Олег",
            sent_at=at + timedelta(minutes=2),
        ),
    )

    # Ответ — один, на голове переписки (§18.1).
    assert [reply.message for reply in replies] == ["", "", TAKEN]
    [deferred] = defer.calls
    assert deferred["message_id"] == "m12"
    conversation = deferred["retry_input"]["conversation"]
    assert conversation["text"] == analyst.conversations[0]
    assert conversation["title"] == "Переписка: Игорь, Олег — напомни в пятницу"
    assert conversation["caption"] is True
    assert datetime.fromisoformat(conversation["before"]) == RECEIVED


# --- Разбор позже (§33.2, шаги 2–4) -----------------------------------------------------


async def test_later_parse_answers_on_the_original_with_the_time_received() -> None:
    understandings = FakeUnderstandings()
    service = build(recorded(), understandings=understandings, clock=lambda: NOW)

    outcome = await service.retry(row())

    said = PARSED
    assert outcome.message == said
    # В базе ответ лежит тем, что уйдёт владельцу: повтор и блок 6 видят его.
    assert understandings.calls[0]["reply"] == said
    assert understandings.calls[0]["message_id"] == "m1"


async def test_later_parse_counts_tomorrow_from_the_day_received() -> None:
    # Пришло в 23:50, разбирается в 00:20 следующего дня: «завтра» — от 11-го.
    received = datetime(2026, 10, 11, 18, 50, tzinfo=UTC)
    analyst = recorded()
    service = build(analyst, clock=lambda: received + timedelta(minutes=30))

    await service.retry(row(received_at=received))

    assert analyst.moments == [received]


async def test_model_sees_the_moment_received_not_the_moment_parsed() -> None:
    received = datetime(2026, 10, 11, 18, 50, tzinfo=UTC)
    prompts: list[str] = []

    class _Usage:
        input_tokens = 100
        output_tokens = 20

    class _Answer:
        parsed_output = make_message_understanding()
        stop_reason = "end_turn"
        model = "claude-opus-5"
        usage = _Usage()

    async def call(*, system: str, text: str) -> _Answer:
        prompts.append(system)
        return _Answer()

    later = received + timedelta(hours=1)
    understanding = UnderstandingService(settings=SETTINGS, call=call, clock=lambda: later)

    verdict = await understanding.analyze(TEXT, now=received)

    assert isinstance(verdict, Analysis)
    assert format_moment(received, ZONE) in prompts[0]
    assert format_moment(later, ZONE) not in prompts[0]


async def test_later_recent_block_is_as_it_was_when_received() -> None:
    edits = FakeEdits(
        recent=[
            RecentMessage(
                received_at=RECEIVED - timedelta(minutes=10),
                kind="text",
                text="созвон с Олегом в четверг",
                forwarded_from=None,
                reply="✅ Записал: созвон с Олегом",
            ),
            RecentMessage(
                received_at=RECEIVED + timedelta(minutes=40),
                kind="text",
                text="это пришло позже",
                forwarded_from=None,
                reply=None,
            ),
        ]
    )
    analyst = recorded()
    service = build(analyst, edits=edits, clock=lambda: RECEIVED + timedelta(hours=2))

    await service.retry(row())

    [(since, before, _)] = edits.talks
    assert since == RECEIVED - timedelta(hours=1)
    assert before == RECEIVED
    assert "созвон с Олегом в четверг" in (analyst.recents[0] or "")
    assert "это пришло позже" not in (analyst.recents[0] or "")


async def test_later_still_unavailable_keeps_waiting() -> None:
    understandings = FakeUnderstandings()
    defer = FakeDefer()
    service = build(FakeAnalyst(TIMEOUT), understandings=understandings, defer=defer)

    outcome = await service.retry(row())

    assert outcome.later
    assert outcome.message == ""
    assert understandings.calls == []
    assert defer.calls == [], "разбор позже сам — голова очереди"


async def test_later_database_failure_keeps_waiting() -> None:
    service = build(recorded(), understandings=FakeUnderstandings(broken=True))

    outcome = await service.retry(row())

    assert outcome.later


async def test_later_permanent_refusal_records_as_is_for_good() -> None:
    understandings = FakeUnderstandings()
    service = build(FakeAnalyst(REFUSED), understandings=understandings)

    outcome = await service.retry(row())

    assert outcome.message == f"⚠️ Записал как есть: «{TEXT}». Разобрать так и не смог."
    assert understandings.calls[0]["reply"] == outcome.message


async def test_giving_up_records_as_is_without_the_model() -> None:
    analyst = recorded()
    understandings = FakeUnderstandings()
    service = build(analyst, understandings=understandings)

    outcome = await service.retry(row(), give_up=True)

    assert outcome.message == f"⚠️ Записал как есть: «{TEXT}». Разобрать так и не смог."
    assert analyst.calls == []
    assert written(understandings)["task"]["title"] == TEXT


async def test_later_voice_goes_by_the_saved_transcript() -> None:
    analyst = recorded()
    transcriber = FakeTranscriber()
    understandings = FakeUnderstandings()
    service = build(analyst, transcriber=transcriber, understandings=understandings)

    await service.retry(row(kind="voice", text="в пятницу расчёт Игорю", transcript_confidence=0.4))

    assert transcriber.calls == []
    assert analyst.calls == [("в пятницу расчёт Игорю", None, "low")]
    assert understandings.calls[0]["transcript"] == "в пятницу расчёт Игорю"


async def test_later_photo_is_downloaded_again_by_file_id() -> None:
    analyst = FakeAnalyst(recorded().verdict, photo=make_photo_understanding(title="оплатить счёт"))
    asked: list[str] = []

    async def load(file_id: str) -> bytes:
        asked.append(file_id)
        return IMAGE

    loader: FileLoader = load
    service = build(analyst)

    outcome = await service.retry(
        row(
            kind="photo",
            text="счёт за дачу",
            telegram_file_id="photo-501",
            retry_input={"media_type": "image/png"},
        ),
        load_file=loader,
    )

    assert asked == ["photo-501"]
    assert analyst.photos == [(IMAGE, "image/png", "счёт за дачу", None)]
    assert analyst.moments == [RECEIVED]
    assert outcome.message.startswith("✅ Разобрал ваше сообщение от 16:18:\n")


async def test_later_photo_that_cannot_be_downloaded_is_not_opened() -> None:
    service = build(FakeAnalyst(REFUSED))

    outcome = await service.retry(row(kind="photo", text="", telegram_file_id="photo-501"))

    assert outcome.message == f"⚠️ {texts.PHOTO_NOT_OPENED}"
    assert not outcome.later


async def test_later_conversation_uses_the_saved_text() -> None:
    analyst = FakeAnalyst(
        REFUSED, conversation=make_conversation_understanding(title="прислать Игорю расчёт")
    )
    service = build(analyst)
    conversation = {
        "text": "Переписка (сообщений: 1):\nсегодня 15:00 Игорь: Пришлёшь расчёт?",
        "title": "Переписка: Игорь",
        "caption": False,
        "before": RECEIVED.isoformat(),
    }

    outcome = await service.retry(row(id="m12", retry_input={"conversation": conversation}))

    assert analyst.conversations == [conversation["text"]]
    assert analyst.moments == [RECEIVED]
    assert outcome.message.startswith("✅ Разобрал ваше сообщение от 16:18:\nИз переписки")


async def test_later_conversation_gives_up_with_its_title() -> None:
    service = build(FakeAnalyst(REFUSED))
    conversation = {"text": "…", "title": "Переписка: Игорь", "caption": False}

    outcome = await service.retry(row(retry_input={"conversation": conversation}), give_up=True)

    assert outcome.message == "⚠️ Записал как есть: «Переписка: Игорь». Разобрать так и не смог."


async def test_later_edit_of_a_task_changed_since_asks_instead() -> None:
    task = make_details(
        title="созвон с Олегом",
        created_at=RECEIVED - timedelta(days=1),
        updated_at=RECEIVED + timedelta(minutes=30),
    )
    analyst = FakeAnalyst(
        make_message_understanding(
            title="созвон с Олегом",
            edit=make_edit(
                action="change",
                task=1,
                due_at=datetime(2026, 10, 16, 10, 0, tzinfo=ZONE),
                due_precision="time",
            ),
        )
    )
    understandings = FakeUnderstandings()
    service = build(analyst, edits=FakeEdits([task]), understandings=understandings)

    outcome = await service.retry(row(text="перенеси созвон на пятницу"))

    edit = written(understandings)["edit"]
    assert edit["action"] == "change"
    assert edit["changes"] == {}
    assert edit["question"] == texts.CHANGED_SINCE
    assert outcome.message == (
        "❓ Разобрал ваше сообщение от 16:18:\n"
        "Созвон с Олегом — её уже поменяли после вашего сообщения. Что с ней сделать?"
    )


async def test_later_edit_of_an_untouched_task_applies() -> None:
    task = make_details(
        title="созвон с Олегом",
        created_at=RECEIVED - timedelta(days=1),
        updated_at=RECEIVED - timedelta(hours=1),
    )
    analyst = FakeAnalyst(
        make_message_understanding(title="созвон с Олегом", edit=make_edit(action="done", task=1))
    )
    understandings = FakeUnderstandings()
    service = build(analyst, edits=FakeEdits([task]), understandings=understandings)

    outcome = await service.retry(row(text="созвон с Олегом провёл"))

    assert written(understandings)["edit"]["action"] == "done"
    assert outcome.message.startswith("✏️ Разобрал ваше сообщение от 16:18:\n")


async def test_open_question_asked_after_the_message_is_not_its() -> None:
    from solomon.db.tasks import OpenQuestion

    asked = OpenQuestion(
        task_id="t1",
        question="Когда займётесь?",
        title="купить лампочку",
        kind="task",
        due_at=None,
        due_precision=None,
        priority="normal",
        promise=None,
        people=(),
        asked_at=RECEIVED + timedelta(minutes=20),
    )
    analyst = recorded()
    service = build(
        analyst, questions=FakeQuestions(asked), clock=lambda: RECEIVED + timedelta(hours=1)
    )

    await service.retry(row())

    assert analyst.questions == [None]


# --- Очередь (§33.2): тик, пауза, порядок, шесть часов, перезапуск ----------------------


class FakeLaterStore:
    """Очередь в памяти — как функции §3.2: и `defer_message` для приёма, и
    `take_deferred` / `release_deferred` / `finish_deferred` для очереди.

    Голова — самое раннее ждущее по времени получения; взятая держит очередь
    `STALE_AFTER`; записанный ответ берётся сразу и без попытки. Сообщения,
    которые можно отложить, тест заводит в `known`.
    """

    def __init__(self, clock: Clock, *rows: DeferredMessage) -> None:
        self.clock = clock
        self.known: dict[str, DeferredMessage] = {}
        self.rows: dict[str, DeferredMessage] = {}
        self.retry_at: dict[str, datetime] = {}
        self.started: dict[str, datetime | None] = {}
        self.releases: list[tuple[str, datetime]] = []
        self.finished: list[str] = []
        for each in rows:
            self.wait(each, self.clock())

    def wait(
        self, message: DeferredMessage, retry_at: datetime, started: datetime | None = None
    ) -> None:
        self.rows[message.id] = message
        self.retry_at[message.id] = retry_at
        self.started[message.id] = started

    async def __call__(
        self,
        *,
        message_id: str,
        retry_at: datetime,
        attempts: int,
        retry_input: Mapping[str, Any] | None = None,
        transcript: str | None = None,
        transcript_confidence: float | None = None,
    ) -> bool:
        if message_id in self.retry_at:
            return True
        known = self.known[message_id]
        self.wait(
            replace(
                known,
                attempts=attempts,
                retry_input=dict(retry_input or {}),
                text=transcript if transcript is not None else known.text,
            ),
            retry_at,
        )
        for other, at in list(self.retry_at.items()):
            if other != message_id and self.rows[other].reply is None and at > retry_at:
                self.retry_at[other] = retry_at
        return True

    async def take(self, due_before: datetime, stale_before: datetime) -> DeferredMessage | None:
        if not self.retry_at:
            return None
        head = min(self.retry_at, key=lambda key: (self.rows[key].received_at, key))
        found = self.rows[head]
        started = self.started[head]
        if started is not None and started >= stale_before:
            return None
        if found.reply is None and self.retry_at[head] > due_before:
            return None
        self.started[head] = self.clock()
        if found.reply is None:
            found = replace(found, attempts=found.attempts + 1)
            self.rows[head] = found
        return found

    async def release(self, message_id: str, retry_at: datetime) -> bool:
        self.releases.append((message_id, retry_at))
        self.retry_at[message_id] = retry_at
        self.started[message_id] = None
        return True

    async def finish(self, message_id: str) -> bool:
        self.finished.append(message_id)
        self.retry_at.pop(message_id, None)
        self.started.pop(message_id, None)
        return True


class FakeReplier:
    """Вместо Telegram: что ушло ответом на какое сообщение."""

    def __init__(self, *, broken: bool = False) -> None:
        self.broken = broken
        self.sent: list[tuple[int, int, str, tuple[Button, ...]]] = []

    async def __call__(
        self, *, chat_id: int, reply_to: int, text: str, buttons: Sequence[Button] = ()
    ) -> int:
        if self.broken:
            raise RuntimeError("Telegram is down")
        self.sent.append((chat_id, reply_to, text, tuple(buttons)))
        return 9000 + len(self.sent)


def queue(
    store: FakeLaterStore,
    tasks: Any,
    clock: Clock,
    reply: FakeReplier | None = None,
    launched: list[str] | None = None,
) -> tuple[LaterService, FakeReplier]:
    replier = reply or FakeReplier()

    def launch(search_id: str) -> bool:
        if launched is not None:
            launched.append(search_id)
        return True

    service = LaterService(
        settings=SETTINGS,
        store=store,
        tasks=tasks,
        reply=replier,
        launch_search=launch,
        clock=clock,
    )
    return service, replier


async def test_503_then_timeout_then_success_answers_on_the_original() -> None:
    """Приёмка 1–2: «Принял», пауза, снова недоступна, потом разбор и ответ."""
    clock = Clock(NOW)
    store = FakeLaterStore(clock)
    store.known["m1"] = row(attempts=0)
    analyst = FakeAnalyst(DOWN)
    understandings = FakeUnderstandings()
    tasks = build(analyst, understandings=understandings, defer=store, clock=clock)
    later, replier = queue(store, tasks, clock)

    assert (await text_message(tasks)).message == TAKEN
    assert store.retry_at["m1"] == NOW + timedelta(minutes=1)

    # Пауза ещё не прошла — модель не зовётся.
    clock.now = NOW + timedelta(seconds=30)
    assert await later.run_queue() == 0
    assert len(analyst.calls) == 1

    # Через минуту — таймаут: снова ждёт, пауза — две минуты.
    clock.now = NOW + timedelta(minutes=1)
    analyst.verdict = TIMEOUT
    assert await later.run_queue() == 0
    assert store.releases == [("m1", clock.now + timedelta(minutes=2))]
    assert understandings.calls == []

    # Модель ожила: разбор, запись прежним путём и ответ на исходное.
    clock.now = NOW + timedelta(minutes=3)
    analyst.verdict = recorded().verdict
    assert await later.run_queue() == 1

    said = PARSED
    assert replier.sent == [(42, 501, said, ())]
    assert store.finished == ["m1"]
    [call] = understandings.calls
    assert call["reply"] == said
    assert analyst.moments[-1] == RECEIVED


async def test_head_waits_for_its_pause() -> None:
    clock = Clock(NOW)
    store = FakeLaterStore(clock)
    store.wait(row(), NOW + timedelta(minutes=5))
    analyst = recorded()
    later, replier = queue(store, build(analyst, clock=clock), clock)

    assert await later.run_queue() == 0

    assert analyst.calls == []
    assert replier.sent == []


async def test_six_hours_without_the_model_records_as_is() -> None:
    """Приёмка 4: шесть часов прошли — «Записал как есть: … Разобрать так и не смог.»"""
    clock = Clock(RECEIVED + timedelta(hours=6))
    store = FakeLaterStore(clock, row(attempts=20))
    understandings = FakeUnderstandings()
    later, replier = queue(store, build(FakeAnalyst(DOWN), understandings=understandings), clock)

    assert await later.run_queue() == 1

    said = f"⚠️ Записал как есть: «{TEXT}». Разобрать так и не смог."
    assert replier.sent == [(42, 501, said, ())]
    assert written(understandings)["task"]["title"] == TEXT
    assert store.finished == ["m1"]


async def test_last_attempt_lands_on_the_six_hour_mark() -> None:
    clock = Clock(RECEIVED + timedelta(hours=5, minutes=50))
    store = FakeLaterStore(clock, row(attempts=20))
    later, _ = queue(store, build(FakeAnalyst(DOWN)), clock)

    await later.run_queue()

    assert store.releases == [("m1", RECEIVED + timedelta(hours=6))]


async def test_queue_goes_in_order_and_stops_at_the_first_failure() -> None:
    clock = Clock(NOW + timedelta(minutes=10))
    first = row(id="m1", telegram_message_id=501, text="завтра позвонить Олегу")
    second = row(
        id="m2",
        telegram_message_id=502,
        text="в пятницу расчёт Игорю",
        received_at=RECEIVED + timedelta(minutes=2),
    )
    store = FakeLaterStore(clock, second, first)
    analyst = FakeAnalyst(DOWN)
    later, replier = queue(store, build(analyst, clock=clock), clock)

    assert await later.run_queue() == 0
    # Голова не разобралась — второе не обгоняет её.
    assert [call[0] for call in analyst.calls] == ["завтра позвонить Олегу"]

    clock.now += timedelta(hours=1)
    analyst.verdict = recorded().verdict
    assert await later.run_queue() == 2
    assert [sent[1] for sent in replier.sent] == [501, 502]


async def test_waiting_messages_are_parsed_after_a_restart() -> None:
    """Приёмка 7: строки ждут в базе — новый процесс разбирает их первым тиком."""
    clock = Clock(NOW + timedelta(minutes=30))
    # Прошлый процесс взял голову и упал больше десяти минут назад.
    store = FakeLaterStore(clock)
    store.wait(row(), NOW, started=clock.now - STALE_AFTER - timedelta(seconds=1))
    later, replier = queue(store, build(recorded(), clock=clock), clock)

    assert await later.tick(clock.now) == 0
    await later.wait()

    assert [sent[1] for sent in replier.sent] == [501]
    assert store.finished == ["m1"]


async def test_head_taken_recently_holds_the_queue() -> None:
    clock = Clock(NOW + timedelta(minutes=30))
    store = FakeLaterStore(clock)
    store.wait(row(), NOW, started=clock.now - timedelta(minutes=2))
    analyst = recorded()
    later, _ = queue(store, build(analyst, clock=clock), clock)

    assert await later.run_queue() == 0
    assert analyst.calls == []


class StubRetrier:
    """Вместо `TaskService.retry`: заранее решённые исходы и вызовы."""

    def __init__(self, *outcomes: RecordOutcome) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[str, bool]] = []

    async def retry(
        self, row: DeferredMessage, *, give_up: bool = False, load_file: FileLoader | None = None
    ) -> RecordOutcome:
        self.calls.append((row.id, give_up))
        return self.outcomes.pop(0)


async def test_written_reply_is_sent_without_a_new_parse() -> None:
    clock = Clock(NOW)
    store = FakeLaterStore(clock)
    written = "✅ Разобрал ваше сообщение от 16:18:\nЗаписал: …"
    store.wait(row(reply=written), NOW + timedelta(hours=1))
    retrier = StubRetrier()
    later, replier = queue(store, retrier, clock)

    assert await later.run_queue() == 1

    assert retrier.calls == []
    assert replier.sent[0][2] == "✅ Разобрал ваше сообщение от 16:18:\nЗаписал: …"
    assert store.finished == ["m1"]


async def test_undelivered_reply_returns_to_the_queue() -> None:
    clock = Clock(NOW)
    store = FakeLaterStore(clock, row())
    retrier = StubRetrier(RecordOutcome(ok=True, message="✅ Разобрал …"))
    later, _ = queue(store, retrier, clock, reply=FakeReplier(broken=True))

    assert await later.run_queue() == 0

    assert store.finished == []
    assert store.releases == [("m1", NOW + timedelta(minutes=1))]


async def test_search_from_a_later_parse_starts_after_the_reply() -> None:
    clock = Clock(NOW)
    store = FakeLaterStore(clock, row())
    launched: list[str] = []
    retrier = StubRetrier(RecordOutcome(ok=True, message="🔍 Разобрал …", search_id="s1"))
    later, replier = queue(store, retrier, clock, launched=launched)

    await later.run_queue()

    assert len(replier.sent) == 1
    assert launched == ["s1"]


async def test_buttons_of_the_parse_go_with_the_reply() -> None:
    clock = Clock(NOW)
    store = FakeLaterStore(clock, row())
    back = Button(text="Вернуть", data="reopen:t1")
    retrier = StubRetrier(RecordOutcome(ok=True, message="✏️ Разобрал …", buttons=(back,)))
    later, replier = queue(store, retrier, clock)

    await later.run_queue()

    assert replier.sent[0][3] == (back,)


async def test_broken_parse_counts_as_unavailable() -> None:
    class Broken:
        async def retry(
            self,
            row: DeferredMessage,
            *,
            give_up: bool = False,
            load_file: FileLoader | None = None,
        ) -> RecordOutcome:
            raise RuntimeError("неожиданное")

    clock = Clock(NOW)
    store = FakeLaterStore(clock, row(attempts=1))
    later, replier = queue(store, Broken(), clock)

    assert await later.run_queue() == 0

    assert replier.sent == []
    assert store.releases == [("m1", NOW + timedelta(minutes=2))]


# --- Шаг тика (§6.2) -----------------------------------------------------------------


class FakeLaterTicker:
    """Шаг тика об отложенных: сколько раз запускали; `broken` — падает."""

    def __init__(self, *, broken: bool = False) -> None:
        self.broken = broken
        self.ticks: list[datetime | None] = []

    async def tick(self, now: datetime | None = None) -> int:
        self.ticks.append(now)
        if self.broken:
            raise RuntimeError("очередь сломалась")
        return 0


def reminders_with(later: FakeLaterTicker) -> Any:
    from solomon.services.reminders import ReminderService
    from tests.test_reminders import (
        FakeAnnouncer,
        FakeClearMoved,
        FakeCloser,
        FakeDue,
        FakeMarks,
        FakeMoved,
        FakeNotifier,
    )

    return ReminderService(
        settings=SETTINGS,
        due=FakeDue(),
        mark_sent=FakeMarks(),
        close_task=FakeCloser(),
        notify=FakeNotifier(),
        moved=FakeMoved(),
        clear_moved=FakeClearMoved(),
        announce=FakeAnnouncer(),
        clock=lambda: NOW,
        later=later,
    )


async def test_tick_launches_the_queue_every_time() -> None:
    later = FakeLaterTicker()
    reminders = reminders_with(later)

    await reminders.tick()
    await reminders.tick()

    assert later.ticks == [NOW, NOW]


async def test_broken_queue_step_does_not_stop_the_tick() -> None:
    reminders = reminders_with(FakeLaterTicker(broken=True))

    assert await reminders.tick() == 0


# --- Слой базы (`db/later.py`) -------------------------------------------------------


async def test_defer_message_goes_to_the_database_with_the_owner() -> None:
    from solomon.db import later as db_later
    from tests.test_tasks_db import FakeClient, as_client

    fake = FakeClient(data=True)

    deferred = await db_later.defer_message(
        as_client(fake),
        owner_telegram_id=777,
        message_id="m1",
        retry_at=NOW,
        attempts=1,
        retry_input={"media_type": "image/png"},
    )

    assert deferred is True
    assert fake.calls == [
        (
            "rpc",
            "defer_message",
            {
                "owner_telegram_id": 777,
                "message_id": "m1",
                "retry_at": NOW.isoformat(),
                "attempts": 1,
                "retry_input": {"media_type": "image/png"},
                "transcript": None,
                "transcript_confidence": None,
            },
        )
    ]


async def test_take_deferred_reads_the_head_or_nothing() -> None:
    from solomon.db import later as db_later
    from tests.test_tasks_db import FakeClient, as_client

    head = {
        "id": "m1",
        "chat_id": 42,
        "telegram_message_id": 501,
        "kind": "text",
        "text": TEXT,
        "received_at": RECEIVED.isoformat(),
        "retry_attempts": 2,
        "retry_input": {},
    }
    taken = await db_later.take_deferred(
        as_client(FakeClient(data=[head])),
        owner_telegram_id=777,
        due_before=NOW,
        stale_before=NOW - STALE_AFTER,
    )
    empty = await db_later.take_deferred(
        as_client(FakeClient(data=[])),
        owner_telegram_id=777,
        due_before=NOW,
        stale_before=NOW - STALE_AFTER,
    )

    assert taken == row()
    assert empty is None


async def test_unclear_answer_of_the_queue_is_a_refusal() -> None:
    from solomon.db import later as db_later
    from tests.test_tasks_db import FakeClient, as_client

    with pytest.raises(DatabaseError):
        await db_later.finish_deferred(
            as_client(FakeClient(data=None)), owner_telegram_id=777, message_id="m1"
        )


def test_saved_message_knows_it_is_waiting() -> None:
    from solomon.db.tasks import _message_from_row

    waiting = _message_from_row({"id": "m1", "reply": None, "retry_at": NOW.isoformat()})
    plain = _message_from_row({"id": "m1", "reply": None, "retry_at": None})

    assert waiting.deferred
    assert not plain.deferred


async def test_later_last_task_ignores_a_reminder_sent_after_the_message() -> None:
    from solomon.db.tasks import TaskEvent

    call = make_details(id="5b0c7a52-8f3e-4c1d-9a6b-2e4f1d3c8b91", title="созвон с Олегом")
    report = make_details(id="5b0c7a52-8f3e-4c1d-9a6b-2e4f1d3c8b92", title="расчёт Игорю")
    edits = FakeEdits(
        [call, report],
        message_event=TaskEvent(task_id=call.id, at=RECEIVED - timedelta(minutes=10)),
        reminder_event=TaskEvent(task_id=report.id, at=RECEIVED + timedelta(minutes=10)),
    )
    analyst = recorded()
    service = build(analyst, edits=edits, clock=lambda: RECEIVED + timedelta(minutes=30))

    await service.retry(row(text="перенеси его на пятницу"))

    # Номер последней задачи — по тому же списку, что ушёл модели.
    [numbers] = analyst.last_tasks
    assert len(numbers) == 1
    listed = analyst.tasks[0] or []
    assert [task.title for task in listed][numbers[0] - 1] == "созвон с Олегом"
