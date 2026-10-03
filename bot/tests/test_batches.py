"""Пачка пересланного — окно тишины и строки переписки (`techspec/18-forwarded.md`).

Окно проверяется на подменённых часах и сне: время двигает тест, а не
настоящие секунды. Строки переписки, пределы и суть «как есть» — чистые
функции над готовыми строками, без сети и базы.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from solomon.services.batches import (
    FORWARDED_LIMIT,
    LIMIT_SECONDS,
    LINE_LIMIT,
    TEXT_LIMIT,
    WINDOW_SECONDS,
    Batches,
    Closed,
    Line,
    as_is_title,
    closes_at,
    conversation_text,
    has_words,
    head_of,
    is_conversation,
    time_label,
    to_hear,
)
from tests.conftest import OWNER_TIMEZONE

TZ = ZoneInfo(OWNER_TIMEZONE)
# Среда, 16 сентября 2026, 10:30 в поясе владельца (+05:00).
NOW = datetime(2026, 9, 16, 10, 30, tzinfo=TZ)
OWNER = "Тимур Иванов"


def at(day: int, hour: int, minute: int, month: int = 9, year: int = 2026) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=TZ)


def said(when: datetime, name: str, text: str, **fields: Any) -> Line:
    """Пересланная строка переписки."""
    return Line(sent_at=when, text=text, forwarded_from=name, **fields)


def mine(text: str, **fields: Any) -> Line:
    """Своё, не пересланное сообщение пачки — подпись."""
    return Line(sent_at=NOW, text=text, **fields)


def by_owner(when: datetime, text: str) -> Line:
    """Пересланное от самого владельца — «Владелец» в переписке."""
    return Line(sent_at=when, text=text, forwarded_from=OWNER, from_owner=True)


# ------------------------------------------------------------------ окно


class FakeTime:
    """Часы и сон, которыми управляет тест: `advance` будит уснувших, чей срок прошёл."""

    def __init__(self) -> None:
        self.now = 0.0
        self._sleepers: list[tuple[float, asyncio.Future[None]]] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._sleepers.append((self.now + seconds, future))
        await future

    async def advance(self, seconds: float) -> None:
        self.now += seconds
        waiting = []
        for wake, future in self._sleepers:
            if wake <= self.now and not future.done():
                future.set_result(None)
            elif not future.done():
                waiting.append((wake, future))
        self._sleepers = waiting
        await settle()


async def settle() -> None:
    """Дать задачам дойти до следующего ожидания."""
    for _ in range(10):
        await asyncio.sleep(0)


async def arrive[T](batches: Batches[T], chat_id: int, item: T) -> asyncio.Task[Closed[T]]:
    """Сообщение пришло: его обработчик встаёт в пачку и ждёт её закрытия."""
    joining: Coroutine[Any, Any, Closed[T]] = batches.join(chat_id, item)
    task = asyncio.create_task(joining)
    await settle()
    return task


def build() -> tuple[Batches[str], FakeTime]:
    time = FakeTime()
    return Batches[str](clock=time.clock, sleep=time.sleep), time


def test_window_and_limit_are_a_second_and_ten() -> None:
    assert WINDOW_SECONDS == 1.0
    assert LIMIT_SECONDS == 10.0


def test_batch_closes_a_second_after_the_last_message_or_ten_after_the_first() -> None:
    assert closes_at(0.0, 0.0) == 1.0
    assert closes_at(0.0, 0.4) == 1.4
    assert closes_at(0.0, 9.5) == 10.0
    assert closes_at(5.0, 14.9) == 15.0


async def test_messages_within_a_second_close_as_one_batch() -> None:
    """Подпись и пересылка с разницей в доли секунды — одна пачка (§18.1)."""
    batches, time = build()
    caption = await arrive(batches, 1, "подпись")
    await time.advance(0.3)
    forwarded = await arrive(batches, 1, "пересланное")

    await time.advance(0.9)
    assert not caption.done()
    assert not forwarded.done()

    await time.advance(0.2)
    first, second = await caption, await forwarded
    assert first.items == ("подпись", "пересланное")
    assert second.items == first.items
    assert first.seconds == 0.3


async def test_a_single_message_waits_a_second_of_silence() -> None:
    batches, time = build()
    alone = await arrive(batches, 1, "одно")

    await time.advance(0.99)
    assert not alone.done()

    await time.advance(0.02)
    assert (await alone).items == ("одно",)
    assert (await alone).seconds == 0.0


async def test_messages_more_than_a_second_apart_are_separate() -> None:
    batches, time = build()
    first = await arrive(batches, 1, "первое")
    await time.advance(1.2)
    second = await arrive(batches, 1, "второе")
    await time.advance(1.1)

    assert (await first).items == ("первое",)
    assert (await second).items == ("второе",)


async def test_a_stream_closes_on_the_tenth_second_from_the_first() -> None:
    """Поток без перерывов не держит ответ: пачка закрывается на 10-й секунде."""
    batches, time = build()
    stream = []
    for number in range(20):
        stream.append(await arrive(batches, 1, f"сообщение {number}"))
        await time.advance(0.5)

    # t = 10.0: пачка закрылась, хотя последнее пришло полсекунды назад.
    closed = await stream[0]
    assert len(closed.items) == 20
    assert closed.seconds == 9.5

    late = await arrive(batches, 1, "после предела")
    await time.advance(1.0)
    assert (await late).items == ("после предела",)


async def test_chats_have_separate_batches() -> None:
    batches, time = build()
    one = await arrive(batches, 1, "первый чат")
    other = await arrive(batches, 2, "второй чат")
    await time.advance(1.0)

    assert (await one).items == ("первый чат",)
    assert (await other).items == ("второй чат",)


async def test_a_cancelled_first_message_still_releases_the_batch() -> None:
    """Обработчик первого сообщения снят — остальные не висят вечно."""
    batches, time = build()
    first = await arrive(batches, 1, "первое")
    second = await arrive(batches, 1, "второе")

    first.cancel()
    await settle()

    assert (await second).items == ("первое", "второе")
    assert first.cancelled()
    # Следующее сообщение открывает новую пачку.
    third = await arrive(batches, 1, "третье")
    await time.advance(1.0)
    assert (await third).items == ("третье",)


# ---------------------------------------------------------------- состав


def test_two_messages_with_a_forwarded_one_make_a_conversation() -> None:
    """Два сообщения и больше, хотя бы одно переслано, — переписка (§18.1)."""
    forwarded = said(at(15, 21, 40), "Рената", "Завтра в силе?")
    assert is_conversation([mine("напомни в пятницу"), forwarded])
    assert is_conversation([forwarded, said(at(15, 21, 52), "Рената", "Во сколько?")])


def test_single_and_own_messages_are_not_a_conversation() -> None:
    """Одно пересланное без подписи и свои сообщения — по одному, как раньше."""
    assert not is_conversation([said(at(15, 21, 40), "Рената", "Завтра в силе?")])
    assert not is_conversation([mine("купить хлеб")])
    assert not is_conversation([mine("купить хлеб"), mine("и молоко")])
    assert not is_conversation([])


def test_head_is_the_last_forwarded_message() -> None:
    lines = [
        mine("напомни в пятницу"),
        said(at(15, 21, 40), "Рената", "Завтра в силе?"),
        said(at(15, 21, 52), "Рената", "Во сколько?"),
        mine("и ещё одно"),
    ]
    assert head_of(lines) == 2
    assert head_of([mine("своё")]) is None


def test_voices_heard_are_the_last_thirty_forwarded_and_the_caption() -> None:
    """Распознаются только голосовые из последних 30 и свои (§18.2)."""
    voices = [
        said(at(15, 10, 0) + timedelta(minutes=number), "Рената", "", speech="voice")
        for number in range(FORWARDED_LIMIT + 2)
    ]
    lines = [mine("", speech="voice"), said(at(15, 9, 0), "Рената", "текст"), *voices]

    assert to_hear(lines) == [0, *range(4, len(lines))]


def test_words_are_any_text_or_heard_speech() -> None:
    unheard = said(at(15, 21, 40), "Рената", "", speech="voice", heard=False)
    assert not has_words([unheard, mine("", speech="voice", heard=False)])
    assert has_words([unheard, mine("напомни в пятницу")])
    assert has_words([unheard, said(at(15, 21, 52), "Рената", "Во сколько?")])
    assert has_words([said(at(15, 21, 52), "Рената", "Захвати договор", speech="voice")])
    assert not has_words([said(at(15, 21, 52), "Рената", "  \n ")])


# ----------------------------------------------------------------- строки


def test_time_label_names_today_yesterday_or_the_date() -> None:
    """Время — в поясе владельца: «сегодня», «вчера», раньше — дата (§18.2)."""
    assert time_label(at(16, 8, 15), NOW, TZ) == "сегодня 08:15"
    assert time_label(at(15, 21, 40), NOW, TZ) == "вчера 21:40"
    assert time_label(at(14, 18, 5), NOW, TZ) == "14.09 18:05"
    assert time_label(at(3, 10, 0, month=11, year=2025), NOW, TZ) == "03.11.2025 10:00"
    # 19:30 UTC 15-го — уже 00:30 16-го у владельца.
    assert time_label(datetime(2026, 9, 15, 19, 30, tzinfo=UTC), NOW, TZ) == "сегодня 00:30"


def test_conversation_reads_like_the_techspec_example() -> None:
    """Пример §18.2: строки от старых к новым, «Владелец», голос, подпись последней."""
    lines = [
        mine("напомни ответить до обеда"),
        said(at(15, 21, 40), "Рената", "Привет! Завтра в силе?"),
        by_owner(at(15, 21, 41), "Да, только время уточню"),
        said(at(15, 21, 52), "Рената", "Во сколько тогда?"),
        said(at(16, 8, 15), "Рената", "Захвати договор, пожалуйста", speech="voice"),
    ]

    assert conversation_text(lines, NOW, TZ) == "\n".join(
        [
            "Переписка (сообщений: 4):",
            "вчера 21:40 Рената: Привет! Завтра в силе?",
            "вчера 21:41 Владелец: Да, только время уточню",
            "вчера 21:52 Рената: Во сколько тогда?",
            "сегодня 08:15 Рената: [голосовое] Захвати договор, пожалуйста",
            "Подпись владельца: напомни ответить до обеда",
        ]
    )


def test_without_a_caption_there_is_no_caption_line() -> None:
    lines = [
        said(at(15, 21, 40), "Рената", "Завтра в силе?"),
        said(at(15, 21, 52), "Чат «Дача»", "Во сколько?"),
    ]
    text = conversation_text(lines, NOW, TZ)
    assert "Подпись владельца" not in text
    assert text.splitlines()[-1] == "вчера 21:52 Чат «Дача»: Во сколько?"


def test_speech_marks_and_unheard_speech() -> None:
    lines = [
        said(at(16, 8, 0), "Рената", "", speech="voice", heard=False),
        said(at(16, 8, 1), "Рената", "Жду у входа", speech="video_note"),
        said(at(16, 8, 2), "Рената", "", speech="video_note", heard=False),
        mine("напомни в пятницу", speech="voice"),
    ]
    assert conversation_text(lines, NOW, TZ).splitlines()[1:] == [
        "сегодня 08:00 Рената: [голосовое, не расслышал]",
        "сегодня 08:01 Рената: [кружок] Жду у входа",
        "сегодня 08:02 Рената: [кружок, не расслышал]",
        "Подпись владельца: [голосовое] напомни в пятницу",
    ]


def test_message_newlines_collapse_and_captions_join() -> None:
    """Одно сообщение — одна строка; свои сообщения — одной подписью по порядку."""
    lines = [
        mine("напомни\nв пятницу"),
        said(at(15, 21, 40), "Рената", "Привет!\n\nЗавтра   в силе?"),
        mine("и запиши, что я обещал"),
    ]
    assert conversation_text(lines, NOW, TZ).splitlines()[1:] == [
        "вчера 21:40 Рената: Привет! Завтра в силе?",
        "Подпись владельца: напомни в пятницу и запиши, что я обещал",
    ]


def test_long_message_and_caption_are_cut_in_the_middle() -> None:
    long = "начало " + "а" * 3000 + " конец"
    lines = [mine(long), said(at(15, 21, 40), "Рената", long)]
    message, caption = conversation_text(lines, NOW, TZ).splitlines()[1:]

    body = message.removeprefix("вчера 21:40 Рената: ")
    assert len(body) == LINE_LIMIT
    assert body.startswith("начало ")
    assert body.endswith(" конец")
    assert "…" in body
    assert len(caption.removeprefix("Подпись владельца: ")) == LINE_LIMIT


def test_only_the_last_thirty_forwarded_are_shown() -> None:
    """Больше 30 пересланных — первая строка называет, сколько показано (§18.2)."""
    lines = [
        said(at(15, 10, 0) + timedelta(minutes=number), "Рената", f"строка {number}")
        for number in range(45)
    ]
    shown = conversation_text(lines, NOW, TZ).splitlines()

    assert shown[0] == "Переписка (сообщений: 45, показаны последние 30):"
    assert len(shown) == 1 + FORWARDED_LIMIT
    assert shown[1].endswith(": строка 15")
    assert shown[-1].endswith(": строка 44")


def test_oldest_lines_drop_out_past_eight_thousand() -> None:
    """Не влезает в 8000 знаков — выпадают самые старые строки, подпись остаётся."""
    lines = [
        mine("напомни в пятницу"),
        *(
            said(at(15, 21, number), "Рената", f"{number:02d}" + "б" * (LINE_LIMIT - 2))
            for number in range(10)
        ),
    ]
    text = conversation_text(lines, NOW, TZ)
    shown = text.splitlines()

    assert len(text) <= TEXT_LIMIT
    assert shown[0] == "Переписка (сообщений: 10, показаны последние 7):"
    assert shown[1].startswith("вчера 21:03 Рената: 03")
    assert shown[-2].startswith("вчера 21:09 Рената: 09")
    assert shown[-1] == "Подпись владельца: напомни в пятницу"


# ------------------------------------------------------------- «как есть»


def test_as_is_title_names_the_people_in_order() -> None:
    """Суть «как есть» (§18.4): собеседники по порядку, без «Владельца»."""
    lines = [
        said(at(15, 21, 40), "Рената", "Привет"),
        by_owner(at(15, 21, 41), "Привет"),
        said(at(15, 21, 42), "Аня", "И я тут"),
        said(at(15, 21, 43), "Рената", "Завтра в силе?"),
    ]
    assert as_is_title(lines) == "Переписка: Рената, Аня"


def test_as_is_title_with_more_than_three_people_and_a_caption() -> None:
    lines = [
        mine("напомни\nв пятницу"),
        *(
            said(at(15, 21, number), name, "Привет")
            for number, name in enumerate(["Рената", "Аня", "Олег", "Чат «Дача»"])
        ),
    ]
    assert as_is_title(lines) == "Переписка: Рената, Аня, Олег и другие — напомни в пятницу"


def test_as_is_title_without_people_or_heard_caption() -> None:
    lines = [
        mine("", speech="voice", heard=False),
        by_owner(at(15, 21, 41), "Пришлю в пятницу"),
    ]
    assert as_is_title(lines) == "Переписка"
