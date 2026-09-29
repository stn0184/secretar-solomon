"""Повторяющиеся задачи в сервисах бота: запись, ответ, правка, «сделал», «Вернуть», тик.

База, модель и расписание подменены (`techspec/13-repeat.md`): следующий раз
приходит от `FakeNext`, как от `repeat_next`, — само правило считает база, и
его проверяют тесты PGlite (`supabase/tests/repeat.test.ts`). Здесь — что бот
спрашивает у базы, что пишет и какими словами отвечает.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from solomon import texts
from solomon.db.reminders import Planned
from solomon.db.rpc import DatabaseError
from solomon.db.tasks import OpenQuestion, StoredMessage, TaskDetails
from solomon.services.reminders import Completion, ReminderService
from solomon.services.repeat import occurrence_seconds
from solomon.services.tasks import (
    Button,
    PressOutcome,
    RecordOutcome,
    TaskService,
    amendment,
)
from solomon.services.understanding import NotUnderstood, Understanding
from tests.conftest import (
    OWNER_ID,
    OWNER_TIMEZONE,
    FakeAnalyst,
    FakeEdits,
    FakeMessages,
    FakeNext,
    FakePlanner,
    FakeQuestions,
    FakeTranscriber,
    FakeUnderstandings,
    load_audio,
    make_details,
    make_settings,
    make_understanding,
)
from tests.test_chat_edit_service import edited, saved, saved_edit
from tests.test_reminders import (
    FRIDAY_END_OF_DAY,
    MONDAY_MORNING,
    NEXT_FRIDAY,
    FakeAnnouncer,
    FakeClearMoved,
    FakeCloser,
    FakeDue,
    FakeMarks,
    FakeMoved,
    FakeNotifier,
    make_due,
    make_moved,
)

SETTINGS = make_settings()
TZ = ZoneInfo(OWNER_TIMEZONE)
# Вторник, 29 сентября 2026, утро.
NOW = datetime(2026, 9, 29, 10, 0, tzinfo=TZ)
MESSAGE_ID = 41

# Разы серии «каждый понедельник»: вчерашний (просрочен), ближайший и следующий.
PAST_MONDAY = datetime(2026, 9, 28, 18, 0, tzinfo=TZ)
MONDAY = datetime(2026, 10, 5, 18, 0, tzinfo=TZ)
NEXT_MONDAY = datetime(2026, 10, 12, 18, 0, tzinfo=TZ)
TUESDAY = datetime(2026, 10, 6, 18, 0, tzinfo=TZ)
# База отдаёт моменты в UTC — бот называет их по часам владельца.
MONDAY_UTC = MONDAY.astimezone(UTC)
NEXT_MONDAY_UTC = NEXT_MONDAY.astimezone(UTC)

# Правило от модели (§5.3) и оно же в базе — с часом серии (§13.2).
MONDAYS = {"every": "week", "interval": 1, "weekdays": [1], "month_day": None, "month": None}
TUESDAYS = {"every": "week", "interval": 1, "weekdays": [2], "month_day": None, "month": None}
WORKDAYS = {
    "every": "week",
    "interval": 1,
    "weekdays": [1, 2, 3, 4, 5],
    "month_day": None,
    "month": None,
}
WEEKLY = {**MONDAYS, "time": None}
# «Каждые две недели» без дней: у недель дни обязательны (§13.2).
NO_DAYS = {"every": "week", "interval": 2, "weekdays": [], "month_day": None, "month": None}

REPORT_ID = "2e2a9b4f-8d66-4c9f-8b1a-5f7c3d2e1b22"
LAMP_ID = "3f3bac50-9e77-4da0-9c2b-6a8d4e3f2c33"
CALL_ID = "4a4cbd61-af88-4eb1-8d3c-7b9e5f4a3d44"

REPORT = make_details(
    id=REPORT_ID,
    title="отправить отчёт",
    due_at=PAST_MONDAY,
    due_precision="day",
    repeat=WEEKLY,
    occurrence_at=PAST_MONDAY,
    created_at=datetime(2026, 9, 21, 9, 0, tzinfo=TZ),
)
LAMP = make_details(id=LAMP_ID, created_at=datetime(2026, 9, 22, 9, 0, tzinfo=TZ))
# Разовая задача со сроком: ей ставят правило (§13.5).
CALL = make_details(
    id=CALL_ID,
    title="позвонить маме",
    due_at=datetime(2026, 10, 2, 18, 0, tzinfo=TZ),
    due_precision="day",
    created_at=datetime(2026, 9, 23, 9, 0, tzinfo=TZ),
)
# Список в промпте: 1 — отчёт (срок раньше), 2 — звонок, 3 — лампочка.
OPEN = [LAMP, CALL, REPORT]

MONDAY_PLAN = [
    Planned(stage="before", fire_at=datetime(2026, 10, 4, 18, 0, tzinfo=TZ)),
    Planned(stage="due", fire_at=datetime(2026, 10, 5, 9, 0, tzinfo=TZ)),
]
MONDAY_REMIND = "Напомню: 4 октября в 18:00"


def seconds(moment: datetime) -> int:
    return occurrence_seconds(moment)


@dataclass
class Rig:
    """Сервис и всё, что он трогает: тест смотрит в нужное."""

    service: TaskService
    analyst: FakeAnalyst
    understandings: FakeUnderstandings
    planner: FakePlanner
    store: FakeEdits
    following: FakeNext


def build(
    verdict: Understanding | NotUnderstood,
    store: FakeEdits | None = None,
    *,
    following: FakeNext | None = None,
    planner: FakePlanner | None = None,
    questions: FakeQuestions | None = None,
    understandings: FakeUnderstandings | None = None,
) -> Rig:
    """Сервис на подменённых базе, модели, расписании и `repeat_next`; «сейчас» — `NOW`."""
    analyst = FakeAnalyst(verdict)
    recorder = understandings or FakeUnderstandings()
    plan = planner or FakePlanner()
    edits = store if store is not None else FakeEdits(OPEN)
    next_time = following or FakeNext()
    service = TaskService(
        settings=SETTINGS,
        record_message=FakeMessages(),
        record_understanding=recorder,
        analyst=analyst,
        transcriber=FakeTranscriber(),
        planner=plan,
        clock=lambda: NOW,
        open_question=questions,
        edit_store=edits,
        repeat_next=next_time,
    )
    return Rig(service, analyst, recorder, plan, edits, next_time)


async def say(rig: Rig, text: str = "отчёт отправил") -> RecordOutcome:
    return await rig.service.record_from_message(
        chat_id=OWNER_ID, telegram_message_id=MESSAGE_ID, text=text
    )


def weekly_report(**fields: Any) -> Understanding:
    """«Каждый понедельник отправлять отчёт»: срок — ближайший понедельник."""
    base: dict[str, Any] = {
        "title": "отправить отчёт",
        "due_at": MONDAY,
        "due_precision": "day",
        "repeat": MONDAYS,
    }
    return make_understanding(**{**base, **fields})


# ------------------------------------------------------------------ запись


async def test_rule_is_recorded_with_the_task_and_retold() -> None:
    """Одна задача с правилом; «Повтор» — после головы, перед «Срок:» (§13.7)."""
    rig = build(weekly_report(), FakeEdits(), planner=FakePlanner(MONDAY_PLAN))

    outcome = await say(rig, "каждый понедельник отправлять отчёт")

    assert outcome.message == (
        "Записал: отправить отчёт. Повтор: каждый понедельник. "
        f"Срок: понедельник, 5 октября. {MONDAY_REMIND}"
    )
    task = saved(rig.understandings, "task")
    assert task["due_at"] == MONDAY.isoformat()
    # Час серии модель не отдаёт: его ставит база из срока (§13.2).
    assert task["repeat"] == MONDAYS
    assert "time" not in task["repeat"]
    assert task["needs_review"] is False
    assert saved(rig.understandings, "reminders") == [item.as_row() for item in MONDAY_PLAN]


async def test_rule_with_an_hour_leaves_the_hour_to_the_database() -> None:
    """«По будням в 9 планёрка»: срок с часом, правило — без него."""
    wednesday_nine = datetime(2026, 9, 30, 9, 0, tzinfo=TZ)
    rig = build(
        make_understanding(
            title="планёрка", due_at=wednesday_nine, due_precision="time", repeat=WORKDAYS
        ),
        FakeEdits(),
    )

    outcome = await say(rig, "по будням в 9 планёрка")

    assert (
        outcome.message == "Записал: планёрка. Повтор: по будням. Срок: среда, 30 сентября, 09:00"
    )
    task = saved(rig.understandings, "task")
    assert task["due_precision"] == "time"
    assert task["repeat"] == WORKDAYS


async def test_rule_by_voice_is_recorded_as_by_text() -> None:
    rig = build(weekly_report(), FakeEdits(), planner=FakePlanner(MONDAY_PLAN))

    outcome = await rig.service.record_from_voice(
        chat_id=OWNER_ID,
        telegram_message_id=MESSAGE_ID,
        kind="voice",
        file_id="voice-1",
        duration=5,
        load_audio=load_audio,
    )

    assert outcome.message.startswith("Записал: отправить отчёт. Повтор: каждый понедельник.")
    assert saved(rig.understandings, "task")["repeat"] == MONDAYS


@pytest.mark.parametrize(
    "fields",
    [
        {"kind": "idea", "title": "курс по гончарке"},
        {"kind": "wish", "title": "съездить на Байкал"},
        {"due_at": None, "due_precision": None},
    ],
    ids=["idea", "wish", "no-due"],
)
async def test_rule_of_an_idea_or_of_a_task_without_a_due_is_dropped(
    fields: dict[str, Any],
) -> None:
    """Повторять нечего — правило отбрасывается молча, без пометки (§13.1)."""
    rig = build(weekly_report(**fields), FakeEdits())

    outcome = await say(rig)

    assert outcome.ok
    assert "Повтор" not in outcome.message
    task = saved(rig.understandings, "task")
    assert task["repeat"] is None
    assert task["needs_review"] is False


async def test_rule_out_of_form_gives_a_one_off_task_with_a_mark() -> None:
    """Правило не по форме §13.2 — задача разовая, пометка и причина (инвариант 5)."""
    rig = build(weekly_report(repeat=NO_DAYS), FakeEdits(), planner=FakePlanner(MONDAY_PLAN))

    outcome = await say(rig)

    assert outcome.ok
    assert outcome.message == (
        f"Записал: отправить отчёт. Срок: понедельник, 5 октября. {MONDAY_REMIND}. "
        "Не разобрал повтор — записал разовой"
    )
    task = saved(rig.understandings, "task")
    assert task["repeat"] is None
    assert task["needs_review"] is True


async def test_rule_out_of_form_keeps_the_reason_of_the_model_first() -> None:
    rig = build(
        weekly_report(repeat=NO_DAYS, needs_review=True, review_reason="Не понял, какой отчёт."),
        FakeEdits(),
    )

    outcome = await say(rig)

    assert outcome.message.endswith("Не понял, какой отчёт. Не разобрал повтор — записал разовой")


async def test_rule_out_of_form_is_said_before_the_question() -> None:
    rig = build(
        weekly_report(repeat=NO_DAYS, needs_review=True, question="К какому часу?"), FakeEdits()
    )

    outcome = await say(rig)

    assert outcome.message == (
        "Записал: отправить отчёт. Срок: понедельник, 5 октября. "
        "Не разобрал повтор — записал разовой. К какому часу?"
    )
    task = saved(rig.understandings, "task")
    assert task["open_question"] == "К какому часу?"
    assert task["repeat"] is None


# ---------------------------------------------------- вопрос и ответ (§10.2)

RENT = "платить за квартиру"
MONTHLY_ASKED = OpenQuestion(
    task_id="0e2f",
    question="Какого числа каждый месяц?",
    title=RENT,
    kind="task",
    due_at=None,
    due_precision=None,
    priority="normal",
    promise=None,
    people=(),
    asked_at=NOW - timedelta(minutes=5),
)
TENTH = datetime(2026, 10, 10, 18, 0, tzinfo=TZ)
MONTHLY_MODEL = {"every": "month", "interval": 1, "weekdays": [], "month_day": 10, "month": None}
MONTHLY = {"every": "month", "interval": 1, "weekdays": None, "month_day": 10, "month": None}
TENTH_PLAN = [Planned(stage="before", fire_at=datetime(2026, 10, 9, 18, 0, tzinfo=TZ))]


async def test_rule_without_a_first_time_is_asked_about() -> None:
    """«Каждый месяц платить за квартиру»: без срока — без правила, вопрос о числе."""
    rig = build(
        make_understanding(
            title=RENT,
            repeat={**MONTHLY_MODEL, "month_day": None},
            needs_review=True,
            question="Какого числа каждый месяц?",
        ),
        FakeEdits(),
    )

    outcome = await say(rig, "каждый месяц платить за квартиру")

    assert outcome.message == f"Записал: {RENT}. Какого числа каждый месяц?"
    task = saved(rig.understandings, "task")
    assert task["repeat"] is None
    assert task["due_at"] is None
    assert task["open_question"] == "Какого числа каждый месяц?"


async def test_answer_gives_the_task_its_due_and_its_rule() -> None:
    """«Десятого» — срок и правило одной поправкой, «Понял» с повтором."""
    rig = build(
        make_understanding(
            title=RENT,
            answers_question=True,
            due_at=TENTH,
            due_precision="day",
            repeat=MONTHLY_MODEL,
        ),
        FakeEdits(),
        planner=FakePlanner(TENTH_PLAN),
        questions=FakeQuestions(MONTHLY_ASKED),
    )

    outcome = await say(rig, "десятого")

    assert outcome.message == (
        f"Понял: {RENT}. Повтор: каждый месяц 10-го. Срок: суббота, 10 октября. "
        "Напомню: 9 октября в 18:00"
    )
    assert saved(rig.understandings, "task") is None
    assert saved(rig.understandings, "amend") == {
        "task_id": "0e2f",
        "fields": {
            "due_at": TENTH.isoformat(),
            "due_precision": "day",
            "repeat": MONTHLY,
            "needs_review": False,
        },
        "reminders": [item.as_row() for item in TENTH_PLAN],
    }


def test_answer_with_the_same_rule_and_due_does_not_send_the_rule() -> None:
    """То же правило при том же сроке — расписание не трогается."""
    asked = replace(
        MONTHLY_ASKED, due_at=TENTH, due_precision="day", repeat={**MONTHLY, "time": None}
    )

    changed = amendment(
        asked,
        make_understanding(
            title=RENT,
            answers_question=True,
            due_at=TENTH,
            due_precision="day",
            repeat=MONTHLY_MODEL,
            priority="high",
        ),
    )

    assert "repeat" not in changed.fields
    assert changed.repeat == asked.repeat


def test_answer_with_a_rule_out_of_form_marks_the_task() -> None:
    changed = amendment(
        MONTHLY_ASKED,
        make_understanding(
            title=RENT,
            answers_question=True,
            due_at=TENTH,
            due_precision="day",
            repeat={**MONTHLY_MODEL, "month_day": 40},
        ),
    )

    assert "repeat" not in changed.fields
    assert changed.fields["needs_review"] is True
    assert changed.rule.malformed
    assert changed.repeat is None


# ------------------------------------------------- «сделал» и пропуск словом


def advanced_edit(action: str, occurrence: datetime, next_at: datetime) -> dict[str, Any]:
    """Что уходит в `edit` при переходе на следующий раз (§13.3)."""
    return {
        "task_id": REPORT_ID,
        "action": action,
        "changes": {},
        "schedule": [item.as_row() for item in MONDAY_PLAN],
        "question": None,
        "occurrence": seconds(occurrence),
        "next_at": next_at.isoformat(),
    }


async def test_done_moves_a_repeating_task_to_its_next_time() -> None:
    """Следующий раз и его план — у базы заранее; ответ называет записанное."""
    rig = build(
        edited(1, action="done"), following=FakeNext(MONDAY_UTC), planner=FakePlanner(MONDAY_PLAN)
    )

    outcome = await say(rig)

    # Раз вчера: следующий — строго позже «сейчас» (§13.3).
    assert rig.following.calls == [{"repeat": WEEKLY, "occurrence_at": PAST_MONDAY, "after": NOW}]
    assert rig.planner.calls == [
        {"due_at": MONDAY_UTC, "due_precision": "day", "kind": "task", "now": NOW}
    ]
    assert saved_edit(rig.understandings) == advanced_edit("done", PAST_MONDAY, MONDAY_UTC)
    assert outcome.message == (
        f"Отметил: отправить отчёт. Следующий раз: понедельник, 5 октября. {MONDAY_REMIND}"
    )
    back = f"back:{REPORT_ID}:{seconds(PAST_MONDAY)}:{seconds(MONDAY)}"
    assert outcome.buttons == (Button(text=texts.REOPEN_BUTTON, data=back),)


async def test_skip_moves_a_repeating_task_too() -> None:
    rig = build(
        edited(1, action="skip"), following=FakeNext(MONDAY_UTC), planner=FakePlanner(MONDAY_PLAN)
    )

    outcome = await say(rig, "в этот раз не надо")

    assert saved_edit(rig.understandings) == advanced_edit("skip", PAST_MONDAY, MONDAY_UTC)
    assert outcome.message == (
        f"Пропускаю этот раз: отправить отчёт. Следующий раз: понедельник, 5 октября. "
        f"{MONDAY_REMIND}"
    )
    assert [button.data for button in outcome.buttons] == [
        f"back:{REPORT_ID}:{seconds(PAST_MONDAY)}:{seconds(MONDAY)}"
    ]


async def test_done_ahead_of_time_counts_from_the_time_itself() -> None:
    """Раз ещё впереди — следующий считается от него, а не от «сейчас»."""
    ahead = replace(REPORT, due_at=MONDAY, occurrence_at=MONDAY)
    rig = build(
        edited(1, action="done"),
        FakeEdits([ahead]),
        following=FakeNext(NEXT_MONDAY_UTC),
    )

    outcome = await say(rig)

    assert rig.following.calls[0]["after"] == MONDAY
    assert outcome.message == "Отметил: отправить отчёт. Следующий раз: понедельник, 12 октября"


async def test_moved_time_counts_from_the_time_not_from_the_due() -> None:
    """Раз перенесли на среду — следующий всё равно считается от понедельника."""
    moved = replace(REPORT, due_at=datetime(2026, 9, 30, 18, 0, tzinfo=TZ))
    rig = build(edited(1, action="done"), FakeEdits([moved]), following=FakeNext(MONDAY_UTC))

    await say(rig)

    assert rig.following.calls[0]["occurrence_at"] == PAST_MONDAY
    assert saved_edit(rig.understandings)["occurrence"] == seconds(PAST_MONDAY)


async def test_next_time_unknown_means_nothing_is_recorded() -> None:
    """База не дала следующий раз — «Не смог записать», а не «Отметил» (инвариант 4)."""
    rig = build(edited(1, action="done"), following=FakeNext(broken=True))

    outcome = await say(rig)

    assert outcome == RecordOutcome(ok=False, message=texts.NOT_SAVED)
    assert rig.understandings.calls == []


async def test_time_gone_during_the_parse_is_a_failed_record() -> None:
    """Задача ушла на другой раз, пока думала модель: база отвергла запись целиком."""
    rig = build(
        edited(1, action="done"),
        following=FakeNext(MONDAY_UTC),
        understandings=FakeUnderstandings(broken=True),
    )

    outcome = await say(rig)

    assert outcome == RecordOutcome(ok=False, message=texts.NOT_SAVED)


async def test_skip_of_a_one_off_task_removes_it() -> None:
    """Пропуск разовой — как «убрать»; в базу уходит пропуском (§13.3)."""
    rig = build(edited(3, action="skip"))

    outcome = await say(rig, "лампочку в этот раз не надо")

    assert outcome.message == "Убрал из списка: купить лампочку."
    assert outcome.buttons == (Button(text=texts.REOPEN_BUTTON, data=f"reopen:{LAMP_ID}"),)
    assert saved_edit(rig.understandings)["action"] == "skip"
    assert rig.following.calls == []


async def test_cancel_of_a_repeating_task_removes_the_whole_series() -> None:
    rig = build(edited(1, action="cancel"))

    outcome = await say(rig, "отчёт больше не нужен совсем")

    assert outcome.message == "Убрал из списка со всеми повторами: отправить отчёт."
    assert outcome.buttons == (Button(text=texts.REOPEN_BUTTON, data=f"reopen:{REPORT_ID}"),)
    assert saved_edit(rig.understandings) == {
        "task_id": REPORT_ID,
        "action": "cancel",
        "changes": {},
        "schedule": [],
        "question": None,
    }
    assert rig.following.calls == []


# ----------------------------------------------------- правка раза и правила

WEDNESDAY = datetime(2026, 9, 30, 18, 0, tzinfo=TZ)
WEDNESDAY_PLAN = [Planned(stage="due", fire_at=datetime(2026, 9, 30, 9, 0, tzinfo=TZ))]


async def test_moving_the_time_keeps_the_rule() -> None:
    """Перенос раза: меняется только срок, правило остаётся и звучит (§13.5)."""
    rig = build(
        edited(1, due_at=WEDNESDAY, due_precision="day"), planner=FakePlanner(WEDNESDAY_PLAN)
    )

    outcome = await say(rig, "отчёт перенеси на среду")

    assert saved_edit(rig.understandings)["changes"] == {"due_date": "2026-09-30"}
    assert outcome.message == (
        "Перенёс: отправить отчёт. Повтор: каждый понедельник. Срок: среда, 30 сентября. "
        "Напомню: 30 сентября в 09:00"
    )


async def test_new_rule_goes_with_its_first_time() -> None:
    """«Теперь по вторникам»: правило и срок ближайшего вторника — одной правкой."""
    tuesday_plan = [Planned(stage="before", fire_at=datetime(2026, 10, 5, 18, 0, tzinfo=TZ))]
    rig = build(
        edited(1, due_at=TUESDAY, due_precision="day", repeat=TUESDAYS),
        planner=FakePlanner(tuesday_plan),
    )

    outcome = await say(rig, "отчёт теперь по вторникам")

    assert saved_edit(rig.understandings)["changes"] == {
        "due_date": "2026-10-06",
        "repeat": TUESDAYS,
    }
    assert outcome.message == (
        "Поправил: отправить отчёт. Повтор: каждый вторник. Срок: вторник, 6 октября. "
        "Напомню: 5 октября в 18:00"
    )


async def test_new_hour_sends_the_same_rule_again() -> None:
    """«Теперь в 11»: то же правило уходит со сроком — база возьмёт новый час серии."""
    nine = datetime(2026, 9, 30, 9, 0, tzinfo=TZ)
    standup = make_details(
        id=CALL_ID,
        title="планёрка",
        due_at=nine,
        due_precision="time",
        repeat={**WORKDAYS, "time": "09:00"},
        occurrence_at=nine,
    )
    eleven = nine.replace(hour=11)
    rig = build(
        edited(1, due_at=eleven, due_precision="time", repeat=WORKDAYS), FakeEdits([standup])
    )

    outcome = await say(rig, "планёрка теперь в 11")

    assert saved_edit(rig.understandings)["changes"] == {
        "due_at": eleven.isoformat(),
        "repeat": WORKDAYS,
    }
    assert outcome.message == (
        "Поправил: планёрка. Повтор: по будням. Срок: среда, 30 сентября, 11:00"
    )


async def test_rule_for_a_one_off_task_starts_from_its_due() -> None:
    """Правило разовой задаче со сроком: срок — первый раз, план не пересчитывается."""
    rig = build(edited(2, repeat=MONDAYS))

    outcome = await say(rig, "маме звонить каждый понедельник")

    assert saved_edit(rig.understandings)["changes"] == {"repeat": MONDAYS}
    assert saved_edit(rig.understandings)["schedule"] == []
    assert rig.planner.calls == []
    assert outcome.message == (
        "Поправил: позвонить маме. Повтор: каждый понедельник. Срок: пятница, 2 октября"
    )


async def test_rule_for_a_task_without_a_due_asks_where_to_start() -> None:
    """Первого раза нет и он не назван — вопрос §12.3, ничего не меняется."""
    rig = build(edited(3, repeat=MONDAYS))

    outcome = await say(rig, "лампочку каждый понедельник")

    assert outcome.message == (
        "Не понял, как поправить: купить лампочку. С какого дня начать повтор?"
    )
    assert saved_edit(rig.understandings) == {
        "task_id": LAMP_ID,
        "action": "change",
        "changes": {},
        "schedule": [],
        "question": texts.REPEAT_START,
    }


async def test_removed_rule_leaves_a_one_off_task() -> None:
    rig = build(edited(1, repeat_removed=True))

    outcome = await say(rig, "отчёт больше не повторяй")

    assert saved_edit(rig.understandings)["changes"] == {"repeat": None}
    assert rig.planner.calls == []
    assert outcome.message == "Больше не повторяю: отправить отчёт. Срок: понедельник, 28 сентября"


async def test_removed_due_removes_the_rule_too() -> None:
    """Срок снят — снято и правило; ключ `repeat` не нужен, базе хватит срока."""
    rig = build(edited(1, due_removed=True))

    outcome = await say(rig, "у отчёта нет срока")

    assert saved_edit(rig.understandings)["changes"] == {"due_at": None}
    assert outcome.message == "Убрал срок и повтор: отправить отчёт. Напоминать не буду."


async def test_rule_out_of_form_in_an_edit_is_dropped_the_rest_goes() -> None:
    rig = build(edited(1, repeat=NO_DAYS, priority="high"))

    outcome = await say(rig, "отчёт срочный и через раз")

    assert saved_edit(rig.understandings)["changes"] == {"priority": "high"}
    assert outcome.message.startswith("Поправил: отправить отчёт. Повтор: каждый понедельник.")


# ------------------------------------------------------ кнопка кандидата


def done_candidates() -> StoredMessage:
    verdict = edited(None, action="done", candidates=[1, 2])
    return StoredMessage(
        id="9a71",
        text="сделал",
        task_id=None,
        analysis=verdict.model_dump(mode="json"),
        reply=texts.PICK_DONE,
    )


async def test_pick_of_a_repeating_task_moves_it_to_the_next_time() -> None:
    store = FakeEdits(OPEN, messages={MESSAGE_ID: done_candidates()})
    rig = build(
        make_understanding(),
        store,
        following=FakeNext(MONDAY_UTC),
        planner=FakePlanner(MONDAY_PLAN),
    )

    outcome = await rig.service.pick(
        chat_id=OWNER_ID, telegram_message_id=MESSAGE_ID, task_id=REPORT_ID
    )

    back = f"back:{REPORT_ID}:{seconds(PAST_MONDAY)}:{seconds(MONDAY)}"
    assert outcome == PressOutcome(
        message=(
            f"Отметил: отправить отчёт. Следующий раз: понедельник, 5 октября. {MONDAY_REMIND}"
        ),
        replace=True,
        buttons=(Button(text=texts.REOPEN_BUTTON, data=back),),
    )
    assert store.picks[0][1] == advanced_edit("done", PAST_MONDAY, MONDAY_UTC)
    assert store.tasks[REPORT_ID].occurrence_at == MONDAY_UTC
    assert store.tasks[REPORT_ID].status == "active"


class StaleEdits(FakeEdits):
    """Задача ушла на следующий раз между чтением и записью нажатия."""

    def __init__(self, stale: TaskDetails, current: TaskDetails, **kwargs: Any) -> None:
        super().__init__([current], **kwargs)
        self.stale = stale

    async def task(self, task_id: str) -> TaskDetails | None:
        await super().task(task_id)
        return self.stale


async def test_pick_after_the_task_moved_on_changes_nothing() -> None:
    current = replace(REPORT, due_at=MONDAY_UTC, occurrence_at=MONDAY_UTC)
    store = StaleEdits(REPORT, current, messages={MESSAGE_ID: done_candidates()})
    rig = build(make_understanding(), store, following=FakeNext(MONDAY_UTC))

    outcome = await rig.service.pick(
        chat_id=OWNER_ID, telegram_message_id=MESSAGE_ID, task_id=REPORT_ID
    )

    assert outcome == PressOutcome(message=texts.PICKED_GONE, replace=False)
    assert store.picks == []
    assert store.tasks[REPORT_ID] == current


# ----------------------------------------------------------------- «Вернуть»

AHEAD_DONE = replace(REPORT, due_at=NEXT_MONDAY_UTC, occurrence_at=NEXT_MONDAY_UTC)


async def back(rig: Rig, task_id: str = REPORT_ID) -> PressOutcome:
    """«Вернуть» под «Отметил»: с понедельника 5-го задача ушла на 12-е."""
    return await rig.service.back(
        task_id=task_id, back_to=seconds(MONDAY), moved_from=seconds(NEXT_MONDAY)
    )


async def test_back_returns_the_task_to_its_time() -> None:
    """Прежний раз — в час и точность серии, план заново на момент нажатия."""
    store = FakeEdits([AHEAD_DONE])
    rig = build(make_understanding(), store, planner=FakePlanner(MONDAY_PLAN))

    outcome = await back(rig)

    assert outcome == PressOutcome(
        message=(
            "Вернул в работу: отправить отчёт. Повтор: каждый понедельник. "
            f"Срок: понедельник, 5 октября. {MONDAY_REMIND}"
        ),
        replace=True,
    )
    assert rig.planner.calls == [
        {"due_at": MONDAY, "due_precision": "day", "kind": "task", "now": NOW}
    ]
    assert store.returns == [(REPORT_ID, seconds(MONDAY), seconds(NEXT_MONDAY), MONDAY_PLAN)]
    assert store.tasks[REPORT_ID].occurrence_at == MONDAY


async def test_second_back_says_the_same_and_writes_nothing() -> None:
    """Задача уже на прежнем разе — тот же ответ, без записи (как у `reopen`)."""
    returned = replace(REPORT, due_at=MONDAY_UTC, occurrence_at=MONDAY_UTC)
    store = FakeEdits([returned])
    rig = build(make_understanding(), store)

    outcome = await back(rig)

    assert outcome.replace
    assert outcome.message.startswith(
        "Вернул в работу: отправить отчёт. Повтор: каждый понедельник."
    )
    assert store.returns == []


async def test_back_after_the_task_went_further_changes_nothing() -> None:
    further = replace(REPORT, due_at=NEXT_MONDAY_UTC + timedelta(days=7))
    further = replace(further, occurrence_at=further.due_at)
    store = FakeEdits([further])
    rig = build(make_understanding(), store)

    outcome = await back(rig)

    assert outcome == PressOutcome(message=texts.GONE_FURTHER, replace=False)
    assert store.returns == []
    assert store.tasks[REPORT_ID] == further


async def test_back_of_a_task_that_became_one_off_changes_nothing() -> None:
    one_off = replace(AHEAD_DONE, repeat=None)
    rig = build(make_understanding(), FakeEdits([one_off]))

    outcome = await back(rig)

    assert outcome == PressOutcome(message=texts.GONE_FURTHER, replace=False)
    assert rig.planner.calls == []


async def test_back_of_a_deleted_task_says_so() -> None:
    rig = build(make_understanding(), FakeEdits([LAMP]))

    assert await back(rig) == PressOutcome(message=texts.DONE_UNKNOWN, replace=False)


async def test_back_refused_by_the_database_keeps_the_button() -> None:
    rig = build(make_understanding(), FakeEdits([AHEAD_DONE], broken={"return_occurrence"}))

    assert await back(rig) == PressOutcome(message=texts.NOT_REOPENED, replace=False)


async def test_back_without_a_database_says_it_did_not_return() -> None:
    rig = build(make_understanding())
    service = TaskService(
        settings=SETTINGS,
        record_message=FakeMessages(),
        record_understanding=FakeUnderstandings(),
        analyst=rig.analyst,
        transcriber=FakeTranscriber(),
        planner=FakePlanner(),
        clock=lambda: NOW,
    )

    outcome = await service.back(task_id=REPORT_ID, back_to=1, moved_from=2)

    assert outcome == PressOutcome(message=texts.NOT_REOPENED, replace=False)


async def test_reopened_series_names_its_rule() -> None:
    """«Вернуть» после «Убрал со всеми повторами»: серия снова в работе."""
    store = FakeEdits([replace(REPORT, status="cancelled")])
    rig = build(make_understanding(), store)

    outcome = await rig.service.reopen(task_id=REPORT_ID)

    assert outcome.message == (
        "Вернул в работу: отправить отчёт. Повтор: каждый понедельник. "
        "Срок: понедельник, 28 сентября"
    )


# ------------------------------------------------------ напоминания и тик

FRIDAYS = {
    "every": "week",
    "interval": 1,
    "weekdays": [5],
    "month_day": None,
    "month": None,
    "time": None,
}


class FakeRoller:
    """`roll_repeats` без базы: когда звали и сколько задач «перекатилось»."""

    def __init__(self, rolled: int = 0, broken: bool = False, events: list[str] | None = None):
        self.rolled = rolled
        self.broken = broken
        self.calls: list[tuple[int, datetime]] = []
        self.events = events if events is not None else []

    async def __call__(self, *, owner_telegram_id: int, now: datetime) -> int:
        self.calls.append((owner_telegram_id, now))
        self.events.append("roll")
        if self.broken:
            raise DatabaseError("ConnectTimeout: timed out")
        return self.rolled


def ticker(
    *,
    due: FakeDue | None = None,
    notifier: FakeNotifier | None = None,
    roller: FakeRoller | None = None,
    closer: FakeCloser | None = None,
    moved: FakeMoved | None = None,
    announcer: FakeAnnouncer | None = None,
) -> ReminderService:
    """Сервис напоминаний с перекатыванием на подделках."""
    return ReminderService(
        settings=SETTINGS,
        due=due or FakeDue(),
        mark_sent=FakeMarks(),
        close_task=closer or FakeCloser(),
        notify=notifier or FakeNotifier(),
        moved=moved or FakeMoved(),
        clear_moved=FakeClearMoved(),
        announce=announcer or FakeAnnouncer(),
        clock=lambda: FRIDAY_END_OF_DAY,
        roll=roller,
    )


async def test_tick_rolls_the_repeats_before_the_ripe_reminders() -> None:
    """Новый раз получает ступени до выборки — и созревшая уходит этим же тиком."""
    events: list[str] = []
    roller = FakeRoller(rolled=1, events=events)
    notifier = FakeNotifier(events=events)
    service = ticker(
        due=FakeDue([make_due("due", FRIDAY_END_OF_DAY)]), notifier=notifier, roller=roller
    )

    assert await service.tick() == 1
    assert roller.calls == [(OWNER_ID, FRIDAY_END_OF_DAY)]
    assert events == ["roll", "reminder"]


async def test_failed_roll_is_logged_and_the_reminders_still_go(
    caplog: pytest.LogCaptureFixture,
) -> None:
    notifier = FakeNotifier()
    service = ticker(
        due=FakeDue([make_due("due", FRIDAY_END_OF_DAY)]),
        notifier=notifier,
        roller=FakeRoller(broken=True),
    )

    with caplog.at_level(logging.ERROR, logger="solomon.services.reminders"):
        assert await service.tick() == 1

    assert "Повторяющиеся задачи не перекатились" in caplog.text
    assert len(notifier.sent) == 1


async def test_reminder_of_a_repeating_task_carries_its_time() -> None:
    """Раз уезжает в кнопку «Сделано»; в тексте напоминания повтора нет (§13.7)."""
    notifier = FakeNotifier()
    ripe = [
        make_due("due", FRIDAY_END_OF_DAY, repeat=FRIDAYS, occurrence_at=FRIDAY_END_OF_DAY),
        make_due("due", FRIDAY_END_OF_DAY, task_id="7c31"),
    ]
    service = ticker(due=FakeDue(ripe), notifier=notifier)

    assert await service.tick() == 2
    assert notifier.occurrences == [seconds(FRIDAY_END_OF_DAY), None]
    assert [text for _, text in notifier.sent] == [
        "Напоминаю: отправить расчёт\nСрок: сегодня, 18:00",
        "Напоминаю: отправить расчёт\nСрок: сегодня, 18:00",
    ]


async def test_moved_line_of_a_repeating_task_names_the_rule() -> None:
    announcer = FakeAnnouncer()
    service = ticker(moved=FakeMoved([make_moved(repeat=FRIDAYS)]), announcer=announcer)

    assert await service.tick(MONDAY_MORNING) == 1
    assert announcer.sent == [
        "Перенёс: отправить расчёт клиенту. Повтор: каждую пятницу. Срок: пятница, 2 октября. "
        "Напомню: 2 октября в 09:00"
    ]


def repeating(**fields: Any) -> TaskDetails:
    """Повторяющаяся задача, какой её вернула база после «Сделано»: на следующем разе."""
    base: dict[str, Any] = {
        "id": "0e2f",
        "title": "отправить расчёт",
        "due_at": NEXT_FRIDAY.astimezone(UTC),
        "due_precision": "day",
        "repeat": FRIDAYS,
        "occurrence_at": NEXT_FRIDAY.astimezone(UTC),
    }
    return make_details(**{**base, **fields})


NEXT_WORDS = "пятница, 2 октября"


async def test_done_of_a_repeating_task_names_the_next_time() -> None:
    closer = FakeCloser(repeating())
    service = ticker(closer=closer)

    completion = await service.complete("0e2f", seconds(FRIDAY_END_OF_DAY))

    assert closer.occurrences == [seconds(FRIDAY_END_OF_DAY)]
    assert completion == Completion(
        ok=True,
        answer=f"Следующий раз: {NEXT_WORDS}",
        mark=f"✓ Сделано. Следующий раз: {NEXT_WORDS}",
    )


async def test_old_button_moves_the_current_time() -> None:
    """Кнопка до этапа 011 — без раза: база переводит текущий (§13.3)."""
    closer = FakeCloser(repeating())
    service = ticker(closer=closer)

    completion = await service.complete("0e2f")

    assert closer.occurrences == [None]
    assert completion.mark == f"✓ Сделано. Следующий раз: {NEXT_WORDS}"


async def test_done_of_a_one_off_task_closes_it_as_before() -> None:
    closer = FakeCloser(make_details(id="0e2f", title="отправить расчёт", status="done"))
    service = ticker(closer=closer)

    completion = await service.complete("0e2f")

    assert completion == Completion(ok=True, answer=texts.DONE_ANSWER, mark=texts.DONE_MARK)


def test_done_mark_replaces_the_previous_one() -> None:
    """Отметка одна: новая заменяет прежнюю, а не дописывается второй."""
    marked = f"Напоминаю: отправить расчёт\n\n✓ Сделано. Следующий раз: {NEXT_WORDS}"
    later = "✓ Сделано. Следующий раз: пятница, 9 октября"

    assert texts.done_message(marked, later) == f"Напоминаю: отправить расчёт\n\n{later}"
    assert texts.done_message("Напоминаю: отправить расчёт") == (
        "Напоминаю: отправить расчёт\n\n✓ Сделано"
    )


def test_help_tells_about_repeating_tasks() -> None:
    assert "повторяющейся" in texts.HELP
    assert "переводит на следующий раз" in texts.HELP
