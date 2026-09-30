"""Накладка (`techspec/15-duplicates.md` §15.5): абзац «В это же время у вас».

База, модель и расписание подменены: проверяется, на каких путях бот
спрашивает базу о той же минуте и куда встаёт абзац. Сам запрос — в
`test_tasks_db.py`.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

import pytest

from solomon import texts
from solomon.db.tasks import OpenQuestion, TaskDetails
from solomon.services.tasks import PressOutcome
from solomon.services.understanding import Understanding
from tests.conftest import (
    OWNER_ID,
    FakeEdits,
    FakeNext,
    FakePlanner,
    FakeQuestions,
    make_details,
    make_understanding,
)
from tests.test_chat_edit_service import (
    MEETING,
    MEETING_ID,
    MEETING_PLAN,
    MESSAGE_ID,
    NOW,
    OPEN,
    REPORT,
    REPORT_ID,
    TZ,
    build,
    candidate_message,
    edited,
    saved,
    say,
)
from tests.test_duplicates_service import (
    FRIDAY_FIVE,
    MORE_HINT,
    invitation,
    photo_service,
    send_photo,
)
from tests.test_repeat_service import WORKDAYS, seconds
from tests.test_repeat_service import build as build_series
from tests.test_repeat_service import say as say_series

CLASH = "В это же время у вас: «встреча с Ренатой»."
FRIDAY = "Срок: пятница, 2 октября, 17:00. Напомню: 2 октября в 16:00"
KIRILL_ID = "5b5dce72-b099-4fc2-9e4d-8caf6a5b4e55"


def kirill(**fields: Any) -> Understanding:
    """Новое поручение на пятницу, 17:00 — ту же минуту, что у встречи с Ренатой."""
    base: dict[str, Any] = {
        "title": "созвон с Кириллом",
        "due_at": FRIDAY_FIVE,
        "due_precision": "time",
    }
    return make_understanding(**{**base, **fields})


def paragraphs_of(message: str) -> list[str]:
    return message.split(chr(10) * 2)


def test_same_time_names_up_to_three_tasks_and_counts_the_rest() -> None:
    """До трёх задач по сути, дальше — «и ещё N» (§15.5)."""
    assert texts.same_time(["встреча с Ренатой"]) == ("В это же время у вас: «встреча с Ренатой».")
    assert texts.same_time(["планёрка", "звонок маме", "врач"]) == (
        "В это же время у вас: «планёрка», «звонок маме», «врач»."
    )
    assert texts.same_time(["планёрка", "звонок маме", "врач", "сантехник", "курьер"]) == (
        "В это же время у вас: «планёрка», «звонок маме», «врач» и ещё 2."
    )


# ------------------------------------------------------------- новая задача


async def test_new_task_on_a_taken_minute_is_recorded_with_the_warning() -> None:
    """Накладка не запрещает (§15.5): задача пишется, абзац — после основной строки
    и входит в ответ, который ложится в базу вместе с задачей."""
    service, _, understandings, _, store = build(kirill(), planner=FakePlanner(MEETING_PLAN))

    outcome = await say(service, "созвон с Кириллом в пятницу в пять")

    assert outcome.ok
    assert paragraphs_of(outcome.message) == [f"Записал: созвон с Кириллом. {FRIDAY}", CLASH]
    assert saved(understandings, "task")["title"] == "созвон с Кириллом"
    assert saved(understandings, "reply") == outcome.message
    assert store.minutes == [(FRIDAY_FIVE, None)]


async def test_half_an_hour_apart_is_not_the_same_time() -> None:
    """17:00 и 17:30 — не накладка: сравнивается ровно минута."""
    half_past = FRIDAY_FIVE + timedelta(minutes=30)
    service, _, _, _, store = build(kirill(due_at=half_past))

    outcome = await say(service)

    assert len(paragraphs_of(outcome.message)) == 1
    assert store.minutes == [(half_past, None)]


async def test_due_for_a_day_is_not_compared() -> None:
    """Срок «на день» — условные 18:00, а не время встречи: базу не спрашивают."""
    service, _, _, _, store = build(kirill(due_at=REPORT.due_at, due_precision="day"))

    outcome = await say(service)

    assert len(paragraphs_of(outcome.message)) == 1
    assert store.minutes == []


async def test_more_than_three_at_the_minute_are_counted() -> None:
    """До трёх задач по сути, раньше записанные первыми; остальные — числом."""
    taken = [
        make_details(
            id=f"task-{number}",
            title=title,
            due_at=FRIDAY_FIVE,
            due_precision="time",
            created_at=datetime(2026, 9, 20 + number, 9, 0, tzinfo=TZ),
        )
        for number, title in enumerate(["планёрка", "звонок маме", "врач", "сантехник"])
    ]
    service, _, _, _, _ = build(kirill(), FakeEdits(list(reversed(taken))))

    outcome = await say(service)

    assert paragraphs_of(outcome.message)[-1] == (
        "В это же время у вас: «планёрка», «звонок маме», «врач» и ещё 1."
    )


async def test_new_task_with_a_question_gets_the_warning_too() -> None:
    """Запись с вопросом (§10.1) — тоже новая задача: абзац после вопроса."""
    service, _, understandings, _, _ = build(kirill(question="В Zoom или в Телеграме?"))

    outcome = await say(service)

    first, clash = paragraphs_of(outcome.message)
    assert "В Zoom или в Телеграме?" in first
    assert clash == CLASH
    assert saved(understandings, "task")["open_question"] == "В Zoom или в Телеграме?"


async def test_failed_minute_query_is_logged_and_the_task_still_recorded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Сбой запроса — строка в журнал и ответ без абзаца: запись важнее."""
    store = FakeEdits(OPEN, broken={"same_minute"})
    service, _, understandings, _, _ = build(kirill(), store, planner=FakePlanner(MEETING_PLAN))

    with caplog.at_level(logging.WARNING):
        outcome = await say(service)

    assert outcome.ok
    assert outcome.message == f"Записал: созвон с Кириллом. {FRIDAY}"
    assert saved(understandings, "task")["title"] == "созвон с Кириллом"
    assert "Накладка не проверена" in caplog.text


async def test_without_a_store_nothing_is_compared() -> None:
    service, _, _, _, store = build(kirill(), wired=False)

    outcome = await say(service)

    assert outcome.ok
    assert len(paragraphs_of(outcome.message)) == 1
    assert store.minutes == []


async def test_photo_warning_goes_before_the_rest_of_the_photo() -> None:
    """Снимок (§14.4): основная строка, накладка, «На снимке ещё»."""
    service, _, _ = photo_service(invitation(title="созвон с Кириллом"), FakeEdits(OPEN))

    outcome = await send_photo(service)

    assert paragraphs_of(outcome.message) == [
        f"Записал: созвон с Кириллом. {FRIDAY}",
        CLASH,
        MORE_HINT,
    ]


# ------------------------------------------------------------ правка словом


async def test_move_onto_a_taken_minute_warns_and_leaves_the_task_itself_out() -> None:
    """Перенос словом (§12.5): абзац в ответе, сама задача в сравнение не входит."""
    service, _, understandings, _, store = build(
        edited(2, due_at=FRIDAY_FIVE.isoformat(), due_precision="time"),
        planner=FakePlanner(MEETING_PLAN),
    )

    outcome = await say(service, "отчёт перенеси на пятницу на пять")

    assert paragraphs_of(outcome.message) == [f"Перенёс: отправить отчёт. {FRIDAY}", CLASH]
    assert saved(understandings, "reply") == outcome.message
    assert store.minutes == [(FRIDAY_FIVE, REPORT_ID)]


async def test_edit_without_a_new_due_is_not_compared() -> None:
    service, _, _, _, store = build(edited(1, title="встреча с Ренатой и Кириллом"))

    outcome = await say(service, "встреча теперь и с Кириллом")

    assert len(paragraphs_of(outcome.message)) == 1
    assert store.minutes == []


async def test_pick_onto_a_taken_minute_warns_too() -> None:
    """Кнопка «какую задачу» (§12.6) — та же правка: абзац и в ответе, и в базе."""
    verdict = edited(None, candidates=[2, 3], due_at=FRIDAY_FIVE.isoformat(), due_precision="time")
    store = FakeEdits(OPEN, messages={MESSAGE_ID: candidate_message(verdict)})
    service, _, _, _, _ = build(make_understanding(), store, planner=FakePlanner(MEETING_PLAN))

    outcome = await service.pick(
        chat_id=OWNER_ID, telegram_message_id=MESSAGE_ID, task_id=REPORT_ID
    )

    reply = f"Перенёс: отправить отчёт. {FRIDAY}{chr(10) * 2}{CLASH}"
    assert outcome == PressOutcome(message=reply, replace=True)
    assert store.picks[0][2] == reply
    assert store.minutes == [(FRIDAY_FIVE, REPORT_ID)]


async def test_unfound_move_onto_a_taken_minute_warns() -> None:
    """«Не нашёл — записал новую» (§12.3) — новая задача, абзац тот же."""
    service, _, _, _, store = build(
        edited(
            None,
            due_at=FRIDAY_FIVE.isoformat(),
            due_precision="time",
            top_title="созвон с Кириллом",
        ),
        planner=FakePlanner(MEETING_PLAN),
    )

    outcome = await say(service, "созвон с Кириллом переехал на пятницу в пять")

    assert paragraphs_of(outcome.message) == [
        f"Не нашёл открытой задачи — записал новую: созвон с Кириллом. {FRIDAY}",
        CLASH,
    ]
    assert store.minutes == [(FRIDAY_FIVE, None)]


# ------------------------------------------------------------ ответ на вопрос


def asked_about_report() -> OpenQuestion:
    return OpenQuestion(
        task_id=REPORT_ID,
        question="Во сколько отправить?",
        title=REPORT.title,
        kind="task",
        due_at=REPORT.due_at,
        due_precision="day",
        priority="normal",
        promise=None,
        people=REPORT.people,
        asked_at=NOW - timedelta(minutes=5),
    )


async def test_answer_that_gives_a_taken_minute_warns() -> None:
    """Ответ на вопрос дал срок со временем (§10.2): абзац, задача вопроса — не в счёт."""
    verdict = kirill(title=REPORT.title, answers_question=True)
    service, _, understandings, _, store = build(
        verdict, planner=FakePlanner(MEETING_PLAN), questions=FakeQuestions(asked_about_report())
    )

    outcome = await say(service, "в пять")

    first, clash = paragraphs_of(outcome.message)
    assert first.startswith("Понял: отправить отчёт")
    assert clash == CLASH
    assert saved(understandings, "amend")["task_id"] == REPORT_ID
    assert store.minutes == [(FRIDAY_FIVE, REPORT_ID)]


async def test_answer_without_a_new_due_is_not_compared() -> None:
    verdict = make_understanding(title=REPORT.title, priority="high", answers_question=True)
    service, _, _, _, store = build(verdict, questions=FakeQuestions(asked_about_report()))

    outcome = await say(service, "это срочно")

    assert outcome.message.startswith("Понял: отправить отчёт")
    assert store.minutes == []


# ------------------------------------------------------- что не проверяется

WEDNESDAY_NINE = datetime(2026, 9, 30, 9, 0, tzinfo=TZ)
THURSDAY_NINE = WEDNESDAY_NINE + timedelta(days=1)


def standup(at: datetime) -> TaskDetails:
    return make_details(
        id=REPORT_ID,
        title="планёрка",
        due_at=at,
        due_precision="time",
        repeat={**WORKDAYS, "time": "09:00"},
        occurrence_at=at,
    )


def call_at(at: datetime) -> TaskDetails:
    return make_details(id=KIRILL_ID, title="созвон с Кириллом", due_at=at, due_precision="time")


async def test_reopen_does_not_compare() -> None:
    """«Вернуть» возвращает то, что было (§15.5): базу о минуте не спрашивают."""
    store = FakeEdits([replace(MEETING, status="done"), call_at(FRIDAY_FIVE)])
    service, _, _, _, _ = build(make_understanding(), store, planner=FakePlanner(MEETING_PLAN))

    outcome = await service.reopen(task_id=MEETING_ID)

    assert outcome.message.startswith("Вернул в работу: встреча с Ренатой")
    assert len(paragraphs_of(outcome.message)) == 1
    assert store.minutes == []


async def test_next_time_of_a_series_does_not_compare() -> None:
    """Час серии назначен раньше: «Отметил» не проверяет следующий раз."""
    store = FakeEdits([standup(WEDNESDAY_NINE), call_at(THURSDAY_NINE)])
    rig = build_series(edited(1, action="done"), store, following=FakeNext(THURSDAY_NINE))

    outcome = await say_series(rig, "планёрка прошла")

    assert outcome.message.startswith("Отметил: планёрка")
    assert store.minutes == []


async def test_back_of_a_series_does_not_compare() -> None:
    """«Вернуть раз» — тоже возврат того, что было."""
    store = FakeEdits([standup(THURSDAY_NINE), call_at(WEDNESDAY_NINE)])
    rig = build_series(make_understanding(), store)

    outcome = await rig.service.back(
        task_id=REPORT_ID, back_to=seconds(WEDNESDAY_NINE), moved_from=seconds(THURSDAY_NINE)
    )

    assert outcome.message.startswith("Вернул в работу: планёрка")
    assert store.minutes == []
