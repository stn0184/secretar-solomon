"""Повтор — чистые функции (`techspec/13-repeat.md`).

Правило от модели по форме §13.2 и что с ним делать при записи (§13.5), раз
в секундах Unix для кнопок (§13.3) и правило словами (§13.7). Сети и базы
нет: следующий раз считает база, её правило проверяют тесты PGlite.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from solomon import texts
from solomon.services import repeat
from solomon.services.understanding import Repeat
from tests.conftest import OWNER_TIMEZONE

TZ = ZoneInfo(OWNER_TIMEZONE)
MONDAY = datetime(2026, 10, 5, 18, 0, tzinfo=TZ)


def make_rule(**fields: Any) -> Repeat:
    """Правило, как его отдаёт модель: все поля, пустые — явно."""
    base: dict[str, Any] = {
        "every": "week",
        "interval": 1,
        "weekdays": [1],
        "month_day": None,
        "month": None,
    }
    return Repeat.model_validate({**base, **fields})


def canonical(**fields: Any) -> dict[str, Any]:
    """Правило для базы: пять ключей без `time`, неположенные — пусты."""
    base: dict[str, Any] = {
        "every": "week",
        "interval": 1,
        "weekdays": [1],
        "month_day": None,
        "month": None,
    }
    return {**base, **fields}


@pytest.mark.parametrize(
    ("rule", "expected"),
    [
        (make_rule(), canonical()),
        (
            make_rule(every="day", interval=3, weekdays=[]),
            canonical(every="day", interval=3, weekdays=None),
        ),
        (make_rule(weekdays=[5, 1, 3]), canonical(weekdays=[1, 3, 5])),
        (make_rule(weekdays=[2, 2]), canonical(weekdays=[2])),
        (
            make_rule(every="month", weekdays=[], month_day=10),
            canonical(every="month", weekdays=None, month_day=10),
        ),
        (
            make_rule(every="month", interval=3, weekdays=[], month_day=-1),
            canonical(every="month", interval=3, weekdays=None, month_day=-1),
        ),
        (
            make_rule(every="year", weekdays=[], month_day=29, month=2),
            canonical(every="year", weekdays=None, month_day=29, month=2),
        ),
        (
            make_rule(every="week", interval=99, weekdays=[1, 2, 3, 4, 5, 6, 7]),
            canonical(interval=99, weekdays=[1, 2, 3, 4, 5, 6, 7]),
        ),
    ],
)
def test_rule_of_the_right_form_is_cleaned(rule: Repeat, expected: dict[str, Any]) -> None:
    """Правило по форме — каноническое: дни по порядку и без повторов, лишнее пусто."""
    assert repeat.clean_rule(rule) == expected


@pytest.mark.parametrize(
    "rule",
    [
        make_rule(interval=0),
        make_rule(interval=100),
        make_rule(weekdays=[]),
        make_rule(weekdays=[0]),
        make_rule(weekdays=[8]),
        make_rule(month_day=5),
        make_rule(month=3),
        make_rule(every="day", weekdays=[1]),
        make_rule(every="month", weekdays=[], month_day=None),
        make_rule(every="month", weekdays=[], month_day=0),
        make_rule(every="month", weekdays=[], month_day=32),
        make_rule(every="month", weekdays=[], month_day=-2),
        make_rule(every="month", weekdays=[], month_day=10, month=1),
        make_rule(every="year", weekdays=[], month_day=5, month=None),
        make_rule(every="year", weekdays=[], month_day=5, month=13),
        make_rule(every="year", weekdays=[], month_day=-1, month=3),
        make_rule(every="year", weekdays=[], month_day=31, month=4),
        make_rule(every="year", weekdays=[], month_day=30, month=2),
    ],
)
def test_rule_out_of_form_is_refused(rule: Repeat) -> None:
    """Не по форме §13.2 — `None`: такое правило база не примет."""
    assert repeat.clean_rule(rule) is None


def test_record_keeps_a_rule_of_a_task_with_a_due() -> None:
    outcome = repeat.record_rule("task", MONDAY, make_rule())

    assert outcome == repeat.RuleOutcome(rule=canonical(), malformed=False)


@pytest.mark.parametrize(
    ("kind", "due_at"),
    [("idea", MONDAY), ("wish", MONDAY), ("task", None), ("chat", MONDAY)],
)
def test_record_drops_a_rule_without_a_due_or_of_an_idea(
    kind: str, due_at: datetime | None
) -> None:
    """У идеи, желания и задачи без срока повторять нечего — молча (§13.5)."""
    assert repeat.record_rule(kind, due_at, make_rule()) == repeat.RuleOutcome(
        rule=None, malformed=False
    )


def test_record_without_a_rule_is_a_one_off_task() -> None:
    assert repeat.record_rule("task", MONDAY, None) == repeat.RuleOutcome(
        rule=None, malformed=False
    )


def test_record_marks_a_rule_out_of_form() -> None:
    """Правило не по форме — разовая задача с пометкой, а не отказ (инвариант 5)."""
    outcome = repeat.record_rule("task", MONDAY, make_rule(weekdays=[]))

    assert outcome == repeat.RuleOutcome(rule=None, malformed=True)


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        (None, "Не разобрал повтор — записал разовой"),
        ("", "Не разобрал повтор — записал разовой"),
        (
            "Не понял, кому отправить.",
            "Не понял, кому отправить. Не разобрал повтор — записал разовой",
        ),
        ("Не понял, кому", "Не понял, кому. Не разобрал повтор — записал разовой"),
    ],
)
def test_malformed_reason_keeps_the_model_reason_first(reason: str | None, expected: str) -> None:
    assert repeat.malformed_reason(reason) == expected


def test_occurrence_is_whole_seconds_of_unix_time() -> None:
    """Раз в кнопке — секунды Unix, как их сравнивает база (§13.3)."""
    moment = datetime(2026, 10, 5, 13, 0, 0, 999_999, tzinfo=UTC)

    assert repeat.occurrence_seconds(moment) == 1791205200
    assert repeat.occurrence_seconds(MONDAY) == 1791205200


def test_moment_of_seconds_is_the_occurrence_back() -> None:
    assert repeat.moment_of(1791205200) == datetime(2026, 10, 5, 13, 0, tzinfo=UTC)


def test_same_rule_ignores_the_hour_and_empty_days() -> None:
    """Правило из базы с часом серии — то же, что правило модели без часа (§13.5)."""
    stored = {**canonical(), "time": "09:00"}
    daily = {**canonical(every="day", weekdays=None), "time": None}

    assert repeat.same_rule(stored, canonical())
    assert repeat.same_rule(daily, canonical(every="day", weekdays=None))
    assert repeat.same_rule({**daily, "weekdays": []}, canonical(every="day", weekdays=None))
    assert not repeat.same_rule(stored, canonical(weekdays=[2]))
    assert not repeat.same_rule(stored, canonical(interval=2))
    assert not repeat.same_rule(None, canonical())


def test_series_precision_follows_the_hour_of_the_rule() -> None:
    """Точность раза — по часу серии: есть час — `time`, нет — `day` (§13.3)."""
    assert repeat.series_precision({**canonical(), "time": "09:00"}) == "time"
    assert repeat.series_precision({**canonical(), "time": None}) == "day"
    assert repeat.series_precision(canonical()) == "day"


# Общие примеры бота и приложения (§13.7): тот же список — в
# `miniapp/src/lib/repeat.test.ts`.
WORDS: list[tuple[dict[str, Any], str]] = [
    (canonical(every="day", weekdays=None), "каждый день"),
    (canonical(every="day", interval=2, weekdays=None), "через день"),
    (canonical(every="day", interval=3, weekdays=None), "каждые 3 дня"),
    (canonical(), "каждый понедельник"),
    (canonical(weekdays=[3]), "каждую среду"),
    (canonical(weekdays=[1, 2, 3, 4, 5]), "по будням"),
    (canonical(weekdays=[6, 7]), "по выходным"),
    (canonical(weekdays=[1, 5]), "по понедельникам и пятницам"),
    (canonical(interval=2, weekdays=[2]), "каждые 2 недели по вторникам"),
    (canonical(every="month", weekdays=None, month_day=10), "каждый месяц 10-го"),
    (canonical(every="month", weekdays=None, month_day=-1), "в последний день месяца"),
    (canonical(every="month", interval=3, weekdays=None, month_day=5), "каждые 3 месяца 5-го"),
    (canonical(every="year", weekdays=None, month_day=5, month=3), "каждый год 5 марта"),
]


@pytest.mark.parametrize(("rule", "expected"), WORDS)
def test_rule_in_words(rule: dict[str, Any], expected: str) -> None:
    assert texts.repeat_words(rule) == expected


@pytest.mark.parametrize(
    ("rule", "expected"),
    [
        (canonical(every="day", interval=5, weekdays=None), "каждые 5 дней"),
        (canonical(every="day", interval=21, weekdays=None), "каждый 21 день"),
        (canonical(every="day", interval=11, weekdays=None), "каждые 11 дней"),
        (canonical(weekdays=[7]), "каждое воскресенье"),
        (canonical(weekdays=[5]), "каждую пятницу"),
        (canonical(weekdays=[4]), "каждый четверг"),
        (canonical(weekdays=[1, 3, 5]), "по понедельникам, средам и пятницам"),
        (canonical(weekdays=[1, 2, 3, 4, 5, 6, 7]), "каждый день"),
        (canonical(interval=2, weekdays=[1, 2, 3, 4, 5]), "каждые 2 недели по будням"),
        (canonical(interval=5, weekdays=[6, 7]), "каждые 5 недель по выходным"),
        (canonical(interval=2, weekdays=[3]), "каждые 2 недели по средам"),
        (
            canonical(every="month", interval=3, weekdays=None, month_day=-1),
            "каждые 3 месяца в последний день",
        ),
        (
            canonical(every="year", interval=2, weekdays=None, month_day=29, month=2),
            "каждые 2 года 29 февраля",
        ),
        (
            canonical(every="year", interval=5, weekdays=None, month_day=1, month=1),
            "каждые 5 лет 1 января",
        ),
        ({**canonical(), "time": "09:00"}, "каждый понедельник"),
    ],
)
def test_rule_in_words_beyond_the_shared_examples(rule: dict[str, Any], expected: str) -> None:
    """Склонения и час: `time` в словах не звучит — он в сроке (§13.7)."""
    assert texts.repeat_words(rule) == expected
