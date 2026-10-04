"""Правка задачи словом: что уходит модели, что в базу и какими словами бот отвечает.

База, модель и расписание подменены (`techspec/12-chat-edit.md`): проверяется
операция — список задач и подсказки в промпте, ветки §12.3, ответы §12.5 и
кнопки §12.6, — а не сеть. Правила самой базы (что правка делает с задачей и
напоминаниями) — в тестах PGlite `supabase/tests/chat_edit.test.ts`.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from solomon import texts
from solomon.db.reminders import Planned
from solomon.db.tasks import (
    OpenQuestion,
    RecentMessage,
    SavedMessage,
    StoredMessage,
    TaskEvent,
)
from solomon.services.conversation import recent_block
from solomon.services.tasks import (
    Button,
    DatabaseEditStore,
    PressOutcome,
    Swipe,
    TaskService,
    edit_closes_asked,
)
from solomon.services.understanding import NotUnderstood, TaskEdit, Understanding
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
    load_audio,
    load_image,
    make_details,
    make_settings,
    make_understanding,
)
from tests.test_tasks_db import FakeClient, as_client

SETTINGS = make_settings()
TZ = ZoneInfo(OWNER_TIMEZONE)
# Вторник, 29 сентября 2026, утро.
NOW = datetime(2026, 9, 29, 10, 0, tzinfo=TZ)
MESSAGE_ID = 41

MEETING_ID = "1d1f8a3e-7c55-4b8e-9a0f-4e6b2c1d0a11"
REPORT_ID = "2e2a9b4f-8d66-4c9f-8b1a-5f7c3d2e1b22"
LAMP_ID = "3f3bac50-9e77-4da0-9c2b-6a8d4e3f2c33"

MEETING = make_details(
    id=MEETING_ID,
    title="встреча с Ренатой",
    due_at=datetime(2026, 10, 2, 17, 0, tzinfo=TZ),
    due_precision="time",
    priority="high",
    people=("Рената",),
    created_at=datetime(2026, 9, 20, 9, 0, tzinfo=TZ),
)
REPORT = make_details(
    id=REPORT_ID,
    title="отправить отчёт",
    due_at=datetime(2026, 10, 2, 18, 0, tzinfo=TZ),
    due_precision="day",
    people=("Кузнецов", "Петров"),
    created_at=datetime(2026, 9, 21, 9, 0, tzinfo=TZ),
)
LAMP = make_details(id=LAMP_ID, created_at=datetime(2026, 9, 22, 9, 0, tzinfo=TZ))
# Список в промпте: 1 — встреча, 2 — отчёт, 3 — лампочка (со сроком раньше).
OPEN = [LAMP, REPORT, MEETING]

TODAY_FIVE = "2026-09-29T17:00:00+05:00"
MONDAY = "2026-10-05T18:00:00+05:00"
MOVE_PLAN = [
    Planned(stage="before", fire_at=datetime(2026, 9, 29, 16, 0, tzinfo=TZ)),
    Planned(stage="due", fire_at=datetime(2026, 9, 29, 17, 0, tzinfo=TZ)),
]
MEETING_PLAN = [
    Planned(stage="before", fire_at=datetime(2026, 10, 2, 16, 0, tzinfo=TZ)),
    Planned(stage="due", fire_at=datetime(2026, 10, 2, 17, 0, tzinfo=TZ)),
]


def edit(**fields: Any) -> dict[str, Any]:
    """Правка модели (§5.3): только то, что важно тесту; пустое — «не менял»."""
    base: dict[str, Any] = {
        "action": "change",
        "task": None,
        "candidates": [],
        "title": None,
        "due_at": None,
        "due_precision": None,
        "due_removed": False,
        "time_removed": False,
        "repeat": None,
        "repeat_removed": False,
        "priority": None,
        "promise": None,
        "people": None,
    }
    return {**base, **fields}


def edited(task: int | None = None, **fields: Any) -> Understanding:
    """Разбор сообщения-правки: верхний уровень — как для нового поручения."""
    top: dict[str, Any] = {"title": "встреча"}
    for key in [key for key in fields if key.startswith("top_")]:
        top[key.removeprefix("top_")] = fields.pop(key)
    return make_understanding(edit=edit(task=task, **fields), **top)


def build(
    verdict: Understanding | NotUnderstood,
    store: FakeEdits | None = None,
    *,
    planner: FakePlanner | None = None,
    understandings: FakeUnderstandings | None = None,
    messages: FakeMessages | None = None,
    questions: FakeQuestions | None = None,
    wired: bool = True,
) -> tuple[TaskService, FakeAnalyst, FakeUnderstandings, FakePlanner, FakeEdits]:
    """Сервис на подменённых базе, модели и расписании; «сейчас» — `NOW`."""
    analyst = FakeAnalyst(verdict)
    edits = store if store is not None else FakeEdits(OPEN)
    recorder = understandings or FakeUnderstandings()
    plan = planner or FakePlanner()
    service = TaskService(
        settings=SETTINGS,
        record_message=messages or FakeMessages(),
        record_understanding=recorder,
        analyst=analyst,
        transcriber=FakeTranscriber(),
        planner=plan,
        clock=lambda: NOW,
        open_question=questions,
        edit_store=edits if wired else None,
    )
    return service, analyst, recorder, plan, edits


async def say(
    service: TaskService,
    text: str = "перенеси встречу",
    *,
    forwarded_from: str | None = None,
    swipe: Swipe | None = None,
) -> Any:
    return await service.record_from_message(
        chat_id=OWNER_ID,
        telegram_message_id=MESSAGE_ID,
        text=text,
        forwarded_from=forwarded_from,
        swipe=swipe,
    )


def saved(understandings: FakeUnderstandings, key: str) -> Any:
    """Аргумент первой записи разбора: `task`, `amend`, `edit`, `reminders`."""
    return understandings.calls[0][key]


def saved_edit(understandings: FakeUnderstandings) -> Any:
    return saved(understandings, "edit")


# ------------------------------------------------------------- промпт, блок 5


async def test_prompt_gets_the_open_tasks_in_number_order() -> None:
    """Блок 5 (§12.2): со сроком по возрастанию, потом без срока; номер — место."""
    service, analyst, _, _, store = build(make_understanding())

    await say(service)

    assert analyst.tasks == [[MEETING, REPORT, LAMP]]
    assert ("open_tasks", 50) in store.calls


async def test_without_open_tasks_the_prompt_gets_an_empty_list() -> None:
    """Задач нет — пустой список: «Открытых задач нет.» и те же правила."""
    service, analyst, _, _, _ = build(make_understanding(), FakeEdits())

    await say(service)

    assert analyst.tasks == [[]]


async def test_service_without_a_store_sees_an_empty_list() -> None:
    """Без хранилища правка не находит задачу, но разбор идёт."""
    service, analyst, understandings, _, _ = build(edited(1, action="done"), wired=False)

    outcome = await say(service, "сделал")

    assert analyst.tasks == [[]]
    assert saved_edit(understandings) is None
    assert outcome.message == texts.NOT_FOUND.format(title="встреча")


async def test_forwarded_message_gets_only_the_list_and_its_edit_is_dropped() -> None:
    """Пересланное (§12.2, §15.2): список — только для сверки дублей, без
    последней задачи и свайпа; `edit` отбрасывается, запись — новая."""
    service, analyst, understandings, _, store = build(
        edited(1, action="done", top_title="прислать смету")
    )

    outcome = await say(service, "пришлю смету", forwarded_from="Аня")

    assert analyst.tasks == [[MEETING, REPORT, LAMP]]
    assert analyst.last_tasks == [None]
    assert analyst.swipes == [None]
    assert store.calls == [("open_tasks", 50)]
    assert saved_edit(understandings) is None
    assert saved(understandings, "task")["title"] == "прислать смету"
    assert outcome.message.startswith("Записал: прислать смету")
    assert outcome.buttons == ()


async def test_failed_task_list_is_logged_and_the_edit_dropped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Сбой чтения списка: строка в журнал, разбор как нового, `edit` отброшен."""
    service, analyst, understandings, _, _ = build(
        edited(1, action="done", top_title="сделал"), FakeEdits(OPEN, broken={"open_tasks"})
    )

    with caplog.at_level(logging.ERROR, logger="solomon.services.tasks"):
        await say(service, "сделал")

    assert analyst.tasks == [None]
    assert saved_edit(understandings) is None
    assert saved(understandings, "task") is not None
    assert "Открытые задачи не прочитаны" in caplog.text


# ------------------------------------------------------- последняя задача


async def test_last_task_is_the_later_event_within_an_hour() -> None:
    """Напоминание о встрече позже сообщения об отчёте — последняя задача №1."""
    store = FakeEdits(
        OPEN,
        message_event=TaskEvent(task_id=REPORT_ID, at=NOW - timedelta(minutes=30)),
        reminder_event=TaskEvent(task_id=MEETING_ID, at=NOW - timedelta(minutes=10)),
    )
    service, analyst, _, _, _ = build(make_understanding(), store)

    await say(service)

    assert analyst.last_tasks == [1]
    assert ("last_message_event", NOW - timedelta(hours=1)) in store.calls


async def test_last_task_is_the_later_message_too() -> None:
    store = FakeEdits(
        OPEN,
        message_event=TaskEvent(task_id=REPORT_ID, at=NOW - timedelta(minutes=5)),
        reminder_event=TaskEvent(task_id=MEETING_ID, at=NOW - timedelta(minutes=10)),
    )
    service, analyst, _, _, _ = build(make_understanding(), store)

    await say(service)

    assert analyst.last_tasks == [2]


async def test_event_older_than_an_hour_names_no_last_task() -> None:
    store = FakeEdits(
        OPEN, reminder_event=TaskEvent(task_id=MEETING_ID, at=NOW - timedelta(minutes=61))
    )
    service, analyst, _, _, _ = build(make_understanding(), store)

    await say(service)

    assert analyst.last_tasks == [None]


async def test_closed_last_task_names_nothing() -> None:
    """Задачи последнего события нет в списке — строки нет, к раннему не откатываемся."""
    closed = replace(MEETING, status="done")
    store = FakeEdits(
        [LAMP, REPORT, closed],
        message_event=TaskEvent(task_id=REPORT_ID, at=NOW - timedelta(minutes=30)),
        reminder_event=TaskEvent(task_id=MEETING_ID, at=NOW - timedelta(minutes=10)),
    )
    service, analyst, _, _, _ = build(make_understanding(), store)

    await say(service)

    assert analyst.last_tasks == [None]


async def test_failed_last_task_read_keeps_the_list() -> None:
    store = FakeEdits(
        OPEN,
        message_event=TaskEvent(task_id=REPORT_ID, at=NOW - timedelta(minutes=5)),
        broken={"last_reminder_event"},
    )
    service, analyst, _, _, _ = build(make_understanding(), store)

    await say(service)

    assert analyst.last_tasks == [None]
    assert analyst.tasks == [[MEETING, REPORT, LAMP]]


# ------------------------------------------------------------------ свайп


async def test_swipe_on_a_reminder_names_its_task() -> None:
    store = FakeEdits(OPEN, reminders={40: REPORT_ID})
    service, analyst, _, _, _ = build(make_understanding(), store)

    await say(service, "сделал", swipe=Swipe(40, from_bot=True, text="Напоминаю: отчёт"))

    assert analyst.swipes == ["Ответ на напоминание о задаче №2"]


async def test_swipe_on_a_reminder_of_a_task_outside_the_list_quotes_it() -> None:
    store = FakeEdits(OPEN, reminders={40: "4a4cbd61-af88-4eb1-8d3c-7b9e5f4a3d44"})
    service, analyst, _, _, _ = build(make_understanding(), store)

    await say(service, "сделал", swipe=Swipe(40, from_bot=True, text="Напоминаю: забор"))

    assert analyst.swipes == ["Ответ на напоминание: «Напоминаю: забор»"]


async def test_swipe_on_another_bot_message_quotes_up_to_200_chars() -> None:
    service, analyst, _, _, _ = build(make_understanding())

    await say(service, "да", swipe=Swipe(40, from_bot=True, text="я" * 250))

    assert analyst.swipes == [f"Ответ на сообщение бота: «{'я' * 200}…»"]


async def test_swipe_on_own_message_with_a_task_names_it() -> None:
    stored = StoredMessage(
        id="9a70", text="встреча в пятницу", task_id=MEETING_ID, analysis=None, reply="Записал"
    )
    store = FakeEdits(OPEN, messages={39: stored})
    service, analyst, _, _, _ = build(make_understanding(), store)

    await say(service, "на пять", swipe=Swipe(39, from_bot=False, text="встреча в пятницу"))

    assert analyst.swipes == ["Ответ на своё сообщение о задаче №1"]
    assert ("message", OWNER_ID, 39) in store.calls


async def test_swipe_on_own_message_without_a_task_quotes_it() -> None:
    service, analyst, _, _, _ = build(make_understanding())

    await say(service, "нет", swipe=Swipe(39, from_bot=False, text="как дела"))

    assert analyst.swipes == ["Ответ на своё сообщение: «как дела»"]


async def test_swipe_on_own_voice_quotes_the_transcript() -> None:
    """У голосового в Telegram текста нет — расшифровка из базы."""
    stored = StoredMessage(
        id="9a70", text="купить молоко", task_id=None, analysis=None, reply="Записал"
    )
    service, analyst, _, _, _ = build(make_understanding(), FakeEdits(OPEN, messages={39: stored}))

    await say(service, "и хлеб", swipe=Swipe(39, from_bot=False, text=None))

    assert analyst.swipes == ["Ответ на своё сообщение: «купить молоко»"]


async def test_failed_swipe_read_drops_only_its_line() -> None:
    store = FakeEdits(OPEN, broken={"reminder_task"})
    service, analyst, _, _, _ = build(make_understanding(), store)

    await say(service, "сделал", swipe=Swipe(40, from_bot=True, text="Напоминаю: отчёт"))

    assert analyst.swipes == [None]
    assert analyst.tasks == [[MEETING, REPORT, LAMP]]


async def test_voice_edit_carries_the_swipe_and_closes_the_task() -> None:
    """Правка голосом — как текстом, со строкой свайпа (§12.2)."""
    store = FakeEdits(OPEN, reminders={40: MEETING_ID})
    service, analyst, understandings, _, _ = build(edited(1, action="done"), store)

    outcome = await service.record_from_voice(
        chat_id=OWNER_ID,
        telegram_message_id=MESSAGE_ID,
        kind="voice",
        file_id="voice-1",
        duration=3,
        load_audio=load_audio,
        swipe=Swipe(40, from_bot=True, text="Напоминаю: встреча с Ренатой"),
    )

    assert analyst.swipes == ["Ответ на напоминание о задаче №1"]
    assert saved_edit(understandings)["action"] == "done"
    assert outcome.message == "Закрыл: встреча с Ренатой."


# ------------------------------------------------------------- задача узнана


async def test_move_writes_the_due_and_the_plan_and_says_moved() -> None:
    """Перенос (§12.3–12.5): срок, план у базы, «Перенёс … Напомню» по записанному."""
    service, _, understandings, planner, _ = build(
        edited(1, due_at=TODAY_FIVE, due_precision="time"),
        planner=FakePlanner(MOVE_PLAN),
    )

    outcome = await say(service, "встреча перенеслась на пять")

    assert planner.calls == [
        {
            "due_at": datetime(2026, 9, 29, 17, 0, tzinfo=TZ),
            "due_precision": "time",
            "kind": "task",
            "now": NOW,
        }
    ]
    assert saved_edit(understandings) == {
        "task_id": MEETING_ID,
        "action": "change",
        "changes": {"due_at": TODAY_FIVE},
        "schedule": [item.as_row() for item in MOVE_PLAN],
        "question": None,
    }
    assert saved(understandings, "task") is None
    assert saved(understandings, "amend") is None
    assert outcome.ok
    assert outcome.message == (
        "Перенёс: встреча с Ренатой. Срок: вторник, 29 сентября, 17:00. Напомню: сегодня в 16:00"
    )
    assert outcome.buttons == ()


async def test_move_into_the_past_has_no_remind_line() -> None:
    """Срок в прошлом — план пуст, «Напомню» нет (§12.5)."""
    service, _, understandings, _, _ = build(
        edited(1, due_at="2026-09-29T08:00:00+05:00", due_precision="time")
    )

    outcome = await say(service, "встреча была в восемь")

    assert saved_edit(understandings)["schedule"] == []
    assert outcome.message == "Перенёс: встреча с Ренатой. Срок: вторник, 29 сентября, 08:00"


async def test_move_to_a_day_sends_the_date() -> None:
    """День уходит датой, 18:00 ставит база; план — по 18:00 того дня."""
    plan = [Planned(stage="before", fire_at=datetime(2026, 10, 4, 18, 0, tzinfo=TZ))]
    service, _, understandings, planner, _ = build(
        edited(1, due_at=MONDAY, due_precision="day"), planner=FakePlanner(plan)
    )

    outcome = await say(service, "встречу на понедельник")

    assert saved_edit(understandings)["changes"] == {"due_date": "2026-10-05"}
    assert planner.calls[0]["due_at"] == datetime(2026, 10, 5, 18, 0, tzinfo=TZ)
    assert planner.calls[0]["due_precision"] == "day"
    assert outcome.message == (
        "Перенёс: встреча с Ренатой. Срок: понедельник, 5 октября. Напомню: 4 октября в 18:00"
    )


async def test_move_to_a_part_of_day_sends_its_start_and_the_part() -> None:
    """«Перенеси на завтра утром» — часть дня: одно напоминание в 08:00 (§21.2, §21.3)."""
    plan = [Planned(stage="due", fire_at=datetime(2026, 9, 30, 8, 0, tzinfo=TZ))]
    service, _, understandings, planner, _ = build(
        edited(1, due_at="2026-09-30T08:00:00+05:00", due_precision="morning"),
        planner=FakePlanner(plan),
    )

    outcome = await say(service, "встречу с Ренатой перенеси на завтра утром")

    assert planner.calls == [
        {
            "due_at": datetime(2026, 9, 30, 8, 0, tzinfo=TZ),
            "due_precision": "morning",
            "kind": "task",
            "now": NOW,
        }
    ]
    assert saved_edit(understandings)["changes"] == {
        "due_at": "2026-09-30T08:00:00+05:00",
        "due_precision": "morning",
    }
    assert outcome.message == (
        "Перенёс: встреча с Ренатой. Срок: среда, 30 сентября, утром. Напомню: 30 сентября в 08:00"
    )


async def test_move_names_a_priority_back_to_normal() -> None:
    """Сменилась и срочность — словом в той же строке, «обычный» тоже."""
    service, _, understandings, _, _ = build(
        edited(1, due_at=TODAY_FIVE, due_precision="time", priority="normal"),
        planner=FakePlanner(MOVE_PLAN),
    )

    outcome = await say(service, "встречу на пять, не срочно")

    assert saved_edit(understandings)["changes"] == {"due_at": TODAY_FIVE, "priority": "normal"}
    assert outcome.message.endswith("Напомню: сегодня в 16:00. Приоритет: обычный")


async def test_other_fields_keep_the_reminders_and_say_fixed() -> None:
    """Люди заменяются списком, план не спрашивается, ответ «Поправил» (§12.5)."""
    service, _, understandings, planner, _ = build(edited(2, people=["Петров"]))

    outcome = await say(service, "отчёт не Кузнецову, а Петрову")

    assert planner.calls == []
    assert saved_edit(understandings) == {
        "task_id": REPORT_ID,
        "action": "change",
        "changes": {"people": ["Петров"]},
        "schedule": [],
        "question": None,
    }
    assert outcome.message == "Поправил: отправить отчёт. Срок: пятница, 2 октября. Люди: Петров"


async def test_fixed_task_without_a_due_has_no_due_line() -> None:
    service, _, _, _, _ = build(edited(3, title="купить две лампочки"))

    outcome = await say(service, "лампочек две")

    assert outcome.message == "Поправил: купить две лампочки"


async def test_removed_due_drops_the_reminders() -> None:
    service, _, understandings, planner, _ = build(edited(1, due_removed=True))

    outcome = await say(service, "у встречи нет срока")

    assert planner.calls == []
    assert saved_edit(understandings)["changes"] == {"due_at": None}
    assert saved_edit(understandings)["schedule"] == []
    assert outcome.message == "Убрал срок: встреча с Ренатой. Напоминать не буду."


async def test_done_closes_the_task_with_a_back_button() -> None:
    service, _, understandings, planner, _ = build(edited(1, action="done"))

    outcome = await say(service, "встречу провёл")

    assert planner.calls == []
    assert saved_edit(understandings) == {
        "task_id": MEETING_ID,
        "action": "done",
        "changes": {},
        "schedule": [],
        "question": None,
    }
    assert outcome.message == "Закрыл: встреча с Ренатой."
    assert outcome.buttons == (Button(text="Вернуть", data=f"reopen:{MEETING_ID}"),)


async def test_cancel_removes_the_task_from_the_list_with_a_back_button() -> None:
    service, _, understandings, _, _ = build(edited(2, action="cancel"))

    outcome = await say(service, "отчёт уже не нужен")

    assert saved_edit(understandings)["action"] == "cancel"
    assert outcome.message == "Убрал из списка: отправить отчёт."
    assert outcome.buttons == (Button(text="Вернуть", data=f"reopen:{REPORT_ID}"),)


async def test_unclear_value_asks_and_changes_nothing() -> None:
    """`change` с вопросом: ничего не меняется, даже понятное; пометка и вопрос."""
    service, _, understandings, planner, _ = build(
        edited(1, title="встреча с Ренатой и Олегом", top_question="На какое время перенести?")
    )

    outcome = await say(service, "перенеси встречу с Ренатой и Олегом на 1 1 700")

    assert planner.calls == []
    assert saved_edit(understandings) == {
        "task_id": MEETING_ID,
        "action": "change",
        "changes": {},
        "schedule": [],
        "question": "На какое время перенести?",
    }
    assert (
        outcome.message == "Не понял, как поправить: встреча с Ренатой. На какое время перенести?"
    )


async def test_answer_to_the_edit_question_goes_the_008_way() -> None:
    """Ответ на вопрос по правке дополняет задачу путём §10.2, а не правкой."""
    asked = OpenQuestion(
        task_id=MEETING_ID,
        question="На какое время перенести?",
        title=MEETING.title,
        kind="task",
        due_at=MEETING.due_at,
        due_precision="time",
        priority="high",
        promise=None,
        people=("Рената",),
        asked_at=NOW - timedelta(minutes=5),
    )
    verdict = make_understanding(
        title=MEETING.title,
        due_at=TODAY_FIVE,
        due_precision="time",
        answers_question=True,
        edit=edit(task=1, due_at=TODAY_FIVE, due_precision="time"),
    )
    service, _, understandings, _, _ = build(
        verdict, planner=FakePlanner(MOVE_PLAN), questions=FakeQuestions(asked)
    )

    outcome = await say(service, "на пять")

    assert saved_edit(understandings) is None
    assert saved(understandings, "amend")["task_id"] == MEETING_ID
    assert outcome.message.startswith("Понял: встреча с Ренатой")


async def test_change_without_values_says_nothing_to_change() -> None:
    """Задача всё равно уходит в `edit`: база проверит активность (решение 6)."""
    service, _, understandings, _, _ = build(edited(1))

    outcome = await say(service, "поменяй встречу")

    assert saved_edit(understandings) == {
        "task_id": MEETING_ID,
        "action": "change",
        "changes": {},
        "schedule": [],
        "question": None,
    }
    assert outcome.message == (
        "Не понял, что поменять в задаче «встреча с Ренатой» — ничего не менял."
    )


async def test_same_values_are_nothing_to_change() -> None:
    service, _, understandings, planner, _ = build(
        edited(1, due_at="2026-10-02T17:00:00+05:00", due_precision="time", priority="high")
    )

    outcome = await say(service, "встреча в пятницу в пять, срочно")

    assert planner.calls == []
    assert saved_edit(understandings)["changes"] == {}
    assert outcome.message.startswith("Не понял, что поменять")


async def test_task_closed_meanwhile_is_refused_by_the_base() -> None:
    """База отказала (задачу закрыли, пока модель думала) — «Не смог записать…»."""
    service, _, _, _, _ = build(
        edited(1, action="done"), understandings=FakeUnderstandings(broken=True)
    )

    outcome = await say(service, "сделал")

    assert not outcome.ok
    assert outcome.message == texts.NOT_SAVED
    assert outcome.buttons == ()


async def test_model_failure_records_as_is_and_edits_nothing() -> None:
    service, _, understandings, _, _ = build(NotUnderstood(reason="timeout"))

    outcome = await say(service, "перенеси встречу на пять")

    assert saved_edit(understandings) is None
    assert saved(understandings, "task")["needs_review"] is True
    assert outcome.message.startswith("Записал как есть")


async def test_repeated_update_answers_the_saved_text_without_buttons() -> None:
    messages = FakeMessages(SavedMessage(id="9a71", reply="Закрыл: встреча с Ренатой."))
    service, analyst, understandings, _, _ = build(edited(1, action="done"), messages=messages)

    outcome = await say(service, "сделал")

    assert analyst.calls == []
    assert understandings.calls == []
    assert outcome.message == "Закрыл: встреча с Ренатой."
    assert outcome.buttons == ()


# ------------------------------------------------------ кандидаты и не найдено


async def test_candidates_ask_with_buttons_and_change_nothing() -> None:
    """Кандидаты (§12.6): вопрос с действием, кнопки в порядке модели, ничего не меняется."""
    service, _, understandings, planner, _ = build(
        edited(None, candidates=[2, 1], due_at=MONDAY, due_precision="day")
    )

    outcome = await say(service, "перенеси на понедельник")

    assert planner.calls == []
    assert saved_edit(understandings) is None
    assert saved(understandings, "task") is None
    assert outcome.message == "Какую задачу перенести на понедельник, 5 октября?"
    assert outcome.buttons == (
        Button(text="отправить отчёт — 2 окт", data=f"pick:{MESSAGE_ID}:{REPORT_ID}"),
        Button(text="встреча с Ренатой — 2 окт, 17:00", data=f"pick:{MESSAGE_ID}:{MEETING_ID}"),
    )


async def test_candidates_of_a_move_to_a_part_of_day_hear_the_part() -> None:
    service, _, _, _, _ = build(
        edited(None, candidates=[2, 1], due_at="2026-09-29T18:00:00+05:00", due_precision="evening")
    )

    outcome = await say(service, "перенеси на вечер")

    assert outcome.message == "Какую задачу перенести на сегодня вечером?"


async def test_single_candidate_still_asks() -> None:
    service, _, _, _, _ = build(edited(None, action="done", candidates=[3]))

    outcome = await say(service, "купил")

    assert outcome.message == "Какую задачу закрыть?"
    assert outcome.buttons == (Button(text="купить лампочку", data=f"pick:{MESSAGE_ID}:{LAMP_ID}"),)


async def test_candidates_are_capped_at_five() -> None:
    many = [
        make_details(id=f"5a5c{index:04d}-0000-4000-8000-000000000000", title=f"задача {index}")
        for index in range(7)
    ]
    service, _, _, _, _ = build(
        edited(None, action="cancel", candidates=[1, 2, 3, 4, 5, 6, 7]), FakeEdits(many)
    )

    outcome = await say(service, "убери")

    assert outcome.message == "Какую задачу убрать из списка?"
    assert len(outcome.buttons) == 5


async def test_unfound_move_records_a_new_task_without_the_question() -> None:
    """Не найдено, перенос (§12.3): новая задача по полям верхнего уровня, срок —
    из правки; вопрос был о правке и новой задаче не ставится (решение 3)."""
    plan = [Planned(stage="before", fire_at=datetime(2026, 10, 1, 14, 0, tzinfo=TZ))]
    questions = FakeQuestions()
    service, _, understandings, planner, _ = build(
        edited(
            None,
            due_at="2026-10-01T15:00:00+05:00",
            due_precision="time",
            top_title="встреча с Кириллом",
            top_question="С кем встреча?",
        ),
        planner=FakePlanner(plan),
        understandings=FakeUnderstandings(questions=questions),
        questions=questions,
    )

    outcome = await say(service, "встреча с Кириллом переехала на четверг в три")

    task = saved(understandings, "task")
    assert task["title"] == "встреча с Кириллом"
    assert task["due_at"] == "2026-10-01T15:00:00+05:00"
    assert task["due_precision"] == "time"
    assert "open_question" not in task
    assert questions.asked is None
    assert saved_edit(understandings) is None
    assert understandings.calls[0]["reminders"] == [item.as_row() for item in plan]
    assert planner.calls[0]["due_at"] == datetime(2026, 10, 1, 15, 0, tzinfo=TZ)
    assert outcome.message == (
        "Не нашёл открытой задачи — записал новую: встреча с Кириллом. "
        "Срок: четверг, 1 октября, 15:00. Напомню: 1 октября в 14:00"
    )


async def test_unfound_other_edit_records_nothing() -> None:
    service, _, understandings, _, _ = build(
        edited(None, action="done", top_title="забрать посылку")
    )

    outcome = await say(service, "посылку забрал")

    assert saved(understandings, "task") is None
    assert saved_edit(understandings) is None
    assert outcome.message == "Не нашёл открытой задачи «забрать посылку» — ничего не менял."


async def test_number_outside_the_list_is_not_found() -> None:
    service, _, understandings, _, _ = build(
        edited(9, action="done", candidates=[8], top_title="встреча")
    )

    outcome = await say(service, "сделал")

    assert saved_edit(understandings) is None
    assert outcome.message == "Не нашёл открытой задачи «встреча» — ничего не менял."


async def test_unfound_move_of_a_chat_records_nothing() -> None:
    """Вид верхнего уровня не задача, идея или желание — не записывается (решение 7)."""
    service, _, understandings, _, _ = build(
        edited(None, due_at=TODAY_FIVE, due_precision="time", top_kind="chat")
    )

    outcome = await say(service, "перенеси на пять")

    assert saved(understandings, "task") is None
    assert outcome.message.startswith("Не нашёл открытой задачи «")


# ------------------------------------------------------------ кнопка кандидата


def candidate_message(verdict: Understanding, task_id: str | None = None) -> StoredMessage:
    return StoredMessage(
        id="9a71",
        text="перенеси на понедельник",
        task_id=task_id,
        analysis=verdict.model_dump(mode="json"),
        reply="Какую задачу перенести на понедельник, 5 октября?",
    )


MOVE_CANDIDATES = edited(None, candidates=[2, 1], due_at=MONDAY, due_precision="day")
MONDAY_PLAN = [Planned(stage="before", fire_at=datetime(2026, 10, 4, 18, 0, tzinfo=TZ))]


async def test_pick_writes_the_edit_for_the_chosen_task() -> None:
    """Нажатие (§12.6): разбор из базы, план на момент нажатия, ответ вместо вопроса."""
    store = FakeEdits(OPEN, messages={MESSAGE_ID: candidate_message(MOVE_CANDIDATES)})
    service, _, _, planner, _ = build(make_understanding(), store, planner=FakePlanner(MONDAY_PLAN))

    outcome = await service.pick(
        chat_id=OWNER_ID, telegram_message_id=MESSAGE_ID, task_id=MEETING_ID
    )

    reply = "Перенёс: встреча с Ренатой. Срок: понедельник, 5 октября. Напомню: 4 октября в 18:00"
    assert outcome == PressOutcome(message=reply, replace=True)
    assert planner.calls[0]["now"] == NOW
    assert store.picks == [
        (
            "9a71",
            {
                "task_id": MEETING_ID,
                "action": "change",
                "changes": {"due_date": "2026-10-05"},
                "schedule": [item.as_row() for item in MONDAY_PLAN],
                "question": None,
            },
            reply,
        )
    ]


async def test_pick_of_done_gets_the_back_button() -> None:
    verdict = edited(None, action="done", candidates=[1, 2])
    store = FakeEdits(OPEN, messages={MESSAGE_ID: candidate_message(verdict)})
    service, _, _, _, _ = build(make_understanding(), store)

    outcome = await service.pick(
        chat_id=OWNER_ID, telegram_message_id=MESSAGE_ID, task_id=REPORT_ID
    )

    assert outcome == PressOutcome(
        message="Закрыл: отправить отчёт.",
        replace=True,
        buttons=(Button(text="Вернуть", data=f"reopen:{REPORT_ID}"),),
    )
    assert store.tasks[REPORT_ID].status == "done"


async def test_pick_with_a_question_asks_about_the_chosen_task() -> None:
    """Вопрос ждёт выбора и ставится на выбранную задачу (решение 3)."""
    verdict = edited(None, candidates=[1, 2], top_question="На какое время?")
    store = FakeEdits(OPEN, messages={MESSAGE_ID: candidate_message(verdict)})
    service, _, _, _, _ = build(make_understanding(), store)

    outcome = await service.pick(
        chat_id=OWNER_ID, telegram_message_id=MESSAGE_ID, task_id=MEETING_ID
    )

    assert outcome.message == "Не понял, как поправить: встреча с Ренатой. На какое время?"
    assert store.picks[0][1]["question"] == "На какое время?"


async def test_second_press_writes_nothing_and_shows_the_saved_answer() -> None:
    store = FakeEdits(OPEN, messages={MESSAGE_ID: candidate_message(MOVE_CANDIDATES)})
    service, _, _, _, _ = build(make_understanding(), store, planner=FakePlanner(MONDAY_PLAN))
    first = await service.pick(chat_id=OWNER_ID, telegram_message_id=MESSAGE_ID, task_id=MEETING_ID)

    again = await service.pick(chat_id=OWNER_ID, telegram_message_id=MESSAGE_ID, task_id=REPORT_ID)

    assert again == PressOutcome(message=first.message, replace=True)
    assert len(store.picks) == 1


async def test_press_racing_an_earlier_pick_shows_the_saved_answer() -> None:
    """Правку по сообщению записали между чтением и записью — второй не будет."""
    earlier = "Закрыл: отправить отчёт."

    class Raced(FakeEdits):
        async def pick(self, message_id: str, edit: Any, reply: str) -> Any:
            stored = self.messages[MESSAGE_ID]
            self.messages[MESSAGE_ID] = replace(stored, task_id=REPORT_ID, reply=earlier)
            return await super().pick(message_id, edit, reply)

    store = Raced(OPEN, messages={MESSAGE_ID: candidate_message(MOVE_CANDIDATES)})
    service, _, _, _, _ = build(make_understanding(), store)

    outcome = await service.pick(
        chat_id=OWNER_ID, telegram_message_id=MESSAGE_ID, task_id=MEETING_ID
    )

    assert outcome == PressOutcome(message=earlier, replace=True)
    assert store.picks == []


async def test_picked_task_already_closed_writes_nothing() -> None:
    closed = replace(MEETING, status="cancelled")
    store = FakeEdits(
        [LAMP, REPORT, closed], messages={MESSAGE_ID: candidate_message(MOVE_CANDIDATES)}
    )
    service, _, _, planner, _ = build(make_understanding(), store)

    outcome = await service.pick(
        chat_id=OWNER_ID, telegram_message_id=MESSAGE_ID, task_id=MEETING_ID
    )

    assert outcome == PressOutcome(message=texts.PICKED_GONE, replace=False)
    assert planner.calls == []
    assert store.picks == []


async def test_picked_task_closed_meanwhile_writes_nothing() -> None:
    """Задачу закрыли между чтением и записью — `pick_task` вернул пустую задачу."""

    class Closing(FakeEdits):
        async def pick(self, message_id: str, edit: Any, reply: str) -> Any:
            self.tasks[MEETING_ID] = replace(self.tasks[MEETING_ID], status="done")
            return await super().pick(message_id, edit, reply)

    store = Closing(OPEN, messages={MESSAGE_ID: candidate_message(MOVE_CANDIDATES)})
    service, _, _, _, _ = build(make_understanding(), store)

    outcome = await service.pick(
        chat_id=OWNER_ID, telegram_message_id=MESSAGE_ID, task_id=MEETING_ID
    )

    assert outcome == PressOutcome(message=texts.PICKED_GONE, replace=False)


async def test_picked_task_deleted_writes_nothing() -> None:
    store = FakeEdits([LAMP, REPORT], messages={MESSAGE_ID: candidate_message(MOVE_CANDIDATES)})
    service, _, _, _, _ = build(make_understanding(), store)

    outcome = await service.pick(
        chat_id=OWNER_ID, telegram_message_id=MESSAGE_ID, task_id=MEETING_ID
    )

    assert outcome == PressOutcome(message=texts.PICKED_GONE, replace=False)


@pytest.mark.parametrize("broken", ["message", "task", "pick"])
async def test_press_the_base_refused_keeps_the_question(broken: str) -> None:
    """Отказ базы — всплывающий ответ, вопрос с кнопками остаётся (решение 10)."""
    store = FakeEdits(
        OPEN, messages={MESSAGE_ID: candidate_message(MOVE_CANDIDATES)}, broken={broken}
    )
    service, _, _, _, _ = build(make_understanding(), store)

    outcome = await service.pick(
        chat_id=OWNER_ID, telegram_message_id=MESSAGE_ID, task_id=MEETING_ID
    )

    assert outcome == PressOutcome(message=texts.NOT_PICKED, replace=False)


async def test_press_the_plan_refused_keeps_the_question() -> None:
    store = FakeEdits(OPEN, messages={MESSAGE_ID: candidate_message(MOVE_CANDIDATES)})
    service, _, _, _, _ = build(make_understanding(), store, planner=FakePlanner(broken=True))

    outcome = await service.pick(
        chat_id=OWNER_ID, telegram_message_id=MESSAGE_ID, task_id=MEETING_ID
    )

    assert outcome == PressOutcome(message=texts.NOT_PICKED, replace=False)
    assert store.picks == []


async def test_press_under_an_unknown_message_finds_nothing() -> None:
    service, _, _, _, _ = build(make_understanding())

    outcome = await service.pick(
        chat_id=OWNER_ID, telegram_message_id=MESSAGE_ID, task_id=MEETING_ID
    )

    assert outcome == PressOutcome(message=texts.DONE_UNKNOWN, replace=False)


async def test_press_with_an_unreadable_analysis_guesses_nothing() -> None:
    stored = replace(candidate_message(MOVE_CANDIDATES), analysis={"kind": "chat"})
    store = FakeEdits(OPEN, messages={MESSAGE_ID: stored})
    service, _, _, _, _ = build(make_understanding(), store)

    outcome = await service.pick(
        chat_id=OWNER_ID, telegram_message_id=MESSAGE_ID, task_id=MEETING_ID
    )

    assert outcome == PressOutcome(message=texts.DONE_UNKNOWN, replace=False)
    assert store.picks == []


async def test_press_without_a_store_writes_nothing() -> None:
    service, _, _, _, _ = build(make_understanding(), wired=False)

    outcome = await service.pick(
        chat_id=OWNER_ID, telegram_message_id=MESSAGE_ID, task_id=MEETING_ID
    )

    assert outcome == PressOutcome(message=texts.NOT_PICKED, replace=False)


# ------------------------------------------------------------------ «Вернуть»


async def test_reopen_plans_by_the_due_and_says_back_in_work() -> None:
    closed = replace(MEETING, status="done")
    store = FakeEdits([LAMP, REPORT, closed])
    service, _, _, planner, _ = build(
        make_understanding(), store, planner=FakePlanner(MEETING_PLAN)
    )

    outcome = await service.reopen(task_id=MEETING_ID)

    assert planner.calls == [
        {"due_at": MEETING.due_at, "due_precision": "time", "kind": "task", "now": NOW}
    ]
    assert store.reopens == [(MEETING_ID, MEETING_PLAN)]
    assert outcome == PressOutcome(
        message=(
            "Вернул в работу: встреча с Ренатой. Срок: пятница, 2 октября, 17:00. "
            "Напомню: 2 октября в 16:00"
        ),
        replace=True,
    )


async def test_reopen_with_a_past_due_has_no_remind_line() -> None:
    """Срок в прошлом — план пуст, «Напомню» нет (решение 4)."""
    past = replace(MEETING, status="cancelled", due_at=datetime(2026, 9, 28, 17, 0, tzinfo=TZ))
    service, _, _, _, store = build(make_understanding(), FakeEdits([past]))

    outcome = await service.reopen(task_id=MEETING_ID)

    assert store.reopens == [(MEETING_ID, [])]
    assert outcome == PressOutcome(
        message="Вернул в работу: встреча с Ренатой. Срок: понедельник, 28 сентября, 17:00",
        replace=True,
    )


async def test_reopen_of_an_active_task_answers_the_same_without_a_write() -> None:
    service, _, _, _, store = build(make_understanding(), planner=FakePlanner(MEETING_PLAN))

    outcome = await service.reopen(task_id=MEETING_ID)

    assert store.reopens == []
    assert outcome.replace
    assert outcome.message.startswith("Вернул в работу: встреча с Ренатой.")


async def test_reopen_of_a_deleted_task_finds_nothing() -> None:
    service, _, _, _, _ = build(make_understanding(), FakeEdits([LAMP]))

    outcome = await service.reopen(task_id=MEETING_ID)

    assert outcome == PressOutcome(message=texts.DONE_UNKNOWN, replace=False)


@pytest.mark.parametrize("broken", ["task", "reopen"])
async def test_reopen_the_base_refused(broken: str) -> None:
    closed = replace(MEETING, status="done")
    service, _, _, _, _ = build(make_understanding(), FakeEdits([closed], broken={broken}))

    outcome = await service.reopen(task_id=MEETING_ID)

    assert outcome == PressOutcome(message=texts.NOT_REOPENED, replace=False)


async def test_reopen_the_plan_refused() -> None:
    closed = replace(MEETING, status="done")
    service, _, _, _, store = build(
        make_understanding(), FakeEdits([closed]), planner=FakePlanner(broken=True)
    )

    outcome = await service.reopen(task_id=MEETING_ID)

    assert outcome == PressOutcome(message=texts.NOT_REOPENED, replace=False)
    assert store.reopens == []


# --------------------------------------------------------- хранилище над базой


async def test_database_store_asks_only_for_the_settings_owner() -> None:
    """Инвариант 2: владелец — из настроек, в каждом чтении и каждой записи."""
    reads = FakeClient(data=[])
    store = DatabaseEditStore(SETTINGS, as_client(reads))

    assert await store.open_tasks(50) == []
    assert await store.last_message_event(NOW) is None
    assert await store.last_reminder_event(NOW) is None
    assert await store.reminder_task(40) is None
    assert await store.message(OWNER_ID, 39) is None
    assert await store.task(MEETING_ID) is None
    assert await store.same_minute(NOW, MEETING_ID) == []
    assert await store.recent_messages(NOW - timedelta(hours=1), NOW, 10) == []

    owners = [call for call in reads.calls if call[:2] == ("eq", "owner_telegram_id")]
    assert owners == [("eq", "owner_telegram_id", OWNER_ID)] * 8

    writes = FakeClient(data={"id": "9a71", "task_id": None, "reply": None})
    store = DatabaseEditStore(SETTINGS, as_client(writes))
    await store.pick("9a71", {"task_id": MEETING_ID}, "ответ")
    rpc = [call for call in writes.calls if call[0] == "rpc"]
    assert rpc[0][2]["owner_telegram_id"] == OWNER_ID

    reopens = FakeClient(data=None)
    store = DatabaseEditStore(SETTINGS, as_client(reopens))
    assert await store.reopen(MEETING_ID, []) is None
    rpc = [call for call in reopens.calls if call[0] == "rpc"]
    assert rpc[0][2]["owner_telegram_id"] == OWNER_ID


# ------------------------------------------- недавний разговор, блок 6 (§17.3)

# Текущее сообщение пришло за секунду до «сейчас»: граница блока — его время.
RECEIVED = NOW - timedelta(seconds=1)
ASKED_THURSDAY = RecentMessage(
    received_at=NOW - timedelta(minutes=10),
    kind="text",
    text="что у меня в четверг?",
    forwarded_from=None,
    reply="В четверг в 17:00 встреча с Ренатой.",
)
FROM_RENATA = RecentMessage(
    received_at=NOW - timedelta(minutes=5),
    kind="text",
    text="Во сколько?",
    forwarded_from="Рената",
    reply=None,
)
TALK = recent_block([ASKED_THURSDAY, FROM_RENATA], TZ)


def received(at: datetime | None = RECEIVED) -> FakeMessages:
    """Первый шаг приёма, отдающий время записи текущего сообщения."""
    return FakeMessages(SavedMessage(id="9a71", reply=None, received_at=at))


async def test_own_message_gets_the_recent_talk_after_the_task_list() -> None:
    store = FakeEdits(OPEN, recent=[FROM_RENATA, ASKED_THURSDAY])
    service, analyst, _, _, _ = build(make_understanding(), store, messages=received())

    await say(service, "да, можно в четверг")

    assert TALK is not None
    assert analyst.recents == [TALK.text]
    assert analyst.tasks == [[MEETING, REPORT, LAMP]]
    # Окно — тот же час, что у последней задачи; граница — само сообщение.
    assert store.talks == [(NOW - timedelta(hours=1), RECEIVED, 10)]


async def test_current_later_and_older_messages_stay_out_of_the_block() -> None:
    """Текущее, пришедшее позже него и старше часа в блок не попадают."""
    current = replace(FROM_RENATA, received_at=RECEIVED, forwarded_from=None, text="да")
    later = replace(FROM_RENATA, received_at=NOW, forwarded_from=None, text="и ещё")
    old = replace(ASKED_THURSDAY, received_at=NOW - timedelta(minutes=61))
    store = FakeEdits(OPEN, recent=[old, ASKED_THURSDAY, current, later])
    service, analyst, _, _, _ = build(make_understanding(), store, messages=received())

    await say(service, "да")

    expected = recent_block([ASKED_THURSDAY], TZ)
    assert expected is not None
    assert analyst.recents == [expected.text]


async def test_without_messages_within_the_hour_there_is_no_block() -> None:
    service, analyst, _, _, store = build(make_understanding(), messages=received())

    await say(service)

    assert analyst.recents == [None]
    assert len(store.talks) == 1


async def test_without_the_time_of_the_message_the_boundary_is_the_clock() -> None:
    store = FakeEdits(OPEN, recent=[ASKED_THURSDAY])
    service, _, _, _, _ = build(make_understanding(), store, messages=received(None))

    await say(service)

    assert store.talks == [(NOW - timedelta(hours=1), NOW, 10)]


async def test_own_voice_gets_the_recent_talk() -> None:
    store = FakeEdits(OPEN, recent=[ASKED_THURSDAY, FROM_RENATA])
    service, analyst, _, _, _ = build(make_understanding(), store, messages=received())

    await service.record_from_voice(
        chat_id=OWNER_ID,
        telegram_message_id=MESSAGE_ID,
        kind="voice",
        file_id="voice-1",
        duration=5,
        load_audio=load_audio,
    )

    assert TALK is not None
    assert analyst.recents == [TALK.text]


async def test_forwarded_message_does_not_read_the_talk() -> None:
    store = FakeEdits(OPEN, recent=[ASKED_THURSDAY])
    service, analyst, _, _, _ = build(make_understanding(), store, messages=received())

    await say(service, "Во сколько?", forwarded_from="Рената")

    assert store.talks == []
    assert analyst.recents == [None]


async def test_photo_does_not_read_the_talk() -> None:
    store = FakeEdits(OPEN, recent=[ASKED_THURSDAY])
    service, _, _, _, _ = build(make_understanding(), store, messages=received())

    await service.record_from_photo(
        chat_id=OWNER_ID,
        telegram_message_id=MESSAGE_ID,
        file_id="photo-1",
        media_type="image/jpeg",
        caption="что это?",
        load_image=load_image,
    )

    assert store.talks == []


async def test_failed_talk_read_is_logged_and_the_message_understood(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Поручение важнее контекста: без блока, строка в журнал, ответ как обычно."""
    store = FakeEdits(OPEN, recent=[ASKED_THURSDAY], broken={"recent_messages"})
    service, analyst, understandings, _, _ = build(make_understanding(), store, messages=received())

    with caplog.at_level(logging.WARNING, logger="solomon.services.tasks"):
        outcome = await say(service, "купить лампочку")

    assert analyst.recents == [None]
    assert analyst.tasks == [[MEETING, REPORT, LAMP]]
    assert outcome.ok
    assert outcome.message.startswith("Записал: купить лампочку")
    assert saved(understandings, "task") is not None
    assert "Недавний разговор не прочитан" in caplog.text


async def test_failed_task_list_keeps_the_talk() -> None:
    store = FakeEdits(OPEN, recent=[ASKED_THURSDAY, FROM_RENATA], broken={"open_tasks"})
    service, analyst, _, _, _ = build(make_understanding(), store, messages=received())

    await say(service)

    assert TALK is not None
    assert analyst.tasks == [None]
    assert analyst.recents == [TALK.text]


async def test_service_without_a_store_has_no_block() -> None:
    service, analyst, _, _, _ = build(make_understanding(), wired=False, messages=received())

    await say(service)

    assert analyst.recents == [None]


async def test_talk_is_logged_only_as_a_count(caplog: pytest.LogCaptureFixture) -> None:
    store = FakeEdits(OPEN, recent=[ASKED_THURSDAY, FROM_RENATA])
    service, _, _, _, _ = build(make_understanding(), store, messages=received())

    with caplog.at_level(logging.DEBUG, logger="solomon"):
        await say(service)

    assert "Недавний разговор: сообщений 2" in caplog.text
    for said in ("четверг", "Ренат", "Во сколько"):
        assert said not in caplog.text


# ------------------------------------- правка задачи из вопроса главнее ответа


def asked_about(task: Any, question: str = texts.UNDATED_QUESTION) -> OpenQuestion:
    """Открытый вопрос по задаче из списка — как его читает бот (§10.1)."""
    return OpenQuestion(
        task_id=task.id,
        question=question,
        title=task.title,
        kind="task",
        due_at=task.due_at,
        due_precision=task.due_precision,
        priority=task.priority,
        promise=None,
        people=task.people,
        asked_at=NOW - timedelta(hours=1),
    )


def task_edit(task: int | None, action: str) -> TaskEdit | None:
    return edited(task, action=action).edit


# Список, как он ушёл в промпт: номера правки — по нему.
NUMBERED = [MEETING, REPORT, LAMP]


@pytest.mark.parametrize("action", ["done", "cancel"])
def test_done_or_cancel_of_the_asked_task_beats_the_answer(action: str) -> None:
    """«Сделал», «уже не нужно» о задаче из вопроса — правка, а не ответ (§19.5)."""
    assert edit_closes_asked(task_edit(3, action), asked_about(LAMP), NUMBERED) is True


@pytest.mark.parametrize("action", ["change", "skip"])
def test_other_edits_of_the_asked_task_yield_to_the_answer(action: str) -> None:
    """Перенос и прочее по задаче из вопроса уступают ответу, как раньше (§12.1)."""
    assert edit_closes_asked(task_edit(3, action), asked_about(LAMP), NUMBERED) is False


def test_done_of_another_task_yields_to_the_answer() -> None:
    assert edit_closes_asked(task_edit(1, "done"), asked_about(LAMP), NUMBERED) is False


def test_nothing_to_beat_without_a_question_an_edit_or_a_list() -> None:
    assert edit_closes_asked(None, asked_about(LAMP), NUMBERED) is False
    assert edit_closes_asked(task_edit(3, "done"), None, OPEN) is False
    assert edit_closes_asked(task_edit(3, "done"), asked_about(LAMP), None) is False
    assert edit_closes_asked(task_edit(9, "done"), asked_about(LAMP), NUMBERED) is False
    assert edit_closes_asked(task_edit(None, "done"), asked_about(LAMP), NUMBERED) is False


@pytest.mark.parametrize(
    ("action", "reply"),
    [("done", "Закрыл: купить лампочку."), ("cancel", "Убрал из списка: купить лампочку.")],
)
async def test_answer_and_closing_edit_of_the_asked_task_make_an_edit(
    action: str, reply: str
) -> None:
    """Модель отдала и ответ, и `done`/`cancel` задачи из вопроса — побеждает правка."""
    verdict = make_understanding(
        title="купить лампочку", answers_question=True, edit=edit(task=3, action=action)
    )
    service, _, understandings, _, _ = build(verdict, questions=FakeQuestions(asked_about(LAMP)))

    outcome = await say(service, "уже купил")

    assert saved(understandings, "amend") is None
    assert saved_edit(understandings)["task_id"] == LAMP_ID
    assert saved_edit(understandings)["action"] == action
    assert outcome.message == reply
    assert outcome.buttons == (Button(text="Вернуть", data=f"reopen:{LAMP_ID}"),)


async def test_answer_and_done_of_another_task_stay_an_answer() -> None:
    """Правка другой задачи уступает ответу, как раньше (§12.1)."""
    verdict = make_understanding(
        title="купить лампочку", answers_question=True, edit=edit(task=1, action="done")
    )
    service, _, understandings, _, _ = build(verdict, questions=FakeQuestions(asked_about(LAMP)))

    outcome = await say(service, "пока не знаю")

    assert saved_edit(understandings) is None
    assert saved(understandings, "amend")["task_id"] == LAMP_ID
    assert outcome.message == texts.ASK_LATER


async def test_forwarded_answer_hears_no_closing_edit() -> None:
    """У пересланного правки нет (§15.2): ответ остаётся ответом."""
    verdict = make_understanding(
        title="купить лампочку", answers_question=True, edit=edit(task=3, action="done")
    )
    service, _, understandings, _, _ = build(verdict, questions=FakeQuestions(asked_about(LAMP)))

    await say(service, "уже купил", forwarded_from="Сергей")

    assert saved_edit(understandings) is None
    assert saved(understandings, "amend")["task_id"] == LAMP_ID
