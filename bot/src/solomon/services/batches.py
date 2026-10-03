"""Пачка пересланного и строки переписки (`techspec/18-forwarded.md` §18.1–18.2).

Telegram присылает пересланные сообщения по одному, но разом, а подпись к
пересылке — отдельным сообщением перед ними. `Batches` собирает текст и
речь одного чата окном тишины: пачка закрывается, когда секунду не
приходит нового, но не позже десяти секунд от первого сообщения. Пачка
живёт в памяти процесса — бот один (`techspec/16-server.md` §16.3).

Остальное — чистые функции над готовыми строками: переписка ли пачка, чья
она голова, какие голосовые распознавать, текст переписки для модели с
пределами и суть задачи «как есть». Сети, базы и aiogram здесь нет: часы
и сон окна подменяются в тестах.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from typing import Literal

from solomon.services.conversation import cut_middle

# Окно тишины и предел пачки (§18.1): подпись и пересылка уходят из Telegram
# двумя запросами с разницей в доли секунды; дольше — заметная задержка у
# каждого сообщения. Журнал пишет, за сколько собралась переписка, — по этим
# числам окно и подбирается.
WINDOW_SECONDS = 1.0
LIMIT_SECONDS = 10.0
# Пределы переписки (§18.2): последние 30 пересланных, сообщение и подпись —
# до 1000 знаков (длиннее режутся посередине), весь текст — до 8000.
FORWARDED_LIMIT = 30
LINE_LIMIT = 1000
TEXT_LIMIT = 8000
# Сколько собеседников назвать в сути «как есть» (§18.4); больше — «и другие».
NAMES_LIMIT = 3
OWNER_NAME = "Владелец"
CAPTION_LABEL = "Подпись владельца"
AS_IS_TITLE = "Переписка"
MORE_NAMES = "и другие"
UNHEARD = "не расслышал"

Speech = Literal["voice", "video_note"]
SPEECH_MARKS: dict[Speech, str] = {"voice": "голосовое", "video_note": "кружок"}


@dataclass(frozen=True, slots=True)
class Line:
    """Сообщение пачки, как его видит переписка.

    `sent_at` — время исходного сообщения (`forward_origin.date`), у своего —
    время отправки. `forwarded_from` — от кого переслано (§17.5), у своего
    `None`; `from_owner` — переслано от самого владельца. `speech` — голос
    или кружок: `text` у них — расшифровка, `heard = False` — не расслышано.
    """

    sent_at: datetime
    text: str = ""
    forwarded_from: str | None = None
    from_owner: bool = False
    speech: Speech | None = None
    heard: bool = True

    @property
    def forwarded(self) -> bool:
        return self.forwarded_from is not None


def closes_at(
    started: float,
    last: float,
    *,
    window: float = WINDOW_SECONDS,
    limit: float = LIMIT_SECONDS,
) -> float:
    """Когда закрывается пачка: секунда тишины после последнего, но не позже предела."""
    return min(last + window, started + limit)


def head_of(lines: Sequence[Line]) -> int | None:
    """Голова переписки — последнее пересланное сообщение (§18.3); нет такого — `None`."""
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].forwarded:
            return index
    return None


def is_conversation(lines: Sequence[Line]) -> bool:
    """Два сообщения и больше, хотя бы одно переслано (§18.1). Иначе — по одному."""
    return len(lines) >= 2 and head_of(lines) is not None


def _kept(lines: Sequence[Line]) -> list[int]:
    """Номера строк, которые попадают в переписку: свои и последние 30 пересланных."""
    forwarded = [index for index, line in enumerate(lines) if line.forwarded]
    dropped = set(forwarded[:-FORWARDED_LIMIT])
    return [index for index in range(len(lines)) if index not in dropped]


def to_hear(lines: Sequence[Line]) -> list[int]:
    """Номера голосовых, которые распознаются: свои и из последних 30 пересланных."""
    return [index for index in _kept(lines) if lines[index].speech is not None]


def _flat(text: str) -> str:
    """Одно сообщение — одна строка: переводы строк и лишние пробелы схлопнуты."""
    return " ".join(text.split())


def has_words(lines: Sequence[Line]) -> bool:
    """Есть ли что читать модели: текст или расслышанная речь в переписке или подписи.

    Нет — голосовые не расслышаны, а текста нет: модель не зовётся (§18.4).
    """
    return any(lines[index].heard and _flat(lines[index].text) for index in _kept(lines))


def time_label(sent_at: datetime, now: datetime, timezone: tzinfo) -> str:
    """Время строки в поясе владельца: «сегодня 08:15», «вчера 21:40», «14.09 18:05».

    Другой год — с годом: «03.11.2025 10:00», иначе прошлогодняя дата
    читалась бы как ближайшая.
    """
    local = sent_at.astimezone(timezone)
    today = now.astimezone(timezone).date()
    clock = f"{local:%H:%M}"
    if local.date() == today:
        return f"сегодня {clock}"
    if local.date() == today - timedelta(days=1):
        return f"вчера {clock}"
    if local.year == today.year:
        return f"{local:%d.%m} {clock}"
    return f"{local:%d.%m.%Y} {clock}"


def _content(line: Line) -> str:
    """Текст строки: схлопнутый, не длиннее предела; речь — с пометкой."""
    body = cut_middle(_flat(line.text), LINE_LIMIT)
    if line.speech is None:
        return body
    mark = SPEECH_MARKS[line.speech]
    if not line.heard or not body:
        return f"[{mark}, {UNHEARD}]"
    return f"[{mark}] {body}"


def _name(line: Line) -> str:
    """Кто писал: «Владелец» или имя, как в «Переслано от» (§17.5)."""
    if line.from_owner:
        return OWNER_NAME
    return _flat(line.forwarded_from or "")


def _header(total: int, shown: int) -> str:
    if shown < total:
        return f"Переписка (сообщений: {total}, показаны последние {shown}):"
    return f"Переписка (сообщений: {total}):"


def conversation_text(lines: Sequence[Line], now: datetime, timezone: tzinfo) -> str:
    """Сообщение `user` переписки (§18.2).

    Первая строка — сколько сообщений и сколько показано, дальше пересланные
    от старых к новым «время имя: текст», последней — «Подпись владельца: …»
    из своих сообщений пачки по порядку. Пересланных больше 30 или текст
    длиннее 8000 знаков — выпадают самые старые строки; подпись остаётся.
    """
    forwarded = [line for line in lines if line.forwarded]
    body = [
        f"{time_label(line.sent_at, now, timezone)} {_name(line)}: {_content(line)}"
        for line in forwarded[-FORWARDED_LIMIT:]
    ]
    own = (_content(line) for line in lines if not line.forwarded)
    caption = " ".join(part for part in own if part)
    tail = [f"{CAPTION_LABEL}: {cut_middle(caption, LINE_LIMIT)}"] if caption else []
    while True:
        text = "\n".join([_header(len(forwarded), len(body)), *body, *tail])
        if len(text) <= TEXT_LIMIT or len(body) <= 1:
            return text
        body.pop(0)


def as_is_title(lines: Sequence[Line]) -> str:
    """Суть задачи «как есть» при отказе модели (§18.4).

    «Переписка: Рената, Аня» — собеседники в порядке появления, без
    «Владельца»; больше трёх — первые три и «и другие». С подписью —
    «… — <подпись>». Собеседников нет — просто «Переписка».
    """
    names: list[str] = []
    for line in lines:
        name = _flat(line.forwarded_from or "")
        if line.forwarded and not line.from_owner and name and name not in names:
            names.append(name)
    title = AS_IS_TITLE
    if names:
        named = ", ".join(names[:NAMES_LIMIT])
        if len(names) > NAMES_LIMIT:
            named = f"{named} {MORE_NAMES}"
        title = f"{AS_IS_TITLE}: {named}"
    caption = " ".join(
        _flat(line.text) for line in lines if not line.forwarded and line.heard and _flat(line.text)
    )
    return f"{title} — {caption}" if caption else title


@dataclass(frozen=True, slots=True)
class Closed[T]:
    """Закрытая пачка: сообщения в порядке прихода и сколько секунд она собиралась
    — от первого сообщения до последнего; для журнала (§18.1)."""

    items: tuple[T, ...]
    seconds: float


class _Open[T]:
    """Пачка, которая ещё собирается."""

    def __init__(self, item: T, started: float) -> None:
        self.items = [item]
        self.started = started
        self.last = started
        self.done = asyncio.Event()

    def closed(self) -> Closed[T]:
        return Closed(items=tuple(self.items), seconds=self.last - self.started)


Sleep = Callable[[float], Awaitable[None]]


class Batches[T]:
    """Пачки по чатам. Каждое сообщение встаёт в пачку своего чата и ждёт её
    закрытия; закрытую пачку получают все её участники разом.

    Первое сообщение пачки держит окно: спит, пока не пройдёт секунда тишины
    или предел. Пришедшее после закрытия, даже пока первое не проснулось,
    открывает новую пачку. Обработчик первого снят — пачка всё равно
    закрывается, и остальные не ждут вечно.
    """

    def __init__(
        self,
        *,
        window: float = WINDOW_SECONDS,
        limit: float = LIMIT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._window = window
        self._limit = limit
        self._clock = clock
        self._sleep = sleep
        self._open: dict[int, _Open[T]] = {}

    def _deadline(self, batch: _Open[T]) -> float:
        return closes_at(batch.started, batch.last, window=self._window, limit=self._limit)

    async def join(self, chat_id: int, item: T) -> Closed[T]:
        """Встать в пачку чата и дождаться её закрытия."""
        now = self._clock()
        batch = self._open.get(chat_id)
        if batch is not None and now < self._deadline(batch):
            batch.items.append(item)
            batch.last = now
            await batch.done.wait()
            return batch.closed()

        batch = _Open(item, now)
        self._open[chat_id] = batch
        try:
            while (left := self._deadline(batch) - self._clock()) > 0:
                await self._sleep(left)
        finally:
            if self._open.get(chat_id) is batch:
                del self._open[chat_id]
            batch.done.set()
        return batch.closed()
