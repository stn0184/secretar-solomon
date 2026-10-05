"""Правка словом — чистые функции (`techspec/12-chat-edit.md`).

Нумерация списка в промпте, номер модели → задача, подсказки свайпа и
последней задачи, правка для базы из разбора, кнопки кандидатов. Сети и
базы здесь нет вовсе: всё, что решает бот, решается этими функциями.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from solomon import texts
from solomon.db.tasks import TaskDetails, TaskEvent
from solomon.services import edits
from solomon.services.understanding import TaskEdit
from tests.conftest import OWNER_TIMEZONE

TZ = ZoneInfo(OWNER_TIMEZONE)
# Вторник, 29 сентября 2026, полдень в поясе владельца (+05:00).
NOW = datetime(2026, 9, 29, 12, 0, tzinfo=TZ)
TASK_ID = "5b0c7a52-8f3e-4c1d-9a6b-2e4f1d3c8b90"
OTHER_ID = "6c1d8b63-9f4e-4d2e-8b7c-3f5e2d4c9a01"


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
    repeat: dict[str, Any] | None = None,
    occurrence_at: datetime | None = None,
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
        repeat=repeat,
        occurrence_at=occurrence_at,
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
        "time_removed": False,
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


# --- последние задачи в разговоре -------------------------------------------


def test_last_task_is_the_later_of_the_message_and_the_reminder() -> None:
    numbered = edits.number_tasks([make_task("a"), make_task("b", created_at=NOW)])
    message = TaskEvent(task_id="a", at=NOW - timedelta(minutes=30))
    reminder = TaskEvent(task_id="b", at=NOW - timedelta(minutes=10))

    assert edits.last_task_numbers([message, reminder], numbered, NOW) == [1]
    assert edits.last_task_numbers([reminder, message], numbered, NOW) == [1]
    earlier_reminder = TaskEvent(task_id="b", at=NOW - timedelta(minutes=50))
    assert edits.last_task_numbers([message, earlier_reminder], numbered, NOW) == [2]


def test_last_task_older_than_an_hour_is_not_named() -> None:
    numbered = edits.number_tasks([make_task("a")])

    fresh = TaskEvent(task_id="a", at=NOW - timedelta(minutes=59))
    stale = TaskEvent(task_id="a", at=NOW - timedelta(minutes=61))

    assert edits.last_task_numbers([fresh], numbered, NOW) == [1]
    assert edits.last_task_numbers([stale], numbered, NOW) == []
    assert edits.last_task_numbers([None, None], numbered, NOW) == []


def test_later_event_about_a_closed_task_hides_the_earlier_one() -> None:
    """Задачи позднего события нет в списке — строки нет, к раннему не откатываемся."""
    numbered = edits.number_tasks([make_task("a")])
    message = TaskEvent(task_id="a", at=NOW - timedelta(minutes=40))
    reminder = TaskEvent(task_id="closed", at=NOW - timedelta(minutes=5))

    assert edits.last_task_numbers([message, reminder], numbered, NOW) == []


def test_message_about_several_tasks_names_all_of_them_in_order() -> None:
    """Сообщение о нескольких делах (`techspec/23-several-tasks.md` §23.2):
    номера всех его задач по порядку дел; закрытой среди них нет."""
    numbered = edits.number_tasks(
        [make_task("a"), make_task("b", created_at=NOW), make_task("c", created_at=NOW)]
    )
    message = TaskEvent(task_id="c", at=NOW - timedelta(minutes=5), more=("closed", "a"))

    assert edits.last_task_numbers([message], numbered, NOW) == [
        edits.number_of(numbered, "c"),
        edits.number_of(numbered, "a"),
    ]


# --- свайп -------------------------------------------------------------------


def test_swipe_on_a_reminder_names_the_task_number() -> None:
    line = edits.swipe_line("reminder", [3], "Напоминаю: встреча с Ренатой")
    assert line == "Ответ на напоминание о задаче №3"


def test_swipe_on_a_reminder_of_a_task_outside_the_list_carries_the_text() -> None:
    line = edits.swipe_line("reminder", [], "Напоминаю: встреча с Ренатой")
    assert line == "Ответ на напоминание: «Напоминаю: встреча с Ренатой»"


def test_swipe_on_another_bot_message_carries_its_text() -> None:
    line = edits.swipe_line("bot", [], "Записал: встреча с Ренатой")
    assert line == "Ответ на сообщение бота: «Записал: встреча с Ренатой»"


def test_swipe_on_an_own_message_names_the_task_or_carries_the_text() -> None:
    assert edits.swipe_line("own", [2], "встреча завтра") == "Ответ на своё сообщение о задаче №2"
    assert edits.swipe_line("own", [], "встреча завтра") == (
        "Ответ на своё сообщение: «встреча завтра»"
    )


def test_swipe_on_an_own_message_about_several_tasks_names_them_all() -> None:
    """Своё сообщение о нескольких делах (§23.2) — «о задачах №A, №B»."""
    line = edits.swipe_line("own", [4, 1, 2], "позвонить Игорю и забрать костюм")

    assert line == "Ответ на своё сообщение о задачах №4, №1, №2"


def test_swipe_text_is_cut_at_two_hundred_characters() -> None:
    line = edits.swipe_line("bot", [], "а" * 250)

    assert line == f"Ответ на сообщение бота: «{'а' * 200}…»"


@pytest.mark.parametrize("target", ["reminder", "bot", "own"])
@pytest.mark.parametrize("text", [None, "", "   "])
def test_swipe_without_text_or_number_is_no_line(
    target: edits.SwipeTarget, text: str | None
) -> None:
    assert edits.swipe_line(target, [], text) is None


# --- правка для базы ---------------------------------------------------------


def test_move_to_a_day_is_sent_as_a_date_in_the_owner_timezone() -> None:
    task = make_task(due_at=datetime(2026, 9, 30, 18, 0, tzinfo=TZ), due_precision="day")
    # 13:00 UTC — это 18:00 у владельца: день берётся в его поясе.
    edit = make_edit(due_at="2026-10-02T13:00:00+00:00", due_precision="day")

    change = edits.edit_changes(task, edit, TZ, NOW)

    assert change.changes == {"due_date": "2026-10-02"}
    assert change.due_at == datetime(2026, 10, 2, 18, 0, tzinfo=TZ)
    assert change.due_precision == "day"
    assert change.due_changed


def test_move_to_an_hour_is_sent_with_the_offset() -> None:
    task = make_task(due_at=datetime(2026, 9, 29, 15, 0, tzinfo=TZ), due_precision="time")
    edit = make_edit(due_at="2026-09-29T17:00:00+05:00", due_precision="time")

    change = edits.edit_changes(task, edit, TZ, NOW)

    assert change.changes == {"due_at": "2026-09-29T17:00:00+05:00"}
    assert change.due_at == datetime(2026, 9, 29, 17, 0, tzinfo=TZ)
    assert change.due_precision == "time"
    assert change.due_changed


def test_hour_without_a_zone_is_read_in_the_owner_timezone() -> None:
    edit = make_edit(due_at="2026-09-29T17:00:00", due_precision="time")

    change = edits.edit_changes(make_task(), edit, TZ, NOW)

    assert change.changes == {"due_at": "2026-09-29T17:00:00+05:00"}


def test_same_day_or_same_hour_is_no_change() -> None:
    by_day = make_task(due_at=datetime(2026, 10, 2, 18, 0, tzinfo=TZ), due_precision="day")
    by_hour = make_task(due_at=datetime(2026, 10, 2, 17, 0, tzinfo=TZ), due_precision="time")

    same_day = edits.edit_changes(
        by_day, make_edit(due_at="2026-10-02T18:00:00+05:00", due_precision="day"), TZ, NOW
    )
    same_hour = edits.edit_changes(
        by_hour, make_edit(due_at="2026-10-02T12:00:00+00:00", due_precision="time"), TZ, NOW
    )

    assert same_day.changes == {}
    assert not same_day.due_changed
    assert same_hour.changes == {}
    assert not same_hour.due_changed


def test_hour_on_the_same_day_is_a_move_from_day_to_time() -> None:
    task = make_task(due_at=datetime(2026, 10, 2, 18, 0, tzinfo=TZ), due_precision="day")
    edit = make_edit(due_at="2026-10-02T18:00:00+05:00", due_precision="time")

    assert edits.edit_changes(task, edit, TZ, NOW).changes == {
        "due_at": "2026-10-02T18:00:00+05:00"
    }


# Часть дня (`techspec/21-part-of-day.md` §21.2): срок уходит моментом начала
# части и ключом `due_precision`; та же часть того же дня — не правка.


def test_move_to_a_part_of_day_sends_its_start_and_the_part() -> None:
    task = make_task(due_at=datetime(2026, 9, 29, 15, 0, tzinfo=TZ), due_precision="time")
    edit = make_edit(due_at="2026-09-30T08:00:00+05:00", due_precision="morning")

    change = edits.edit_changes(task, edit, TZ, NOW)

    assert change.changes == {
        "due_at": "2026-09-30T08:00:00+05:00",
        "due_precision": "morning",
    }
    assert change.due_at == datetime(2026, 9, 30, 8, 0, tzinfo=TZ)
    assert change.due_precision == "morning"
    assert change.due_changed


def test_same_part_of_the_same_day_is_no_change() -> None:
    task = make_task(due_at=datetime(2026, 9, 30, 18, 0, tzinfo=TZ), due_precision="evening")
    # 13:00 UTC — это 18:00 у владельца: тот же вечер.
    edit = make_edit(due_at="2026-09-30T13:00:00+00:00", due_precision="evening")

    change = edits.edit_changes(task, edit, TZ, NOW)

    assert change.changes == {}
    assert change.due_precision == "evening"
    assert not change.due_changed


def test_evening_keeps_six_o_clock_and_six_o_clock_moves_an_evening() -> None:
    """18:00 лежит в вечере (§12.8): «вечером» у дела на 18:00 — тот же срок;
    а 18:00 у дела на вечер — час, хоть и в ту же минуту."""
    by_hour = make_task(due_at=datetime(2026, 9, 30, 18, 0, tzinfo=TZ), due_precision="time")
    evening = make_task(due_at=datetime(2026, 9, 30, 18, 0, tzinfo=TZ), due_precision="evening")

    to_evening = edits.edit_changes(
        by_hour, make_edit(due_at="2026-09-30T18:00:00+05:00", due_precision="evening"), TZ, NOW
    )
    to_hour = edits.edit_changes(
        evening, make_edit(due_at="2026-09-30T18:00:00+05:00", due_precision="time"), TZ, NOW
    )

    assert to_evening.changes == {}
    assert to_evening.due_precision == "time"
    assert to_evening.named
    assert to_hour.changes == {"due_at": "2026-09-30T18:00:00+05:00"}
    assert to_hour.due_precision == "time"


# Прежний час при переносе (`techspec/12-chat-edit.md` §12.8): назван только
# день — час и часть остаются; названа часть — час остаётся, если лежит в ней;
# `time_removed` снимает час; прежний час на сегодня прошёл — срок как назван.

# Пятница, 2 октября, 17:30 у владельца.
FRIDAY_1730 = datetime(2026, 10, 2, 17, 30, tzinfo=TZ)
# «На понедельник» — день: час модели у дня бот не читает.
MONDAY = "2026-10-05T00:00:00+05:00"


def test_day_only_keeps_the_hour_with_its_minutes() -> None:
    task = make_task(due_at=FRIDAY_1730, due_precision="time")

    change = edits.edit_changes(task, make_edit(due_at=MONDAY, due_precision="day"), TZ, NOW)

    assert change.changes == {"due_at": "2026-10-05T17:30:00+05:00"}
    assert change.due_at == datetime(2026, 10, 5, 17, 30, tzinfo=TZ)
    assert change.due_precision == "time"
    assert change.due_changed
    assert change.lost_at is None
    assert change.lost_precision is None


def test_day_only_takes_the_hour_by_the_owner_clock() -> None:
    """Задача из базы приходит в UTC: час берётся по часам владельца."""
    task = make_task(due_at=datetime(2026, 10, 2, 12, 30, tzinfo=UTC), due_precision="time")
    # 19:00 UTC 4 октября — уже понедельник, 00:00, у владельца.
    edit = make_edit(due_at="2026-10-04T19:00:00+00:00", due_precision="day")

    change = edits.edit_changes(task, edit, TZ, NOW)

    assert change.changes == {"due_at": "2026-10-05T17:30:00+05:00"}


def test_day_only_keeps_the_clock_hour_across_a_clock_change() -> None:
    """17:30 на часах остаётся 17:30, а не тем же числом часов от прежнего срока."""
    berlin = ZoneInfo("Europe/Berlin")
    task = make_task(due_at=datetime(2026, 10, 24, 17, 30, tzinfo=berlin), due_precision="time")
    edit = make_edit(due_at="2026-10-26T00:00:00+01:00", due_precision="day")

    change = edits.edit_changes(task, edit, berlin, NOW)

    # 24 октября — летнее +02:00, 26-го — зимнее +01:00.
    assert change.changes == {"due_at": "2026-10-26T17:30:00+01:00"}


def test_day_only_keeps_the_part_of_day() -> None:
    task = make_task(due_at=datetime(2026, 9, 30, 8, 0, tzinfo=TZ), due_precision="morning")

    change = edits.edit_changes(task, make_edit(due_at=MONDAY, due_precision="day"), TZ, NOW)

    assert change.changes == {
        "due_at": "2026-10-05T08:00:00+05:00",
        "due_precision": "morning",
    }
    assert change.due_at == datetime(2026, 10, 5, 8, 0, tzinfo=TZ)
    assert change.due_precision == "morning"


def test_same_day_of_a_part_task_is_no_change() -> None:
    """«Перенеси на среду» у дела на утро среды — так и записано."""
    task = make_task(due_at=datetime(2026, 9, 30, 8, 0, tzinfo=TZ), due_precision="morning")
    edit = make_edit(due_at="2026-09-30T18:00:00+05:00", due_precision="day")

    change = edits.edit_changes(task, edit, TZ, NOW)

    assert change.changes == {}
    assert change.due_precision == "morning"
    assert not change.due_changed
    assert change.named


def test_day_only_of_a_day_task_or_a_task_without_due_is_a_day() -> None:
    by_day = make_task(due_at=datetime(2026, 9, 30, 18, 0, tzinfo=TZ), due_precision="day")
    edit = make_edit(due_at=MONDAY, due_precision="day")

    assert edits.edit_changes(by_day, edit, TZ, NOW).changes == {"due_date": "2026-10-05"}
    undated = edits.edit_changes(make_task(), edit, TZ, NOW)
    assert undated.changes == {"due_date": "2026-10-05"}
    assert undated.due_precision == "day"


def test_same_day_of_a_task_with_an_hour_is_no_change_but_named() -> None:
    """«На понедельник» у дела на понедельник, 17:00 — меняться нечему."""
    task = make_task(due_at=datetime(2026, 10, 5, 17, 0, tzinfo=TZ), due_precision="time")

    change = edits.edit_changes(task, make_edit(due_at=MONDAY, due_precision="day"), TZ, NOW)

    assert change.changes == {}
    assert change.due_at == datetime(2026, 10, 5, 17, 0, tzinfo=TZ)
    assert change.due_precision == "time"
    assert not change.due_changed
    assert change.named


def test_part_keeps_the_hour_that_lies_in_it() -> None:
    task = make_task(due_at=datetime(2026, 10, 2, 19, 0, tzinfo=TZ), due_precision="time")
    edit = make_edit(due_at="2026-10-05T18:00:00+05:00", due_precision="evening")

    change = edits.edit_changes(task, edit, TZ, NOW)

    assert change.changes == {"due_at": "2026-10-05T19:00:00+05:00"}
    assert change.due_precision == "time"


def test_part_without_the_hour_in_it_starts_at_the_part() -> None:
    task = make_task(due_at=datetime(2026, 10, 2, 17, 0, tzinfo=TZ), due_precision="time")
    # Час модели у части бот не читает: начало части — из `parts`.
    edit = make_edit(due_at="2026-10-05T20:00:00+05:00", due_precision="evening")

    change = edits.edit_changes(task, edit, TZ, NOW)

    assert change.changes == {
        "due_at": "2026-10-05T18:00:00+05:00",
        "due_precision": "evening",
    }
    assert change.due_precision == "evening"


def test_morning_begins_at_midnight_for_the_prior_hour() -> None:
    task = make_task(due_at=datetime(2026, 10, 2, 6, 30, tzinfo=TZ), due_precision="time")
    edit = make_edit(due_at="2026-10-05T08:00:00+05:00", due_precision="morning")

    assert edits.edit_changes(task, edit, TZ, NOW).changes == {
        "due_at": "2026-10-05T06:30:00+05:00"
    }


def test_part_of_a_part_task_is_the_named_part() -> None:
    morning = make_task(due_at=datetime(2026, 10, 2, 8, 0, tzinfo=TZ), due_precision="morning")
    edit = make_edit(due_at="2026-10-05T18:00:00+05:00", due_precision="evening")

    assert edits.edit_changes(morning, edit, TZ, NOW).changes == {
        "due_at": "2026-10-05T18:00:00+05:00",
        "due_precision": "evening",
    }


def test_removing_the_time_with_a_day_makes_a_day() -> None:
    task = make_task(due_at=FRIDAY_1730, due_precision="time")
    edit = make_edit(due_at=MONDAY, due_precision="day", time_removed=True)

    change = edits.edit_changes(task, edit, TZ, NOW)

    assert change.changes == {"due_date": "2026-10-05"}
    assert change.due_at == datetime(2026, 10, 5, 18, 0, tzinfo=TZ)
    assert change.due_precision == "day"


def test_removing_the_time_alone_keeps_the_day() -> None:
    by_hour = make_task(due_at=FRIDAY_1730, due_precision="time")
    morning = make_task(due_at=datetime(2026, 10, 2, 8, 0, tzinfo=TZ), due_precision="morning")

    assert edits.edit_changes(by_hour, make_edit(time_removed=True), TZ, NOW).changes == {
        "due_date": "2026-10-02"
    }
    assert edits.edit_changes(morning, make_edit(time_removed=True), TZ, NOW).changes == {
        "due_date": "2026-10-02"
    }


def test_removing_the_time_with_a_part_makes_the_part() -> None:
    task = make_task(due_at=datetime(2026, 10, 2, 19, 0, tzinfo=TZ), due_precision="time")
    edit = make_edit(due_at="2026-10-05T18:00:00+05:00", due_precision="evening", time_removed=True)

    assert edits.edit_changes(task, edit, TZ, NOW).changes == {
        "due_at": "2026-10-05T18:00:00+05:00",
        "due_precision": "evening",
    }


def test_named_hour_wins_over_removing_the_time() -> None:
    task = make_task(due_at=FRIDAY_1730, due_precision="time")
    edit = make_edit(due_at="2026-10-05T11:00:00+05:00", due_precision="time", time_removed=True)

    assert edits.edit_changes(task, edit, TZ, NOW).changes == {
        "due_at": "2026-10-05T11:00:00+05:00"
    }


def test_removing_the_time_of_a_day_or_undated_task_is_no_change_but_named() -> None:
    by_day = make_task(due_at=datetime(2026, 10, 2, 18, 0, tzinfo=TZ), due_precision="day")

    for task in (by_day, make_task()):
        change = edits.edit_changes(task, make_edit(time_removed=True), TZ, NOW)
        assert change.changes == {}
        assert change.named


def test_removing_the_time_wins_over_removing_the_due() -> None:
    """Из противоречивых значений — то, что теряет меньше (§12.4)."""
    task = make_task(due_at=FRIDAY_1730, due_precision="time")
    edit = make_edit(time_removed=True, due_removed=True)

    assert edits.edit_changes(task, edit, TZ, NOW).changes == {"due_date": "2026-10-02"}


# Час уже прошёл (§12.8): NOW — вторник, 29 сентября, 12:00.
TODAY = "2026-09-29T00:00:00+05:00"


def test_today_with_a_passed_hour_is_the_day_and_names_the_lost_hour() -> None:
    task = make_task(due_at=datetime(2026, 9, 30, 9, 0, tzinfo=TZ), due_precision="time")

    change = edits.edit_changes(task, make_edit(due_at=TODAY, due_precision="day"), TZ, NOW)

    assert change.changes == {"due_date": "2026-09-29"}
    assert change.due_precision == "day"
    assert change.lost_at == datetime(2026, 9, 29, 9, 0, tzinfo=TZ)
    assert change.lost_precision == "time"


def test_hour_that_comes_right_now_has_passed() -> None:
    task = make_task(due_at=datetime(2026, 9, 30, 12, 0, tzinfo=TZ), due_precision="time")

    change = edits.edit_changes(task, make_edit(due_at=TODAY, due_precision="day"), TZ, NOW)

    assert change.changes == {"due_date": "2026-09-29"}
    assert change.lost_precision == "time"


def test_today_with_an_hour_ahead_keeps_it() -> None:
    task = make_task(due_at=datetime(2026, 9, 30, 15, 0, tzinfo=TZ), due_precision="time")

    change = edits.edit_changes(task, make_edit(due_at=TODAY, due_precision="day"), TZ, NOW)

    assert change.changes == {"due_at": "2026-09-29T15:00:00+05:00"}
    assert change.lost_at is None


def test_today_after_the_part_ended_is_the_day() -> None:
    morning = make_task(due_at=datetime(2026, 9, 30, 8, 0, tzinfo=TZ), due_precision="morning")
    afternoon = make_task(due_at=datetime(2026, 9, 30, 12, 0, tzinfo=TZ), due_precision="afternoon")
    evening = make_task(due_at=datetime(2026, 9, 30, 18, 0, tzinfo=TZ), due_precision="evening")
    edit = make_edit(due_at=TODAY, due_precision="day")
    late = datetime(2026, 9, 29, 23, 0, tzinfo=TZ)

    after_morning = edits.edit_changes(morning, edit, TZ, NOW)
    in_afternoon = edits.edit_changes(afternoon, edit, TZ, NOW)
    after_afternoon = edits.edit_changes(afternoon, edit, TZ, late)
    late_evening = edits.edit_changes(evening, edit, TZ, late)

    assert after_morning.changes == {"due_date": "2026-09-29"}
    assert after_morning.lost_precision == "morning"
    assert in_afternoon.changes == {
        "due_at": "2026-09-29T12:00:00+05:00",
        "due_precision": "afternoon",
    }
    assert in_afternoon.lost_precision is None
    assert after_afternoon.changes == {"due_date": "2026-09-29"}
    assert after_afternoon.lost_precision == "afternoon"
    # Вечер кончается в полночь и прошедшим не бывает.
    assert late_evening.changes == {
        "due_at": "2026-09-29T18:00:00+05:00",
        "due_precision": "evening",
    }
    assert late_evening.lost_precision is None


def test_today_evening_with_a_passed_hour_in_it_is_the_evening() -> None:
    task = make_task(due_at=datetime(2026, 9, 30, 19, 0, tzinfo=TZ), due_precision="time")
    edit = make_edit(due_at="2026-09-29T18:00:00+05:00", due_precision="evening")
    now = datetime(2026, 9, 29, 20, 0, tzinfo=TZ)

    change = edits.edit_changes(task, edit, TZ, now)

    assert change.changes == {
        "due_at": "2026-09-29T18:00:00+05:00",
        "due_precision": "evening",
    }
    assert change.lost_at == datetime(2026, 9, 29, 19, 0, tzinfo=TZ)
    assert change.lost_precision == "time"


def test_day_in_the_past_keeps_the_hour_without_a_question() -> None:
    """Перенос на день в прошлом — правило «час уже прошёл» его не касается."""
    task = make_task(due_at=datetime(2026, 9, 30, 9, 0, tzinfo=TZ), due_precision="time")
    edit = make_edit(due_at="2026-09-28T00:00:00+05:00", due_precision="day")

    change = edits.edit_changes(task, edit, TZ, NOW)

    assert change.changes == {"due_at": "2026-09-28T09:00:00+05:00"}
    assert change.lost_at is None


def test_named_hour_today_stays_even_if_passed() -> None:
    """«На сегодня в 9» — час назван: прежнего бот не держит, и вопроса нет."""
    task = make_task(due_at=datetime(2026, 9, 30, 15, 0, tzinfo=TZ), due_precision="time")
    edit = make_edit(due_at="2026-09-29T09:00:00+05:00", due_precision="time")

    change = edits.edit_changes(task, edit, TZ, NOW)

    assert change.changes == {"due_at": "2026-09-29T09:00:00+05:00"}
    assert change.lost_at is None


def test_part_of_day_of_a_repeating_task_moves_only_this_time() -> None:
    """Вечер у планёрки в 9: час не в вечере — этот раз уходит на вечер (§12.8)."""
    edit = make_edit(due_at="2026-10-06T18:00:00+05:00", due_precision="evening")

    change = edits.edit_changes(weekly(), edit, TZ, NOW)

    assert change.changes == {
        "due_at": "2026-10-06T18:00:00+05:00",
        "due_precision": "evening",
    }
    assert change.repeat == {**MONDAYS, "time": "09:00"}
    assert not change.repeat_changed


def test_removing_the_due_sends_null() -> None:
    task = make_task(due_at=datetime(2026, 10, 2, 18, 0, tzinfo=TZ), due_precision="day")

    change = edits.edit_changes(task, make_edit(due_removed=True), TZ, NOW)

    assert change.changes == {"due_at": None}
    assert change.due_at is None
    assert change.due_precision is None
    assert change.due_changed


def test_removing_a_due_that_is_not_there_is_no_change() -> None:
    change = edits.edit_changes(make_task(), make_edit(due_removed=True), TZ, NOW)

    assert change.changes == {}
    assert not change.due_changed


def test_new_due_wins_over_a_contradicting_removal() -> None:
    edit = make_edit(due_removed=True, due_at="2026-10-02T17:00:00+05:00", due_precision="time")

    assert edits.edit_changes(make_task(), edit, TZ, NOW).changes == {
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

    change = edits.edit_changes(task, edit, TZ, NOW)

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

    change = edits.edit_changes(task, edit, TZ, NOW)

    assert change.changes == {}
    assert change.title == "позвонить Кузнецову"
    assert change.people == ("Кузнецов",)
    assert edits.edit_changes(task, make_edit(title="   "), TZ, NOW).changes == {}


def test_people_are_replaced_whole_even_by_an_empty_list() -> None:
    task = make_task(people=("Кузнецов", "Рената"))

    assert edits.edit_changes(task, make_edit(people=[]), TZ, NOW).changes == {"people": []}


def test_edit_that_names_nothing_is_not_named() -> None:
    task = make_task(kind="idea", due_at=FRIDAY_1730, due_precision="time")

    assert not edits.edit_changes(task, make_edit(), TZ, NOW).named
    assert not edits.edit_changes(task, make_edit(title="   "), TZ, NOW).named
    # Правило у идеи не ставится — значит, и не названо.
    assert not edits.edit_changes(task, make_edit(repeat=MONDAYS), TZ, NOW).named


@pytest.mark.parametrize(
    "fields",
    [
        {"title": "встреча с Ренатой"},
        {"priority": "normal"},
        {"promise": "mine"},
        {"people": []},
        {"repeat_removed": True},
        {"due_at": "2026-10-02T17:30:00+05:00", "due_precision": "time"},
    ],
)
def test_edit_that_names_the_same_value_is_named(fields: dict[str, Any]) -> None:
    """Правка назвала то, что уже записано, — «Так и записано» (§12.8)."""
    task = make_task(due_at=FRIDAY_1730, due_precision="time", promise="mine")

    change = edits.edit_changes(task, make_edit(**fields), TZ, NOW)

    assert change.named
    assert change.changes == {}


def test_removing_a_due_that_is_not_there_is_named() -> None:
    change = edits.edit_changes(make_task(), make_edit(due_removed=True), TZ, NOW)

    assert change.changes == {}
    assert change.named


def test_task_without_changes_keeps_its_due() -> None:
    due = datetime(2026, 10, 2, 18, 0, tzinfo=TZ)
    change = edits.edit_changes(make_task(due_at=due, due_precision="day"), make_edit(), TZ, NOW)

    assert change.changes == {}
    assert change.due_at == due
    assert change.due_precision == "day"


# --- кнопки ------------------------------------------------------------------


# --- повтор (§13.5) --------------------------------------------------------

MONDAY_9 = datetime(2026, 10, 5, 9, 0, tzinfo=TZ)
MONDAYS = {"every": "week", "interval": 1, "weekdays": [1], "month_day": None, "month": None}
TUESDAYS = {**MONDAYS, "weekdays": [2]}


def weekly(**fields: Any) -> TaskDetails:
    """Повторяющаяся задача: каждый понедельник в 9, стоит на разе 5 октября."""
    return make_task(
        due_at=MONDAY_9,
        due_precision="time",
        repeat={**MONDAYS, "time": "09:00"},
        occurrence_at=MONDAY_9,
        **fields,
    )


def test_move_of_a_repeating_task_changes_only_this_time() -> None:
    edit = make_edit(due_at="2026-10-06T11:00:00+05:00", due_precision="time")

    change = edits.edit_changes(weekly(), edit, TZ, NOW)

    assert change.changes == {"due_at": "2026-10-06T11:00:00+05:00"}
    assert change.repeat == {**MONDAYS, "time": "09:00"}
    assert not change.repeat_changed
    assert not change.repeat_removed


def test_move_of_a_repeating_task_to_a_day_keeps_its_hour() -> None:
    """«Планёрку перенеси на среду»: этот раз — в среду в 9, правило то же (§13.5)."""
    edit = make_edit(due_at="2026-10-07T00:00:00+05:00", due_precision="day")

    change = edits.edit_changes(weekly(), edit, TZ, NOW)

    assert change.changes == {"due_at": "2026-10-07T09:00:00+05:00"}
    assert change.due_precision == "time"
    assert change.repeat == {**MONDAYS, "time": "09:00"}
    assert not change.repeat_changed


def test_new_rule_goes_with_its_first_time() -> None:
    """«Теперь по вторникам»: правило и срок ближайшего вторника в час серии."""
    edit = make_edit(repeat=TUESDAYS, due_at="2026-10-06T09:00:00+05:00", due_precision="time")

    change = edits.edit_changes(weekly(), edit, TZ, NOW)

    assert change.changes == {"due_at": "2026-10-06T09:00:00+05:00", "repeat": TUESDAYS}
    assert change.repeat == TUESDAYS
    assert change.repeat_changed
    assert change.due_changed


def test_same_rule_with_a_new_hour_is_sent_again() -> None:
    """«Теперь в 11»: то же правило, срок в 11:00 — база возьмёт час серии из срока."""
    edit = make_edit(repeat=MONDAYS, due_at="2026-10-05T11:00:00+05:00", due_precision="time")

    change = edits.edit_changes(weekly(), edit, TZ, NOW)

    assert change.changes == {"due_at": "2026-10-05T11:00:00+05:00", "repeat": MONDAYS}
    assert change.repeat_changed


def test_same_rule_without_a_new_due_is_no_change() -> None:
    assert edits.edit_changes(weekly(), make_edit(repeat=MONDAYS), TZ, NOW).changes == {}


def test_rule_for_a_one_off_task_takes_its_due_as_the_first_time() -> None:
    """«Повторяй каждую неделю»: срок задачи и есть первый раз."""
    task = make_task(due_at=MONDAY_9, due_precision="time")

    change = edits.edit_changes(task, make_edit(repeat=MONDAYS), TZ, NOW)

    assert change.changes == {"repeat": MONDAYS}
    assert change.repeat == MONDAYS
    assert change.repeat_changed
    assert not change.due_changed


def test_rule_without_any_due_needs_the_first_day() -> None:
    change = edits.edit_changes(make_task(), make_edit(repeat=MONDAYS, title="планёрка"), TZ, NOW)

    assert change.needs_start
    assert "repeat" not in change.changes


def test_rule_out_of_form_or_of_an_idea_is_dropped() -> None:
    no_days = make_edit(repeat={**MONDAYS, "weekdays": []})
    idea = make_task(kind="idea", due_at=MONDAY_9, due_precision="time")

    assert edits.edit_changes(weekly(), no_days, TZ, NOW).changes == {}
    assert edits.edit_changes(idea, make_edit(repeat=MONDAYS), TZ, NOW).changes == {}
    assert not edits.edit_changes(idea, make_edit(repeat=MONDAYS), TZ, NOW).needs_start


def test_removing_the_repeat_sends_null() -> None:
    change = edits.edit_changes(weekly(), make_edit(repeat_removed=True), TZ, NOW)

    assert change.changes == {"repeat": None}
    assert change.repeat is None
    assert change.repeat_removed
    assert change.due_at == MONDAY_9


def test_removing_a_repeat_that_is_not_there_is_no_change() -> None:
    task = make_task(due_at=MONDAY_9, due_precision="time")

    assert edits.edit_changes(task, make_edit(repeat_removed=True), TZ, NOW).changes == {}


def test_new_rule_wins_over_removing_it_and_the_due() -> None:
    """Из противоречивых значений — то, что ничего не теряет (§12.4)."""
    edit = make_edit(repeat=TUESDAYS, repeat_removed=True, due_removed=True)

    assert edits.edit_changes(weekly(), edit, TZ, NOW).changes == {"repeat": TUESDAYS}


def test_removing_the_due_takes_the_repeat_along() -> None:
    change = edits.edit_changes(weekly(), make_edit(due_removed=True), TZ, NOW)

    assert change.changes == {"due_at": None}
    assert change.repeat is None


def test_skip_question_names_the_action() -> None:
    assert edits.pick_question(make_edit(action="skip", task=None), NOW, TZ) == texts.PICK_SKIP


def test_back_callback_fits_telegram_and_reads_back() -> None:
    """«Вернуть» повторяющейся: задача, раз «откуда» и раз «куда» — 63 байта (§13.3)."""
    data = edits.back_data(TASK_ID, 9_999_999_999, 9_999_999_999)

    assert data == f"back:{TASK_ID}:9999999999:9999999999"
    assert len(data.encode()) <= 64
    assert edits.parse_back(data) == (TASK_ID, 9_999_999_999, 9_999_999_999)


@pytest.mark.parametrize(
    "data",
    [
        "back:",
        f"back:{TASK_ID}",
        f"back:{TASK_ID}:1",
        f"back:{TASK_ID}:1:",
        f"back:{TASK_ID}:x:2",
        f"back:{TASK_ID}:-1:2",
        "back:not-a-uuid:1:2",
        f"reopen:{TASK_ID}",
        "",
    ],
)
def test_broken_back_callback_is_nothing(data: str) -> None:
    assert edits.parse_back(data) is None


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


def test_apart_callback_fits_telegram_and_reads_back() -> None:
    """«Записать отдельно» (§15.4): в callback — только сообщение владельца;
    без номера — дело номер 1 (`techspec/23-several-tasks.md` §23.5)."""
    data = edits.apart_data(9_999_999_999)

    assert data == "apart:9999999999"
    assert len(data.encode()) <= 64
    assert edits.parse_apart(data) == (9_999_999_999, 1)


def test_apart_callback_with_the_task_number_reads_back() -> None:
    """Под ответом о нескольких делах — номер дела в сообщении (§23.5)."""
    data = edits.apart_data(9_999_999_999, 10)

    assert data == "apart:9999999999:10"
    assert len(data.encode()) <= 64
    assert edits.parse_apart(data) == (9_999_999_999, 10)
    assert edits.parse_apart("apart:41:2") == (41, 2)


@pytest.mark.parametrize(
    "data",
    [
        "apart:",
        "apart:x",
        "apart:-1",
        "apart:١٢",
        "apart:1:",
        "apart:1:0",
        "apart:1:11",
        "apart:1:x",
        "apart:1:-2",
        "apart:1:٢",
        "apart:1:2:3",
        f"reopen:{TASK_ID}",
        "",
    ],
)
def test_broken_apart_callback_is_nothing(data: str) -> None:
    assert edits.parse_apart(data) is None


def test_apart_label_names_the_task_when_there_are_several() -> None:
    assert edits.apart_label("созвон с Ренатой") == "Записать отдельно: созвон с Ренатой"
    assert edits.apart_label("а" * 41) == "Записать отдельно: " + "а" * 40 + "…"


def test_buttons_under_several_tasks_point_to_the_message() -> None:
    """«Вернуть» под ответом о нескольких делах — номер сообщения владельца
    вместо задачи (§23.5): итог дописывается к его ответу."""
    assert edits.bound_to_message(edits.reopen_data(TASK_ID), 41) == "reopen:41"
    back = edits.bound_to_message(edits.back_data(TASK_ID, 9_999_999_999, 9_999_999_999), 41)
    assert back == "back:41:9999999999:9999999999"
    assert edits.bound_to_message(edits.pick_data(41, TASK_ID), 41) == f"pick:41:{TASK_ID}"
    assert edits.bound_to_message("apart:41:2", 41) == "apart:41:2"


def test_message_reopen_and_back_callbacks_read_back() -> None:
    reopen = edits.bound_to_message(edits.reopen_data(TASK_ID), 9_999_999_999)
    back = edits.bound_to_message(edits.back_data(TASK_ID, 1, 2), 9_999_999_999)

    assert edits.parse_reopen_message(reopen) == 9_999_999_999
    assert edits.parse_back_message(back) == (9_999_999_999, 1, 2)
    # Прежние кнопки с задачей — своим разбором, а не этим.
    assert edits.parse_reopen(reopen) is None
    assert edits.parse_back(back) is None


@pytest.mark.parametrize(
    "data", ["reopen:", "reopen:-1", "reopen:١", f"reopen:{TASK_ID}", "back:41:1:2", ""]
)
def test_broken_message_reopen_callback_is_nothing(data: str) -> None:
    assert edits.parse_reopen_message(data) is None


@pytest.mark.parametrize(
    "data",
    ["back:41", "back:41:1", "back:41:x:2", "back:-1:1:2", f"back:{TASK_ID}:1:2", "reopen:41", ""],
)
def test_broken_message_back_callback_is_nothing(data: str) -> None:
    assert edits.parse_back_message(data) is None


def test_pressed_pick_takes_all_pick_buttons_along() -> None:
    """Нажатый вопрос теряет свои кнопки (§23.5): выбор — все кнопки выбора."""
    pressed = f"pick:41:{TASK_ID}"

    assert not edits.keeps_button(pressed, pressed)
    assert not edits.keeps_button(f"pick:41:{OTHER_ID}", pressed)
    assert edits.keeps_button("apart:41:2", pressed)
    assert edits.keeps_button("reopen:41", pressed)


def test_pressed_apart_or_reopen_takes_only_itself() -> None:
    assert not edits.keeps_button("apart:41:2", "apart:41:2")
    assert edits.keeps_button("apart:41:3", "apart:41:2")
    assert edits.keeps_button(f"pick:41:{TASK_ID}", "apart:41:2")
    assert not edits.keeps_button("reopen:41", "reopen:41")
    assert edits.keeps_button("apart:41:2", "reopen:41")


def test_candidate_button_carries_the_title_and_a_short_due() -> None:
    by_hour = make_task(due_at=datetime(2026, 10, 2, 17, 0, tzinfo=TZ), due_precision="time")
    by_day = make_task(due_at=datetime(2026, 10, 2, 18, 0, tzinfo=TZ), due_precision="day")

    assert edits.candidate_label(by_hour, TZ) == "встреча с Ренатой — 2 окт, 17:00"
    assert edits.candidate_label(by_day, TZ) == "встреча с Ренатой — 2 окт"
    assert edits.candidate_label(make_task(), TZ) == "встреча с Ренатой"


def test_candidate_button_names_the_part_of_day() -> None:
    task = make_task(due_at=datetime(2026, 10, 2, 8, 0, tzinfo=TZ), due_precision="morning")

    assert edits.candidate_label(task, TZ) == "встреча с Ренатой — 2 окт, утром"


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


def test_pick_question_about_a_move_names_the_part_of_day() -> None:
    today = make_edit(due_at="2026-09-29T18:00:00+05:00", due_precision="evening")
    friday = make_edit(due_at="2026-10-02T08:00:00+05:00", due_precision="morning")

    assert edits.pick_question(today, NOW, TZ) == "Какую задачу перенести на сегодня вечером?"
    assert edits.pick_question(friday, NOW, TZ) == (
        "Какую задачу перенести на пятницу, 2 октября, утром?"
    )
