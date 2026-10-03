"""Часть дня — чистые функции (`techspec/21-part-of-day.md`).

Части и их начала — константы; начало части по поясу владельца; приведение
срока, который назвала модель: у части в `due_at` ложится начало этой части
того же дня, часть вместе с повтором становится сроком со временем (§21.2).
И как часть называется словами — в ответе, на кнопке, в вопросе о переносе,
в напоминании и в плане (§21.4). Сети и базы здесь нет.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from solomon import texts
from solomon.services import parts
from tests.conftest import OWNER_TIMEZONE

TZ = ZoneInfo(OWNER_TIMEZONE)
# Пятница, 9 октября 2026, в поясе владельца (+05:00).
FRIDAY = date(2026, 10, 9)


def at(hour: int, minute: int = 0, day: date = FRIDAY) -> datetime:
    """Этот час в этот день по поясу владельца."""
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)


def test_parts_and_their_starts_are_constants() -> None:
    """Утро с 08:00, день с 12:00, вечер с 18:00 — константы, не настройка (§21.1)."""
    assert parts.PARTS == ("morning", "afternoon", "evening")
    assert parts.PART_STARTS == {
        "morning": time(8, 0),
        "afternoon": time(12, 0),
        "evening": time(18, 0),
    }


@pytest.mark.parametrize(
    ("precision", "part"),
    [
        ("morning", True),
        ("afternoon", True),
        ("evening", True),
        ("day", False),
        ("time", False),
        (None, False),
        ("noon", False),
    ],
)
def test_only_three_precisions_are_parts(precision: str | None, part: bool) -> None:
    assert parts.is_part(precision) is part


@pytest.mark.parametrize(
    ("part", "start"),
    [("morning", at(8)), ("afternoon", at(12)), ("evening", at(18))],
)
def test_part_starts_in_the_owner_zone(part: str, start: datetime) -> None:
    assert parts.part_start(FRIDAY, part, TZ) == start


def test_part_start_keeps_the_local_hour_when_clocks_change() -> None:
    """В день перевода часов утро — всё равно 08:00 по местным часам."""
    berlin = ZoneInfo("Europe/Berlin")

    morning = parts.part_start(date(2026, 10, 25), "morning", berlin)

    assert morning.isoformat() == "2026-10-25T08:00:00+01:00"


@pytest.mark.parametrize(
    ("part", "said", "start"),
    [
        # Модель назвала другой час — ложится начало части того же дня.
        ("morning", at(9), at(8)),
        ("afternoon", at(14), at(12)),
        ("evening", at(19), at(18)),
        # Модель назвала ровно начало — так и остаётся.
        ("morning", at(8), at(8)),
    ],
)
def test_part_due_becomes_the_start_of_the_part(part: str, said: datetime, start: datetime) -> None:
    assert parts.settle(said, part, repeating=False, timezone=TZ) == (start, part)


def test_day_of_the_part_is_taken_in_the_owner_zone() -> None:
    """20:00 по UTC 8 октября — уже 9 октября у владельца (+05:00)."""
    said = datetime(2026, 10, 8, 20, 0, tzinfo=UTC)

    assert parts.settle(said, "morning", repeating=False, timezone=TZ) == (at(8), "morning")


def test_moment_without_a_zone_is_read_as_the_owner_day() -> None:
    """Момент без пояса — день как назван, без пересчёта."""
    said = datetime(2026, 10, 9, 23, 30)

    assert parts.settle(said, "evening", repeating=False, timezone=TZ) == (at(18), "evening")


def test_part_with_a_repeat_becomes_a_time_with_the_model_moment() -> None:
    """Часть вместе с повтором — ошибка модели: срок со временем, момент как есть (§21.6)."""
    said = at(9)

    assert parts.settle(said, "morning", repeating=True, timezone=TZ) == (said, "time")


@pytest.mark.parametrize("precision", ["day", "time", None])
@pytest.mark.parametrize("repeating", [False, True])
def test_other_precisions_pass_as_they_are(precision: str | None, repeating: bool) -> None:
    said = at(15, 30)

    assert parts.settle(said, precision, repeating=repeating, timezone=TZ) == (said, precision)


def test_part_without_a_due_stays_as_it_is() -> None:
    """Срока нет — ставить нечего: так и остаётся."""
    assert parts.settle(None, "evening", repeating=False, timezone=TZ) == (None, "evening")


# ------------------------------------------------------ срок словами (§21.4)


def test_part_words_are_the_words_of_the_owner() -> None:
    assert texts.PART_WORDS == {"morning": "утром", "afternoon": "днём", "evening": "вечером"}
    assert set(texts.PART_WORDS) == set(parts.PARTS)


@pytest.mark.parametrize(
    ("precision", "start", "words"),
    [
        ("morning", 8, "пятница, 9 октября, утром"),
        ("afternoon", 12, "пятница, 9 октября, днём"),
        ("evening", 18, "пятница, 9 октября, вечером"),
        ("time", 15, "пятница, 9 октября, 15:00"),
        ("day", 18, "пятница, 9 октября"),
        (None, 18, "пятница, 9 октября"),
    ],
)
def test_due_names_the_part_without_an_hour(precision: str | None, start: int, words: str) -> None:
    """Ответ при записи и правке, «Перенёс»: часть словом, без часа (§21.4)."""
    assert texts.format_due(at(start), precision) == words


@pytest.mark.parametrize(
    ("precision", "start", "words"),
    [
        ("morning", 8, "9 окт, утром"),
        ("evening", 18, "9 окт, вечером"),
        ("time", 15, "9 окт, 15:00"),
        ("day", 18, "9 окт"),
    ],
)
def test_short_due_on_a_button_names_the_part(precision: str, start: int, words: str) -> None:
    assert texts.format_short_due(at(start), precision) == words


@pytest.mark.parametrize(
    ("precision", "start", "today", "other_day"),
    [
        ("morning", 8, "на сегодня утром", "на пятницу, 9 октября, утром"),
        ("afternoon", 12, "на сегодня днём", "на пятницу, 9 октября, днём"),
        ("evening", 18, "на сегодня вечером", "на пятницу, 9 октября, вечером"),
        ("time", 15, "на сегодня в 15:00", "на пятницу, 9 октября, в 15:00"),
        ("day", 18, "на сегодня", "на пятницу, 9 октября"),
    ],
)
def test_move_target_names_the_part(precision: str, start: int, today: str, other_day: str) -> None:
    """Вопрос «Какую задачу перенести …?» (§12.6): сегодня и другой день."""
    assert texts.format_move_target(at(start), precision, at(7)) == today
    assert texts.format_move_target(at(start), precision, at(7, day=date(2026, 10, 6))) == (
        other_day
    )


@pytest.mark.parametrize(
    ("precision", "start", "today", "other_day"),
    [
        ("morning", 8, "сегодня утром", "пятница, 9 октября, утром"),
        ("afternoon", 12, "сегодня днём", "пятница, 9 октября, днём"),
        ("evening", 18, "сегодня вечером", "пятница, 9 октября, вечером"),
        ("time", 15, "сегодня, 15:00", "пятница, 9 октября, 15:00"),
        # Дело на день — без 18:00, которых владелец не говорил (§21.3).
        ("day", 18, "сегодня", "пятница, 9 октября"),
        (None, 18, "сегодня", "пятница, 9 октября"),
    ],
)
def test_due_moment_in_a_reminder_has_an_hour_only_for_a_time(
    precision: str | None, start: int, today: str, other_day: str
) -> None:
    """Срок в напоминании: час — только у срока со временем (§21.3)."""
    assert texts.format_due_moment(at(start), precision, at(19)) == today
    assert texts.format_due_moment(at(start), precision, at(19, day=date(2026, 10, 10))) == (
        other_day
    )


@pytest.mark.parametrize(
    ("precision", "label"),
    [("morning", "Утром"), ("afternoon", "Днём"), ("evening", "Вечером")],
)
def test_plan_label_of_a_part_starts_with_a_capital(precision: str, label: str) -> None:
    """Строка утреннего плана: «Утром — встреча с Ренатой» (§20.3)."""
    assert texts.part_label(precision) == label
    assert texts.morning_line(texts.part_label(precision), "встреча с Ренатой") == (
        f"{label} — встреча с Ренатой"
    )
