"""Дубль (`techspec/15-duplicates.md` §15.1–15.3): что уходит в базу и что слышит человек.

База, модель и расписание подменены: проверяется операция — какая ветка
главнее, что уходит в `record_understanding` и какими словами бот отвечает.
Что база делает с `same_task`, — в тестах PGlite
`supabase/tests/duplicates.test.ts`.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from solomon import texts
from solomon.db.tasks import OpenQuestion, SavedMessage
from solomon.services.tasks import Button, RecordOutcome, TaskService
from solomon.services.understanding import NotUnderstood, PhotoUnderstanding, Understanding
from tests.conftest import (
    OWNER_ID,
    OWNER_TIMEZONE,
    FakeAnalyst,
    FakeEdits,
    FakeMessages,
    FakePlanner,
    FakeQuestions,
    FakeTranscriber,
    FakeUnderstandings,
    load_image,
    make_details,
    make_photo_understanding,
    make_settings,
    make_understanding,
)
from tests.test_chat_edit_service import (
    LAMP,
    MEETING,
    MEETING_ID,
    MEETING_PLAN,
    MESSAGE_ID,
    NOW,
    OPEN,
    REPORT,
    REPORT_ID,
    build,
    edit,
    saved,
    say,
)

TZ = ZoneInfo(OWNER_TIMEZONE)
FRIDAY_FIVE = datetime(2026, 10, 2, 17, 0, tzinfo=TZ)

MEETING_REPLY = "Это уже записано: встреча с Ренатой. Срок: пятница, 2 октября, 17:00"
APART = (Button(text="Записать отдельно", data=f"apart:{MESSAGE_ID}"),)
MORE_HINT = "На снимке ещё: «купить хлеб». Нужны — напишите или надиктуйте отдельно."

TUESDAYS = {"every": "week", "interval": 1, "weekdays": [2], "month_day": None, "month": None}


def repeated(same_as: int | None, **fields: Any) -> Understanding:
    """Модель узнала в сообщении задачу №`same_as` (§15.2): верхний уровень —
    как для нового поручения, по нему «Записать отдельно» заведёт задачу."""
    top: dict[str, Any] = {
        "title": "созвон с Ренатой",
        "due_at": FRIDAY_FIVE,
        "due_precision": "time",
        "same_as": same_as,
    }
    return make_understanding(**{**top, **fields})


# ------------------------------------------------------------------- ответ


def test_duplicate_reply_names_the_found_task_without_reminders() -> None:
    """«Это уже записано» (§15.3): суть, повтор и срок — без «Напомню» и приоритета."""
    assert texts.duplicate_reply("купить молоко") == "Это уже записано: купить молоко"
    assert texts.duplicate_reply("планёрка", "вторник, 6 октября, 10:00", "каждый вторник") == (
        "Это уже записано: планёрка. Повтор: каждый вторник. Срок: вторник, 6 октября, 10:00"
    )
    assert texts.APART_BUTTON == "Записать отдельно"


# ------------------------------------------------------------ своё сообщение


async def test_duplicate_records_no_task_and_points_the_message_at_the_found_one() -> None:
    """Дубль (§15.3): новой задачи нет, сообщение — о найденной, под ответом кнопка."""
    service, _, understandings, planner, _ = build(repeated(1), planner=FakePlanner(MEETING_PLAN))

    outcome = await say(service, "созвон с Ренатой в пятницу в пять")

    assert outcome.ok
    assert outcome.message == MEETING_REPLY
    assert outcome.buttons == APART
    call = understandings.calls[0]
    assert call["same_task"] == MEETING_ID
    assert (call["task"], call["amend"], call["edit"]) == (None, None, None)
    assert call["reminders"] == []
    assert call["reply"] == MEETING_REPLY
    # Разбор записан целиком: по нему кнопка заведёт задачу (§15.4).
    analysis = saved(understandings, "analysis")
    assert analysis["same_as"] == 1
    assert analysis["title"] == "созвон с Ренатой"
    # Напоминания у найденной задачи прежние — план не нужен.
    assert planner.calls == []


async def test_duplicate_of_a_repeating_task_names_its_rule_and_nearest_time() -> None:
    """Повтор — перед сроком, срок — ближайший раз найденной задачи (§13.7)."""
    tuesday = datetime(2026, 10, 6, 10, 0, tzinfo=TZ)
    standup = make_details(
        id=REPORT_ID,
        title="планёрка",
        due_at=tuesday,
        due_precision="time",
        repeat={**TUESDAYS, "time": "10:00"},
        occurrence_at=tuesday,
    )
    service, _, _, _, _ = build(repeated(1, title="планёрка"), FakeEdits([standup]))

    outcome = await say(service, "планёрка во вторник в десять")

    assert outcome.message == (
        "Это уже записано: планёрка. Повтор: каждый вторник. Срок: вторник, 6 октября, 10:00"
    )


async def test_duplicate_of_a_task_without_a_due_has_no_due_line() -> None:
    service, _, understandings, _, _ = build(
        repeated(3, title="купить лампочку", due_at=None, due_precision=None, priority="high")
    )

    outcome = await say(service, "лампочку купить надо")

    assert outcome.message == "Это уже записано: купить лампочку"
    assert saved(understandings, "same_task") == LAMP.id


async def test_duplicate_puts_neither_a_question_nor_the_review_reason() -> None:
    """Задачи из сообщения нет — спрашивать не о чем (§15.3)."""
    verdict = repeated(
        1, question="Во сколько?", needs_review=True, review_reason="Не понял, какая встреча."
    )
    service, _, understandings, _, _ = build(verdict)

    outcome = await say(service)

    assert outcome.message == MEETING_REPLY
    assert saved(understandings, "task") is None


async def test_duplicate_keeps_the_memory() -> None:
    """Память пишется как обычно (§15.3)."""
    verdict = repeated(1, facts=[{"category": "work", "text": "Рената — заказчица"}])
    service, _, understandings, _, _ = build(verdict)

    await say(service)

    assert saved(understandings, "facts") == [
        {"category": "work", "text": "Рената — заказчица", "status": "guess"}
    ]


async def test_number_outside_the_list_is_logged_and_recorded_new(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`same_as` вне списка не слушается: строка в журнал, обычная запись."""
    service, _, understandings, _, _ = build(repeated(7))

    with caplog.at_level(logging.INFO, logger="solomon.services.tasks"):
        outcome = await say(service)

    assert saved(understandings, "same_task") is None
    assert saved(understandings, "task")["title"] == "созвон с Ренатой"
    assert outcome.message.startswith("Записал: созвон с Ренатой")
    assert outcome.buttons == ()
    assert "Дубль №7 не принят" in caplog.text


@pytest.mark.parametrize("kind", ["chat", "about_me"])
async def test_same_as_of_a_chat_or_about_me_is_not_heard(
    kind: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Разговор и сведение о себе дублем не бывают (§15.3)."""
    service, _, understandings, _, _ = build(repeated(1, kind=kind, due_at=None))

    with caplog.at_level(logging.INFO, logger="solomon.services.tasks"):
        outcome = await say(service)

    assert saved(understandings, "same_task") is None
    assert saved(understandings, "task") is None
    assert outcome.buttons == ()
    assert "Дубль №1 не принят" in caplog.text


async def test_answer_to_the_open_question_beats_the_duplicate() -> None:
    """Ответ на вопрос главнее дубля (§15.3, §10.2)."""
    asked = OpenQuestion(
        task_id=REPORT_ID,
        question="К какому сроку?",
        title="отправить отчёт",
        kind="task",
        due_at=None,
        due_precision=None,
        priority="normal",
        promise=None,
        people=(),
        asked_at=NOW,
    )
    service, _, understandings, _, _ = build(
        repeated(1, answers_question=True), questions=FakeQuestions(asked)
    )

    outcome = await say(service, "к пятнице к пяти")

    assert saved(understandings, "same_task") is None
    assert saved(understandings, "amend")["task_id"] == REPORT_ID
    assert outcome.message.startswith("Понял:")


async def test_edit_beats_the_duplicate() -> None:
    """Правка главнее дубля (§15.3, §12.3)."""
    service, _, understandings, _, _ = build(repeated(1, edit=edit(task=2, action="done")))

    await say(service, "отчёт отправил")

    assert saved(understandings, "same_task") is None
    assert saved(understandings, "edit")["task_id"] == REPORT_ID


async def test_refused_duplicate_is_not_saved() -> None:
    """Найденную задачу закрыли, пока модель думала: база отказывает (§15.3)."""
    service, _, _, _, _ = build(repeated(1), understandings=FakeUnderstandings(broken=True))

    outcome = await say(service)

    assert not outcome.ok
    assert outcome.message == texts.NOT_SAVED
    assert outcome.buttons == ()


async def test_repeated_update_of_a_duplicate_gets_the_saved_reply_without_the_button() -> None:
    """Повтор того же `update` (§15.3, §12.6): сохранённый ответ, кнопки нет."""
    stored = SavedMessage(id="7c1d2e3f-0000-4000-8000-000000000041", reply=MEETING_REPLY)
    service, analyst, understandings, _, _ = build(repeated(1), messages=FakeMessages(stored))

    outcome = await say(service)

    assert outcome.message == MEETING_REPLY
    assert outcome.buttons == ()
    assert analyst.calls == []
    assert understandings.calls == []


@pytest.mark.parametrize("sender", [None, "Аня"])
async def test_without_the_list_there_is_no_duplicate(
    sender: str | None, caplog: pytest.LogCaptureFixture
) -> None:
    """Сбой чтения списка (§15.2): разбор без блока 5, запись — новая."""
    service, analyst, understandings, _, _ = build(
        repeated(1), FakeEdits(OPEN, broken={"open_tasks"})
    )

    with caplog.at_level(logging.ERROR, logger="solomon.services.tasks"):
        await say(service, forwarded_from=sender)

    assert analyst.tasks == [None]
    assert saved(understandings, "same_task") is None
    assert saved(understandings, "task")["title"] == "созвон с Ренатой"
    assert "Открытые задачи не прочитаны" in caplog.text


# ------------------------------------------------------------- пересланное


async def test_forwarded_message_is_checked_against_the_list_but_edits_nothing() -> None:
    """Пересланное (§15.2): список уходит модели, `edit` по-прежнему отброшен."""
    verdict = repeated(
        2,
        title="отправить отчёт",
        due_at=None,
        due_precision=None,
        edit=edit(task=2, action="done"),
    )
    service, analyst, understandings, _, store = build(verdict)

    outcome = await say(service, "жду отчёт к пятнице", forwarded_from="Аня")

    assert analyst.tasks == [[MEETING, REPORT, LAMP]]
    assert analyst.last_tasks == [None]
    assert analyst.swipes == [None]
    assert store.calls == [("open_tasks", 50)]
    assert saved(understandings, "edit") is None
    assert saved(understandings, "same_task") == REPORT_ID
    assert outcome.message == "Это уже записано: отправить отчёт. Срок: пятница, 2 октября"
    assert outcome.buttons == APART


# ------------------------------------------------------------------- снимок


def photo_service(
    photo: PhotoUnderstanding, store: FakeEdits | None
) -> tuple[TaskService, FakeAnalyst, FakeUnderstandings]:
    """Сервис, у которого модель видит только снимок."""
    analyst = FakeAnalyst(NotUnderstood(reason="текста тест не ждал"), photo=photo)
    understandings = FakeUnderstandings()
    service = TaskService(
        settings=make_settings(),
        record_message=FakeMessages(),
        record_understanding=understandings,
        analyst=analyst,
        transcriber=FakeTranscriber(),
        planner=FakePlanner(MEETING_PLAN),
        clock=lambda: NOW,
        edit_store=store,
    )
    return service, analyst, understandings


async def send_photo(service: TaskService) -> RecordOutcome:
    return await service.record_from_photo(
        chat_id=OWNER_ID,
        telegram_message_id=MESSAGE_ID,
        file_id="photo-1",
        media_type="image/jpeg",
        caption="",
        load_image=load_image,
    )


def invitation(**fields: Any) -> PhotoUnderstanding:
    base: dict[str, Any] = {
        "title": "встреча с Ренатой",
        "due_at": FRIDAY_FIVE,
        "due_precision": "time",
        "photo_text": "Встреча с Ренатой, пятница, 17:00",
        "more_tasks": ["купить хлеб"],
    }
    return make_photo_understanding(**{**base, **fields})


async def test_photo_duplicate_keeps_the_rest_of_the_photo() -> None:
    """Снимок (§15.2–15.3): короткий список уходит модели, «На снимке ещё» остаётся."""
    service, analyst, understandings = photo_service(invitation(same_as=1), FakeEdits(OPEN))

    outcome = await send_photo(service)

    assert analyst.tasks == [[MEETING, REPORT, LAMP]]
    assert outcome.message.split(chr(10) * 2) == [MEETING_REPLY, MORE_HINT]
    assert outcome.buttons == APART
    assert saved(understandings, "same_task") == MEETING_ID
    assert saved(understandings, "task") is None
    assert saved(understandings, "photo_text") == "Встреча с Ренатой, пятница, 17:00"


async def test_photo_with_the_list_still_edits_nothing() -> None:
    """`edit` снимка отбрасывается и тогда, когда модель видела список (§15.2)."""
    photo = invitation(edit=edit(task=1, action="done"), more_tasks=[])
    service, _, understandings = photo_service(photo, FakeEdits(OPEN))

    outcome = await send_photo(service)

    assert saved(understandings, "edit") is None
    assert saved(understandings, "task")["title"] == "встреча с Ренатой"
    assert outcome.message.startswith("Записал: встреча с Ренатой")


async def test_photo_without_the_list_is_recorded_new() -> None:
    """Список не прочитан — снимок разбирается без блока 5 (§15.2)."""
    service, analyst, understandings = photo_service(
        invitation(same_as=1), FakeEdits(OPEN, broken={"open_tasks"})
    )

    await send_photo(service)

    assert analyst.tasks == [None]
    assert saved(understandings, "same_task") is None
    assert saved(understandings, "task") is not None
