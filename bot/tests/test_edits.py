"""Правка словом — чистые функции (`techspec/12-chat-edit.md`).

Нумерация списка в промпте, номер модели → задача, подсказки свайпа и
последней задачи, правка для базы из разбора, кнопки кандидатов. Сети и
базы здесь нет вовсе: всё, что решает бот, решается этими функциями.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from solomon.db.tasks import TaskDetails, TaskEvent
from solomon.services import edits
from solomon.services.understanding import TaskEdit
from tests.conftest import OWNER_TIMEZONE

TZ = ZoneInfo(OWNER_TIMEZONE)
# Вторник, 29 сентября 2026, полдень в поясе владельца (+05:00).
NOW = datetime(2026, 9, 29, 12, 0, tzinfo=TZ)
TASK_ID = "5b0c7a52-8f3e-4c1d-9a6b-2e4f1d3c8b90"


def make_task(
    task_id: str = TASK_ID,
    title: str = "встреча с Ренатой",
    *,
    due_at: datetime | None = None,
    due_precision: str | None = None,
    created_at: datetime = NOW - timedelta(days=1),
    kind: str = "task",
    priority: str = "normal",
    promise: str | None = None,
    people: tuple[str, ...] = (),
    status: str = "active",
) -> TaskDetails:
    return TaskDetails(
        id=task_id,
        title=title,
        kind=kind,
        status=status,
        due_at=due_at,
        due_precision=due_precision,
        priority=priority,
        promise=promise,
        people=people,
        created_at=created_at,
    )


def make_edit(**fields: Any) -> TaskEdit:
    base: dict[str, Any] = {
        "action": "change",
        "task": 1,
        "candidates": [],
        "title": None,
        "due_at": None,
        "due_precision": None,
        "due_removed": False,
        "repeat": None,
        "repeat_removed": False,
        "priority": None,
        "promise": None,
        "people": None,
    }
    return TaskEdit.model_validate({**base, **fields})


# --- нумерация -------------------------------------------------------------


def test_tasks_with_due_go_first_by_due_then_without_due_newest_first() -> None:
    later = make_task("later", due_at=NOW + timedelta(days=3), due_precision="day")
    sooner = make_task("sooner", due_at=NOW + timedelta(hours=5), due_precision="time")
    old = make_task("old", created_at=NOW - timedelta(days=9))
    new = make_task("new", created_at=NOW - timedelta(hours=1))

    numbered = edits.number_tasks([old, later, new, sooner])

    assert [task.id for task in numbered] == ["sooner", "later", "new", "old"]


def test_same_due_puts_the_newer_task_higher() -> None:
    due = NOW + timedelta(days=1)
    old = make_task("old", due_at=due, due_precision="day", created_at=NOW - timedelta(days=5))
    new = make_task("new", due_at=due, due_precision="day", created_at=NOW - timedelta(days=1))

    assert [task.id for task in edits.number_tasks([old, new])] == ["new", "old"]


def test_list_is_cut_at_fifty() -> None:
    many = [
        make_task(f"t{index}", created_at=NOW - timedelta(minutes=index)) for index in range(60)
    ]

    numbered = edits.number_tasks(many)

    assert len(numbered) == edits.TASK_LIMIT == 50
    assert numbered[0].id == "t0"
    assert numbered[-1].id == "t49"


def test_number_is_turned_into_the_task_and_back() -> None:
    numbered = edits.number_tasks([make_task("a"), make_task("b", created_at=NOW)])

    assert edits.task_by_number(numbered, 1) == numbered[0]
    assert edits.task_by_number(numbered, 2) == numbered[1]
    assert edits.number_of(numbered, numbered[1].id) == 2


@pytest.mark.parametrize("number", [None, 0, -1, 3, 51])
def test_number_outside_the_list_is_no_task(number: int | None) -> None:
    numbered = edits.number_tasks([make_task("a"), make_task("b", created_at=NOW)])

    assert edits.task_by_number(numbered, number) is None


def test_unknown_task_has_no_number() -> None:
    numbered = edits.number_tasks([make_task("a")])

    assert edits.number_of(numbered, "b") is None
    assert edits.number_of(numbered, None) is None


def test_candidates_keep_the_order_drop_unknown_and_repeated_and_stop_at_five() -> None:
    numbered = edits.number_tasks(
        [make_task(f"t{index}", created_at=NOW - timedelta(minutes=index)) for index in range(8)]
    )

    picked = edits.candidates_of(numbered, [3, 9, 1, 3, 0, 2, 4, 5, 6, 7])

    assert [task.id for task in picked] == ["t2", "t0", "t1", "t3", "t4"]
    assert edits.CANDIDATE_LIMIT == 5


def test_no_valid_candidates_is_an_empty_list() -> None:
    numbered = edits.number_tasks([make_task("a")])

    assert edits.candidates_of(numbered, [0, 2, 7]) == []


# --- последняя задача в разговоре ------------------------------------------


def test_last_task_is_the_later_of_the_message_and_the_reminder() -> None:
    numbered = edits.number_tasks([make_task("a"), make_task("b", created_at=NOW)])
    message = TaskEvent(task_id="a", at=NOW - timedelta(minutes=30))
    reminder = TaskEvent(task_id="b", at=NOW - timedelta(minutes=10))

    assert edits.last_task_number([message, reminder], numbered, NOW) == 1
    assert edits.last_task_number([reminder, message], numbered, NOW) == 1
    earlier_reminder = TaskEvent(task_id="b", at=NOW - timedelta(minutes=50))
    assert edits.last_task_number([message, earlier_reminder], numbered, NOW) == 2


def test_last_task_older_than_an_hour_is_not_named() -> None:
    numbered = edits.number_tasks([make_task("a")])

    fresh = TaskEvent(task_id="a", at=NOW - timedelta(minutes=59))
    stale = TaskEvent(task_id="a", at=NOW - timedelta(minutes=61))

    assert edits.last_task_number([fresh], numbered, NOW) == 1
    assert edits.last_task_number([stale], numbered, NOW) is None
    assert edits.last_task_number([None, None], numbered, NOW) is None


def test_later_event_about_a_closed_task_hides_the_earlier_one() -> None:
    """Задачи позднего события нет в списке — строки нет, к раннему не откатываемся."""
    numbered = edits.number_tasks([make_task("a")])
    message = TaskEvent(task_id="a", at=NOW - timedelta(minutes=40))
    reminder = TaskEvent(task_id="closed", at=NOW - timedelta(minutes=5))

    assert edits.last_task_number([message, reminder], numbered, NOW) is None


# --- свайп -------------------------------------------------------------------


def test_swipe_on_a_reminder_names_the_task_number() -> None:
    line = edits.swipe_line("reminder", 3, "Напоминаю: встреча с Ренатой")
    assert line == "Ответ на напоминание о задаче №3"


def test_swipe_on_a_reminder_of_a_task_outside_the_list_carries_the_text() -> None:
    line = edits.swipe_line("reminder", None, "Напоминаю: встреча с Ренатой")
    assert line == "Ответ на напоминание: «Напоминаю: встреча с Ренатой»"


def test_swipe_on_another_bot_message_carries_its_text() -> None:
    line = edits.swipe_line("bot", None, "Записал: встреча с Ренатой")
    assert line == "Ответ на сообщение бота: «Записал: встреча с Ренатой»"


def test_swipe_on_an_own_message_names_the_task_or_carries_the_text() -> None:
    assert edits.swipe_line("own", 2, "встреча завтра") == "Ответ на своё сообщение о задаче №2"
    assert edits.swipe_line("own", None, "встреча завтра") == (
        "Ответ на своё сообщение: «встреча завтра»"
    )


def test_swipe_text_is_cut_at_two_hundred_characters() -> None:
    line = edits.swipe_line("bot", None, "а" * 250)

    assert line == f"Ответ на сообщение бота: «{'а' * 200}…»"


@pytest.mark.parametrize("target", ["reminder", "bot", "own"])
@pytest.mark.parametrize("text", [None, "", "   "])
def test_swipe_without_text_or_number_is_no_line(
    target: edits.SwipeTarget, text: str | None
) -> None:
    assert edits.swipe_line(target, None, text) is None


# --- правка для базы ---------------------------------------------------------


def test_move_to_a_day_is_sent_as_a_date_in_the_owner_timezone() -> None:
    task = make_task(due_at=datetime(2026, 9, 30, 18, 0, tzinfo=TZ), due_precision="day")
    # 13:00 UTC — это 18:00 у владельца: день берётся в его поясе.
    edit = make_edit(due_at="2026-10-02T13:00:00+00:00", due_precision="day")

    change = edits.edit_changes(task, edit, TZ)

    assert change.changes == {"due_date": "2026-10-02"}
    assert change.due_at == datetime(2026, 10, 2, 18, 0, tzinfo=TZ)
    assert change.due_precision == "day"
    assert change.due_changed


def test_move_to_an_hour_is_sent_with_the_offset() -> None:
    task = make_task(due_at=datetime(2026, 9, 29, 15, 0, tzinfo=TZ), due_precision="time")
    edit = make_edit(due_at="2026-09-29T17:00:00+05:00", due_precision="time")

    change = edits.edit_changes(task, edit, TZ)

    assert change.changes == {"due_at": "2026-09-29T17:00:00+05:00"}
    assert change.due_at == datetime(2026, 9, 29, 17, 0, tzinfo=TZ)
    assert change.due_precision == "time"
    assert change.due_changed


def test_hour_without_a_zone_is_read_in_the_owner_timezone() -> None:
    edit = make_edit(due_at="2026-09-29T17:00:00", due_precision="time")

    change = edits.edit_changes(make_task(), edit, TZ)

    assert change.changes == {"due_at": "2026-09-29T17:00:00+05:00"}


def test_same_day_or_same_hour_is_no_change() -> None:
    by_day = make_task(due_at=datetime(2026, 10, 2, 18, 0, tzinfo=TZ), due_precision="day")
    by_hour = make_task(due_at=datetime(2026, 10, 2, 17, 0, tzinfo=TZ), due_precision="time")

    same_day = edits.edit_changes(
        by_day, make_edit(due_at="2026-10-02T18:00:00+05:00", due_precision="day"), TZ
    )
    same_hour = edits.edit_changes(
        by_hour, make_edit(due_at="2026-10-02T12:00:00+00:00", due_precision="time"), TZ
    )

    assert same_day.changes == {}
    assert not same_day.due_changed
    assert same_hour.changes == {}
    assert not same_hour.due_changed


def test_hour_on_the_same_day_is_a_move_from_day_to_time() -> None:
    task = make_task(due_at=datetime(2026, 10, 2, 18, 0, tzinfo=TZ), due_precision="day")
    edit = make_edit(due_at="2026-10-02T18:00:00+05:00", due_precision="time")

    assert edits.edit_changes(task, edit, TZ).changes == {"due_at": "2026-10-02T18:00:00+05:00"}


def test_removing_the_due_sends_null() -> None:
    task = make_task(due_at=datetime(2026, 10, 2, 18, 0, tzinfo=TZ), due_precision="day")

    change = edits.edit_changes(task, make_edit(due_removed=True), TZ)

    assert change.changes == {"due_at": None}
    assert change.due_at is None
    assert change.due_precision is None
    assert change.due_changed


def test_removing_a_due_that_is_not_there_is_no_change() -> None:
    change = edits.edit_changes(make_task(), make_edit(due_removed=True), TZ)

    assert change.changes == {}
    assert not change.due_changed


def test_new_due_wins_over_a_contradicting_removal() -> None:
    edit = make_edit(due_removed=True, due_at="2026-10-02T17:00:00+05:00", due_precision="time")

    assert edits.edit_changes(make_task(), edit, TZ).changes == {
        "due_at": "2026-10-02T17:00:00+05:00"
    }


def test_only_fields_that_differ_are_sent() -> None:
    task = make_task(
        title="позвонить Кузнецову",
        priority="high",
        promise="mine",
        people=("Кузнецов",),
    )
    edit = make_edit(
        title="позвонить Петрову",
        priority="normal",
        promise="mine",
        people=["Петров"],
    )

    change = edits.edit_changes(task, edit, TZ)

    assert change.changes == {
        "title": "позвонить Петрову",
        "priority": "normal",
        "people": ["Петров"],
    }
    assert change.title == "позвонить Петрову"
    assert change.priority == "normal"
    assert change.people == ("Петров",)
    assert not change.due_changed


def test_same_or_empty_values_are_not_changes() -> None:
    task = make_task(title="позвонить Кузнецову", priority="high", people=("Кузнецов",))
    edit = make_edit(title="  позвонить Кузнецову ", priority="high", people=["Кузнецов"])

    change = edits.edit_changes(task, edit, TZ)

    assert change.changes == {}
    assert change.title == "позвонить Кузнецову"
    assert change.people == ("Кузнецов",)
    assert edits.edit_changes(task, make_edit(title="   "), TZ).changes == {}


def test_people_are_replaced_whole_even_by_an_empty_list() -> None:
    task = make_task(people=("Кузнецов", "Рената"))

    assert edits.edit_changes(task, make_edit(people=[]), TZ).changes == {"people": []}


def test_task_without_changes_keeps_its_due() -> None:
    due = datetime(2026, 10, 2, 18, 0, tzinfo=TZ)
    change = edits.edit_changes(make_task(due_at=due, due_precision="day"), make_edit(), TZ)

    assert change.changes == {}
    assert change.due_at == due
    assert change.due_precision == "day"


# --- кнопки ------------------------------------------------------------------


def test_pick_callback_fits_telegram_and_reads_back() -> None:
    data = edits.pick_data(9_999_999_999, TASK_ID)

    assert data == f"pick:9999999999:{TASK_ID}"
    assert len(data.encode()) <= 64
    assert edits.parse_pick(data) == (9_999_999_999, TASK_ID)


def test_reopen_callback_fits_telegram_and_reads_back() -> None:
    data = edits.reopen_data(TASK_ID)

    assert data == f"reopen:{TASK_ID}"
    assert len(data.encode()) <= 64
    assert edits.parse_reopen(data) == TASK_ID


@pytest.mark.parametrize(
    "data",
    ["pick:", "pick:12", f"pick:x:{TASK_ID}", "pick:12:not-a-uuid", f"reopen:{TASK_ID}", ""],
)
def test_broken_pick_callback_is_nothing(data: str) -> None:
    assert edits.parse_pick(data) is None


@pytest.mark.parametrize("data", ["reopen:", "reopen:not-a-uuid", f"pick:1:{TASK_ID}", ""])
def test_broken_reopen_callback_is_nothing(data: str) -> None:
    assert edits.parse_reopen(data) is None


def test_candidate_button_carries_the_title_and_a_short_due() -> None:
    by_hour = make_task(due_at=datetime(2026, 10, 2, 17, 0, tzinfo=TZ), due_precision="time")
    by_day = make_task(due_at=datetime(2026, 10, 2, 18, 0, tzinfo=TZ), due_precision="day")

    assert edits.candidate_label(by_hour, TZ) == "встреча с Ренатой — 2 окт, 17:00"
    assert edits.candidate_label(by_day, TZ) == "встреча с Ренатой — 2 окт"
    assert edits.candidate_label(make_task(), TZ) == "встреча с Ренатой"


def test_candidate_button_cuts_a_long_title_at_forty() -> None:
    label = edits.candidate_label(make_task(title="о" * 60), TZ)

    assert label == f"{'о' * 40}…"


def test_pick_question_names_the_action() -> None:
    assert edits.pick_question(make_edit(action="done"), NOW, TZ) == "Какую задачу закрыть?"
    assert edits.pick_question(make_edit(action="cancel"), NOW, TZ) == (
        "Какую задачу убрать из списка?"
    )
    assert edits.pick_question(make_edit(due_removed=True), NOW, TZ) == (
        "С какой задачи снять срок?"
    )
    assert edits.pick_question(make_edit(title="позвонить Петрову"), NOW, TZ) == (
        "Какую задачу поправить?"
    )


def test_pick_question_about_a_move_names_the_new_due_in_the_accusative() -> None:
    friday = make_edit(due_at="2026-10-02T18:00:00+05:00", due_precision="day")
    today = make_edit(due_at="2026-09-29T17:00:00+05:00", due_precision="time")
    wednesday = make_edit(due_at="2026-09-30T09:00:00+05:00", due_precision="time")
    sunday = make_edit(due_at="2026-10-04T18:00:00+05:00", due_precision="day")

    assert edits.pick_question(friday, NOW, TZ) == "Какую задачу перенести на пятницу, 2 октября?"
    assert edits.pick_question(today, NOW, TZ) == "Какую задачу перенести на сегодня в 17:00?"
    assert edits.pick_question(wednesday, NOW, TZ) == (
        "Какую задачу перенести на среду, 30 сентября, в 09:00?"
    )
    assert edits.pick_question(sunday, NOW, TZ) == (
        "Какую задачу перенести на воскресенье, 4 октября?"
    )
