"""Часы по сферам (`techspec/31-hours.md`).

Чистые функции: сессии переписки из сообщений чатов, часы встреч, окна
«сегодня», «вчера» и «неделя с понедельника» по поясу владельца, пересечения
без двойного счёта и строки блока «Часы по сферам». Дальше — блок в промпте и
как бот его собирает: только своему тексту и голосу, а без сфер или при сбое
базы — без блока. Сети и базы здесь нет: модель и хранилище подменены.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from solomon import texts
from solomon.db.chats import ChatStamp
from solomon.db.spheres import Sphere
from solomon.db.tasks import Meeting as DbMeeting
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


@dataclass(frozen=True, slots=True)
class Meeting:
    start: datetime
    minutes: int
    sphere_id: str | None


@dataclass(frozen=True, slots=True)
class Stamp:
    thread_id: str
    sphere_id: str | None
    sent_at: datetime
    mine: bool


@dataclass(frozen=True, slots=True)
class Book:
    id: str
    name: str


VOICEFIN = Book(id="s-voicefin", name="VoiceFin")
REIVA = Book(id="s-reiva", name="РЕЙВА")
BOOK = (VOICEFIN, REIVA)


def chat(thread: str, sphere: str | None, *moments: tuple[datetime, bool]) -> list[Stamp]:
    """Сообщения одного чата: время и «моё ли»."""
    return [Stamp(thread, sphere, sent_at, mine) for sent_at, mine in moments]


def today(periods: list[hours.Period]) -> hours.Period:
    return periods[0]


def minutes_of(period: hours.Period) -> dict[str | None, tuple[int, int]]:
    """Сфера → (встречи, переписка) в минутах."""
    return {item.name: (item.meetings, item.chat) for item in period.spheres}


# --- сессии переписки -----------------------------------------------------------


def test_messages_closer_than_ten_minutes_make_one_session_with_two_minutes_tail() -> None:
    stamps = chat(
        "t1",
        "s-voicefin",
        (at(10, 0), False),
        (at(10, 4), True),
        (at(10, 14), False),
    )

    found = hours.sessions(stamps)

    assert found == [("s-voicefin", hours.Span(at(10, 0), at(10, 16)))]


def test_a_gap_longer_than_ten_minutes_starts_a_new_session() -> None:
    stamps = chat(
        "t1",
        None,
        (at(10, 0), True),
        (at(10, 11), True),
    )

    found = hours.sessions(stamps)

    assert found == [
        (None, hours.Span(at(10, 0), at(10, 2))),
        (None, hours.Span(at(10, 11), at(10, 13))),
    ]


def test_a_session_without_the_owner_does_not_count() -> None:
    """Собеседник писал, владелец молчал — его время на это не ушло."""
    stamps = chat("t1", "s-voicefin", (at(10, 0), False), (at(10, 5), False))

    assert hours.sessions(stamps) == []


def test_sessions_are_per_chat_and_ignore_the_arrival_order() -> None:
    """Сессия — в пределах одного чата; сообщения считаются по времени площадки,
    в каком бы порядке их ни отдала база."""
    stamps = [
        *chat("t2", "s-reiva", (at(10, 3), True)),
        *chat("t1", "s-voicefin", (at(10, 5), False), (at(10, 0), True)),
    ]

    found = hours.sessions(stamps)

    assert sorted(found, key=lambda item: item[1].start) == [
        ("s-voicefin", hours.Span(at(10, 0), at(10, 7))),
        ("s-reiva", hours.Span(at(10, 3), at(10, 5))),
    ]


# --- подсчёт ------------------------------------------------------------------------


def test_past_meeting_goes_to_its_sphere() -> None:
    meetings = [Meeting(at(14), 120, "s-voicefin")]

    periods = hours.tally(meetings, [], BOOK, NOW, TZ)

    assert minutes_of(today(periods)) == {"VoiceFin": (120, 0)}


def test_meeting_counts_only_the_part_that_has_passed() -> None:
    """Время встречи ещё не прошло — в часы идёт только прошедшее."""
    meetings = [Meeting(at(17), 120, "s-voicefin"), Meeting(at(19), 60, "s-reiva")]

    periods = hours.tally(meetings, [], BOOK, NOW, TZ)

    assert minutes_of(today(periods)) == {"VoiceFin": (60, 0)}


def test_meeting_and_chat_at_the_same_time_count_once_within_a_sphere() -> None:
    """Переписка во время встречи — та же работа: пересечение — встреча."""
    meetings = [Meeting(at(14), 60, "s-voicefin")]
    stamps = chat("t1", "s-voicefin", (at(14, 50), True), (at(14, 58), False), (at(15, 8), True))

    periods = hours.tally(meetings, stamps, BOOK, NOW, TZ)

    assert minutes_of(today(periods)) == {"VoiceFin": (60, 10)}


def test_chats_of_one_sphere_at_the_same_time_count_once() -> None:
    stamps = [
        *chat("t1", "s-voicefin", (at(10, 0), True), (at(10, 8), False)),
        *chat("t2", "s-voicefin", (at(10, 5), True)),
    ]

    periods = hours.tally([], stamps, BOOK, NOW, TZ)

    assert minutes_of(today(periods)) == {"VoiceFin": (0, 10)}


def test_overlaps_of_different_spheres_both_count() -> None:
    """Пересечение не складывается дважды только в пределах сферы."""
    meetings = [Meeting(at(14), 60, "s-voicefin")]
    stamps = chat("t1", "s-reiva", (at(14, 10), True))

    periods = hours.tally(meetings, stamps, BOOK, NOW, TZ)

    assert minutes_of(today(periods)) == {"VoiceFin": (60, 0), "РЕЙВА": (0, 2)}


def test_unknown_or_missing_sphere_is_without_a_sphere_and_goes_last() -> None:
    meetings = [Meeting(at(9), 30, None), Meeting(at(11), 30, "s-gone")]
    stamps = chat("t1", "s-reiva", (at(12), True))

    periods = hours.tally(meetings, stamps, BOOK, NOW, TZ)

    assert [item.name for item in today(periods).spheres] == ["РЕЙВА", None]
    assert minutes_of(today(periods))[None] == (60, 0)


def test_spheres_follow_the_book_order() -> None:
    meetings = [Meeting(at(9), 30, "s-reiva"), Meeting(at(11), 30, "s-voicefin")]

    periods = hours.tally(meetings, [], BOOK, NOW, TZ)

    assert [item.name for item in today(periods).spheres] == ["VoiceFin", "РЕЙВА"]


def test_today_yesterday_and_the_week_from_monday_by_the_owner_clock() -> None:
    """Окна — по поясу владельца: вчерашняя встреча не в «сегодня», понедельничная
    — в неделе, прошлое воскресенье — нигде."""
    meetings = [
        Meeting(at(10), 60, "s-voicefin"),
        Meeting(at(10, day=FRIDAY), 90, "s-voicefin"),
        Meeting(at(10, day=MONDAY), 30, "s-voicefin"),
        Meeting(at(10, day=date(2026, 10, 4)), 45, "s-voicefin"),
    ]

    periods = hours.tally(meetings, [], BOOK, NOW, TZ)

    assert [period.label for period in periods] == [
        "Сегодня, суббота, 10 октября",
        "Вчера, пятница, 9 октября",
        "Неделя с понедельника, 5 октября",
    ]
    assert [minutes_of(period) for period in periods] == [
        {"VoiceFin": (60, 0)},
        {"VoiceFin": (90, 0)},
        {"VoiceFin": (180, 0)},
    ]


def test_a_session_across_midnight_is_split_between_the_days() -> None:
    stamps = chat("t1", None, (at(23, 55, day=FRIDAY), True), (at(0, 5), True))

    periods = hours.tally([], stamps, BOOK, NOW, TZ)

    assert minutes_of(periods[0]) == {None: (0, 7)}
    assert minutes_of(periods[1]) == {None: (0, 5)}


def test_on_monday_yesterday_is_last_sunday_and_the_week_is_today() -> None:
    monday_evening = at(20, day=MONDAY)
    meetings = [Meeting(at(10, day=date(2026, 10, 4)), 60, "s-reiva")]

    periods = hours.tally(meetings, [], BOOK, monday_evening, TZ)

    assert periods[1].label == "Вчера, воскресенье, 4 октября"
    assert minutes_of(periods[1]) == {"РЕЙВА": (60, 0)}
    assert minutes_of(periods[2]) == {}


def test_reading_starts_a_day_before_the_earliest_window() -> None:
    """Встреча до суток длиной и сессия, начатая накануне, задевают окно —
    читать надо с запасом."""
    assert hours.reading_since(NOW, TZ) == at(0, day=date(2026, 10, 4))
    assert hours.reading_since(at(9, day=MONDAY), TZ) == at(0, day=date(2026, 10, 3))


# --- слова --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("minutes", "words"),
    [(5, "5 мин"), (45, "45 мин"), (60, "1 ч"), (130, "2 ч 10 мин"), (600, "10 ч")],
)
def test_hours_words(minutes: int, words: str) -> None:
    assert hours.hours_words(minutes) == words


def test_lines_name_every_sphere_with_meetings_and_chat_and_the_total() -> None:
    meetings = [Meeting(at(14), 120, "s-voicefin")]
    stamps = [
        *chat("t1", "s-voicefin", (at(16, 30), True), (at(16, 38), False)),
        *chat(
            "t2",
            None,
            (at(11, 0), True),
            (at(11, 20), True),
            (at(11, 10), False),
            (at(11, 23), True),
        ),
    ]

    lines = hours.lines(hours.tally(meetings, stamps, BOOK, NOW, TZ))

    assert lines == [
        "Сегодня, суббота, 10 октября: VoiceFin — 2 ч 10 мин (встречи 2 ч, переписка "
        "10 мин); без сферы — 25 мин (переписка 25 мин). Всего 2 ч 35 мин.",
        "Вчера, пятница, 9 октября: ни встреч, ни переписки.",
        "Неделя с понедельника, 5 октября: VoiceFin — 2 ч 10 мин (встречи 2 ч, "
        "переписка 10 мин); без сферы — 25 мин (переписка 25 мин). Всего 2 ч 35 мин.",
    ]


def test_seconds_round_to_minutes_and_parts_add_up() -> None:
    """Минуты округляются, а встречи и переписка в сумме — ровно итог сферы."""
    meetings = [Meeting(at(9), 31, "s-voicefin")]
    stamps = chat("t1", "s-voicefin", (at(9, 30) + timedelta(seconds=40), True))

    period = today(hours.tally(meetings, stamps, BOOK, NOW, TZ))

    item = period.spheres[0]
    assert (item.meetings, item.chat, item.total) == (31, 2, 33)


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


async def test_own_message_gets_the_hours_counted_from_meetings_and_chats() -> None:
    """Бот читает встречи и сообщения с запасом до самого раннего окна и кладёт
    в промпт строки, посчитанные `tally`."""
    store = FakeEdits(
        spheres=SPHERES,
        meetings=[DbMeeting(start=at(14), minutes=120, sphere_id="s-voicefin")],
        stamps=[ChatStamp(thread_id="t1", sphere_id=None, sent_at=at(11), mine=True)],
    )
    service, analyst = hours_service(store)

    await service.record_from_message(chat_id=42, telegram_message_id=7, text="куда ушёл день?")

    since = hours.reading_since(NOW, TZ)
    assert sorted(store.hour_reads) == [("chat_stamps", since), ("meetings", since)]
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


@pytest.mark.parametrize("broken", ["meetings", "chat_stamps", "spheres"])
async def test_failed_read_leaves_the_parse_without_the_hours(broken: str) -> None:
    """База не ответила — разбор идёт без блока: поручение важнее контекста.
    Без сфер часы ушли бы в «без сферы» — тоже без блока."""
    store = FakeEdits(spheres=SPHERES, broken=[broken])
    service, analyst = hours_service(store)

    outcome = await service.record_from_message(
        chat_id=42, telegram_message_id=7, text="сколько ушло на VoiceFin?"
    )

    assert outcome.ok
    assert analyst.hours == [None]
