"""Часы по сферам (`techspec/31-hours.md`): обвязка бота вокруг счёта базы.

Считает функция базы `sphere_hours` (этап 035, `techspec/03-schema.md` §3.18):
её случаи — сессии переписки, встречи, пересечения, окна, полночь,
понедельник, округление — проверяет `supabase/tests/sphere_hours.test.ts` на
настоящем Postgres, с теми же числами, что проверялись здесь до переезда.
Здесь — то, что осталось боту: окна «сегодня», «вчера» и «неделя с
понедельника» по поясу владельца, слова и строки блока «Часы по сферам», блок
в промпте, чтение функции базы и как бот собирает блок — только своему тексту
и голосу, при сбое базы — без блока. Сети и базы здесь нет: модель и
хранилище подменены.
"""

from __future__ import annotations

import inspect
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from solomon import texts
from solomon.db import hours as db_hours
from solomon.db.hours import SphereMinutes
from solomon.db.rpc import DatabaseError
from solomon.db.spheres import Sphere
from solomon.services import hours
from solomon.services.tasks import TaskService
from solomon.services.understanding import (
    EDIT_RULES,
    HOURS_HEAD,
    HOURS_RULES,
    RULES,
    UnderstandingService,
    build_system_prompt,
    format_hours,
    open_task_lines,
)
from tests.conftest import (
    OWNER_ID,
    OWNER_TIMEZONE,
    FakeAnalyst,
    FakeEdits,
    FakeMessages,
    FakePlanner,
    FakeTranscriber,
    FakeUnderstandings,
    make_details,
    make_message_understanding,
    make_settings,
    make_understanding,
)
from tests.test_tasks_db import FakeClient, as_client
from tests.test_understanding import FakeAnswer, FakeCall

TZ = ZoneInfo(OWNER_TIMEZONE)
# Суббота, 10 октября 2026, 18:00 у владельца (+05:00). Неделя — с
# понедельника, 5 октября; вчера — пятница, 9 октября.
NOW = datetime(2026, 10, 10, 18, 0, tzinfo=TZ)
SATURDAY = date(2026, 10, 10)
FRIDAY = date(2026, 10, 9)
MONDAY = date(2026, 10, 5)


def at(hour: int, minute: int = 0, day: date = SATURDAY) -> datetime:
    """Этот час в этот день по поясу владельца."""
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)


def row(name: str | None, meetings: int, chat: int) -> SphereMinutes:
    """Строка ответа `sphere_hours`; id сферы тестам обвязки не нужен."""
    return SphereMinutes(
        sphere_id=None if name is None else f"s-{name}", name=name, meetings=meetings, chat=chat
    )


# --- окна -------------------------------------------------------------------------


def test_today_yesterday_and_the_week_from_monday_by_the_owner_clock() -> None:
    """Окна — по поясу владельца, правый край — «сейчас»: так бот спрашивает
    базу (§31.2)."""
    found = hours.windows(NOW, TZ)

    assert found == [
        hours.Window("Сегодня, суббота, 10 октября", at(0), NOW),
        hours.Window("Вчера, пятница, 9 октября", at(0, day=FRIDAY), at(0)),
        hours.Window("Неделя с понедельника, 5 октября", at(0, day=MONDAY), NOW),
    ]


def test_windows_follow_the_owner_clock_not_the_utc_day() -> None:
    """В 02:00 у владельца по UTC ещё вчера — окно всё равно его сегодня."""
    late = datetime(2026, 10, 9, 21, 0, tzinfo=ZoneInfo("UTC"))

    today = hours.windows(late, TZ)[0]

    assert today.label == "Сегодня, суббота, 10 октября"
    assert today.start == at(0)


def test_on_monday_yesterday_is_last_sunday_and_the_week_is_today() -> None:
    monday_evening = at(20, day=MONDAY)

    found = hours.windows(monday_evening, TZ)

    assert found[1] == hours.Window(
        "Вчера, воскресенье, 4 октября", at(0, day=date(2026, 10, 4)), at(0, day=MONDAY)
    )
    assert found[2].start == found[0].start == at(0, day=MONDAY)


def test_period_keeps_the_order_of_the_database_and_drops_empty_spheres() -> None:
    """Порядок — как отдала база: сферы по заведению, «без сферы» последней."""
    window = hours.windows(NOW, TZ)[0]

    found = hours.period(window, [row("VoiceFin", 60, 0), row("РЕЙВА", 0, 0), row(None, 0, 2)])

    assert found == hours.Period(
        window.label, (hours.SphereHours("VoiceFin", 60, 0), hours.SphereHours(None, 0, 2))
    )


# --- слова --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("minutes", "words"),
    [(5, "5 мин"), (45, "45 мин"), (60, "1 ч"), (130, "2 ч 10 мин"), (600, "10 ч")],
)
def test_hours_words(minutes: int, words: str) -> None:
    assert hours.hours_words(minutes) == words


def test_lines_name_every_sphere_with_meetings_and_chat_and_the_total() -> None:
    """Те же строки, что до переезда счёта в базу: часы — те, что база
    посчитала бы для встречи 14–16 и переписки (`sphere_hours.test.ts`)."""
    counted = (hours.SphereHours("VoiceFin", 120, 10), hours.SphereHours(None, 0, 25))
    labels = [window.label for window in hours.windows(NOW, TZ)]

    lines = hours.lines(
        [
            hours.Period(labels[0], counted),
            hours.Period(labels[1], ()),
            hours.Period(labels[2], counted),
        ]
    )

    assert lines == [
        "Сегодня, суббота, 10 октября: VoiceFin — 2 ч 10 мин (встречи 2 ч, переписка "
        "10 мин); без сферы — 25 мин (переписка 25 мин). Всего 2 ч 35 мин.",
        "Вчера, пятница, 9 октября: ни встреч, ни переписки.",
        "Неделя с понедельника, 5 октября: VoiceFin — 2 ч 10 мин (встречи 2 ч, "
        "переписка 10 мин); без сферы — 25 мин (переписка 25 мин). Всего 2 ч 35 мин.",
    ]


def test_parts_add_up_to_the_sphere_total() -> None:
    """База округляет итог и встречи, переписка — их разность (§31.2)."""
    item = hours.SphereHours("VoiceFin", 31, 2)

    assert (item.meetings, item.chat, item.total) == (31, 2, 33)


# --- чтение функции базы ------------------------------------------------------------


async def test_sphere_hours_asks_the_database_for_the_window_of_this_owner() -> None:
    fake = FakeClient(
        data=[
            {"sphere_id": "s1", "name": "VoiceFin", "meetings": 120, "chat": 10},
            {"sphere_id": None, "name": None, "meetings": 0, "chat": 25},
        ]
    )

    found = await db_hours.sphere_hours(
        as_client(fake), owner_telegram_id=OWNER_ID, start=at(0), end=NOW
    )

    assert found == [
        SphereMinutes(sphere_id="s1", name="VoiceFin", meetings=120, chat=10),
        SphereMinutes(sphere_id=None, name=None, meetings=0, chat=25),
    ]
    assert fake.calls == [
        (
            "rpc",
            "sphere_hours",
            {
                "window_start": at(0).isoformat(),
                "window_end": NOW.isoformat(),
                "owner_telegram_id": OWNER_ID,
            },
        )
    ]


async def test_empty_answer_is_no_hours() -> None:
    fake = FakeClient(data=None)

    found = await db_hours.sphere_hours(
        as_client(fake), owner_telegram_id=OWNER_ID, start=at(0), end=NOW
    )

    assert found == []


@pytest.mark.parametrize(
    "data",
    [
        {"sphere_id": None},
        ["не строка"],
        [{"sphere_id": None, "name": None, "meetings": "час", "chat": 0}],
        [{"sphere_id": None, "name": None, "meetings": 10}],
        [{"sphere_id": None, "name": None, "meetings": -1, "chat": 0}],
    ],
)
async def test_broken_answer_is_a_failure(data: Any) -> None:
    fake = FakeClient(data=data)

    with pytest.raises(DatabaseError):
        await db_hours.sphere_hours(
            as_client(fake), owner_telegram_id=OWNER_ID, start=at(0), end=NOW
        )


def test_owner_is_required_by_the_hours_query() -> None:
    """Инвариант 2 держится сигнатурой: владельца не забыть и не подставить."""
    parameter = inspect.signature(db_hours.sphere_hours).parameters["owner_telegram_id"]

    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty


# --- срок со временем и длительностью --------------------------------------------


def test_due_with_an_hour_and_a_duration_names_the_end() -> None:
    """Запись и правка называют конец встречи: «14:00–16:00»; без часа — нет."""
    assert texts.format_due(at(14), "time", 120) == "суббота, 10 октября, 14:00–16:00"
    assert texts.format_due(at(23), "time", 90) == "суббота, 10 октября, 23:00–00:30"
    assert texts.format_due(at(14), "time") == "суббота, 10 октября, 14:00"
    assert texts.format_due(at(18), "day", 60) == "суббота, 10 октября"


# --- блок в промпте (§31.2) ---------------------------------------------------------

LINES = (
    "Сегодня, суббота, 10 октября: VoiceFin — 2 ч (встречи 2 ч). Всего 2 ч.",
    "Вчера, пятница, 9 октября: ни встреч, ни переписки.",
    "Неделя с понедельника, 5 октября: VoiceFin — 2 ч (встречи 2 ч). Всего 2 ч.",
)


def test_hours_block_has_the_lines_and_the_rules_after_the_spheres() -> None:
    """Блок — за сферами: часы называют сферы так же, как список сфер."""
    system = build_system_prompt(NOW, TZ, spheres=[], hours=LINES)

    block = format_hours(LINES)
    assert block == "\n".join((HOURS_HEAD, *LINES, HOURS_RULES))
    assert block in system
    assert system.index("Сфер у владельца пока нет.") < system.index(HOURS_HEAD)
    assert format_hours(None) == format_hours([]) == ""
    assert HOURS_HEAD not in build_system_prompt(NOW, TZ, spheres=[])


def test_hours_rules_answer_roughly_from_the_lines_and_never_unasked() -> None:
    for words in ("примерно", "ничего не досчитывайте", "chat", "без\nвопроса"):
        assert words in HOURS_RULES, words


def test_duration_rules_tell_a_meeting_from_a_simple_errand() -> None:
    """Поле разбора (§31.1): «с 14 до 16» — 120, встреча без конца — 60,
    простое дело с часом — без длительности; в правке — верхним полем."""
    for words in ("duration", "«с 14 до 16» — 120", "60", "«в 15 позвонить маме» — null"):
        assert words in RULES, words
    assert "«до 16»" in EDIT_RULES
    assert "duration = null, прежняя длительность останется" in EDIT_RULES


def test_open_task_line_names_the_end_of_a_meeting() -> None:
    meeting = make_details(
        title="созвон по VoiceFin", due_at=at(14), due_precision="time", duration=120
    )

    assert open_task_lines([meeting], TZ) == [
        "1. созвон по VoiceFin (срок: суббота, 10 октября, 14:00–16:00)"
    ]


async def test_forwarded_message_gets_no_hours_block() -> None:
    """Разговор ведёт только своё сообщение (§17.1): у пересланного часов нет."""
    call = FakeCall(FakeAnswer(parsed_output=make_message_understanding()))
    service = UnderstandingService(settings=make_settings(), call=call, clock=lambda: NOW)

    await service.analyze("созвон с Игорем", hours=LINES)
    await service.analyze("созвон с Игорем", forwarded_from="Олег", hours=LINES)

    own, forwarded = (system for system, _ in call.calls)
    assert HOURS_HEAD in own
    assert HOURS_HEAD not in forwarded


# --- сборка блока ботом -------------------------------------------------------------

SPHERES = [Sphere(id="s-voicefin", name="VoiceFin", facts=())]
TODAY, YESTERDAY, WEEK = ((window.start, window.end) for window in hours.windows(NOW, TZ))


def hours_service(store: FakeEdits) -> tuple[TaskService, FakeAnalyst]:
    analyst = FakeAnalyst(make_understanding(kind="chat", reply_hint="Примерно два часа."))
    service = TaskService(
        settings=make_settings(),
        record_message=FakeMessages(),
        record_understanding=FakeUnderstandings(),
        analyst=analyst,
        transcriber=FakeTranscriber(),
        planner=FakePlanner(),
        clock=lambda: NOW,
        edit_store=store,
    )
    return service, analyst


async def test_own_message_gets_the_hours_of_three_windows_from_the_database() -> None:
    """Бот спрашивает `sphere_hours` на сегодня, вчера и неделю и кладёт в
    промпт строки — те же, что считал сам до этапа 035."""
    counted = [row("VoiceFin", 120, 0), row(None, 0, 2)]
    store = FakeEdits(spheres=SPHERES, hours={TODAY: counted, WEEK: counted})
    service, analyst = hours_service(store)

    await service.record_from_message(chat_id=42, telegram_message_id=7, text="куда ушёл день?")

    assert sorted(store.hour_reads) == sorted([TODAY, YESTERDAY, WEEK])
    assert analyst.hours == [
        [
            "Сегодня, суббота, 10 октября: VoiceFin — 2 ч (встречи 2 ч); без сферы — 2 мин "
            "(переписка 2 мин). Всего 2 ч 2 мин.",
            "Вчера, пятница, 9 октября: ни встреч, ни переписки.",
            "Неделя с понедельника, 5 октября: VoiceFin — 2 ч (встречи 2 ч); без сферы — "
            "2 мин (переписка 2 мин). Всего 2 ч 2 мин.",
        ]
    ]


async def test_forwarded_message_does_not_read_the_hours() -> None:
    store = FakeEdits(spheres=SPHERES)
    service, analyst = hours_service(store)

    await service.record_from_message(
        chat_id=42, telegram_message_id=7, text="созвон в 14", forwarded_from="Игорь"
    )

    assert store.hour_reads == []
    assert analyst.hours == [None]


async def test_failed_hours_leave_the_parse_without_the_block() -> None:
    """База не ответила — разбор идёт без блока: поручение важнее контекста."""
    store = FakeEdits(spheres=SPHERES, broken=["sphere_hours"])
    service, analyst = hours_service(store)

    outcome = await service.record_from_message(
        chat_id=42, telegram_message_id=7, text="сколько ушло на VoiceFin?"
    )

    assert outcome.ok
    assert analyst.hours == [None]


async def test_hours_name_spheres_themselves_when_the_spheres_did_not_read() -> None:
    """Названия сфер приходят из ответа `sphere_hours`: без списка сфер часы
    не уходят в «без сферы» — блок тот же."""
    store = FakeEdits(spheres=SPHERES, broken=["spheres"], hours={TODAY: [row("VoiceFin", 60, 0)]})
    service, analyst = hours_service(store)

    await service.record_from_message(chat_id=42, telegram_message_id=7, text="куда ушёл день?")

    assert analyst.spheres == [None]
    assert analyst.hours[0] is not None
    assert analyst.hours[0][0] == (
        "Сегодня, суббота, 10 октября: VoiceFin — 1 ч (встречи 1 ч). Всего 1 ч."
    )
