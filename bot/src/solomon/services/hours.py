"""Часы по сферам (`techspec/31-hours.md`): сколько времени ушло на каждую сферу.

Часы берутся из двух источников (§31.1). Встреча — дело с часом и
длительностью: идёт в часы своей сферы, пока не убрана, и только та её часть,
что уже прошла. Переписка — сессии личных чатов (§25): сообщения чата подряд с
промежутками до 10 минут, среди них хотя бы одно владельца; сессия — от первого
до последнего сообщения и ещё 2 минуты, по времени площадки. Сессия идёт в
сферу чата — нынешнюю: смена сферы чата переносит и его прошлые часы.

Пересечение в пределах сферы считается один раз: две встречи в одно время,
переписка во время встречи, два чата сразу. Переписка во время встречи — часть
встречи: «встречи» и «переписка» сферы в сумме дают её итог. Пересечение
разных сфер не вычитается — у каждой своё.

Окна (§31.2) — сегодня, вчера и неделя с понедельника по поясу владельца, как
дни напоминаний (`techspec/06-reminders.md` §6.1); правый край — «сейчас».
Здесь только счёт и слова: читает слой выше (`db/tasks.py`, `db/chats.py`),
а в промпт строки кладёт `services/understanding.py`. Сети и базы здесь нет.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from solomon import texts

# Сессия переписки (§31.1): промежуток, который её ещё не рвёт, и хвост
# после последнего сообщения — время на то, чтобы его прочитать и написать.
SESSION_GAP = timedelta(minutes=10)
SESSION_TAIL = timedelta(minutes=2)
# Запас чтения до самого раннего окна: встреча длится до суток (§3.3), а
# сессия, начатая накануне, задевает окно своим концом.
LOOKBACK = timedelta(days=1)


class MeetingLike(Protocol):
    """Встреча — задача с часом и длительностью (§31.1)."""

    @property
    def start(self) -> datetime: ...

    @property
    def minutes(self) -> int: ...

    @property
    def sphere_id(self) -> str | None: ...


class StampLike(Protocol):
    """Сообщение чата для сессий: чат, его сфера, время площадки, чьё оно."""

    @property
    def thread_id(self) -> str: ...

    @property
    def sphere_id(self) -> str | None: ...

    @property
    def sent_at(self) -> datetime: ...

    @property
    def mine(self) -> bool: ...


class SphereLike(Protocol):
    """Живая сфера владельца: id и название (`techspec/30-spheres.md` §30.1)."""

    @property
    def id(self) -> str: ...

    @property
    def name(self) -> str: ...


@dataclass(frozen=True, slots=True)
class Span:
    """Отрезок времени: начало включительно, конец — нет."""

    start: datetime
    end: datetime


@dataclass(frozen=True, slots=True)
class SphereHours:
    """Часы сферы за окно в минутах: `name` — название, `None` — без сферы."""

    name: str | None
    meetings: int
    chat: int

    @property
    def total(self) -> int:
        return self.meetings + self.chat


@dataclass(frozen=True, slots=True)
class Period:
    """Окно и часы сфер в нём — только сферы, у которых что-то набралось."""

    label: str
    spheres: tuple[SphereHours, ...]


@dataclass(frozen=True, slots=True)
class _Window:
    label: str
    start: datetime
    end: datetime


def sessions(stamps: Iterable[StampLike]) -> list[tuple[str | None, Span]]:
    """Сессии переписки по чатам (§31.1): сфера чата и отрезок сессии.

    Сообщения чата — по времени площадки; следующее, отстоящее не больше чем
    на `SESSION_GAP`, продолжает сессию. Сессия без сообщения владельца не
    считается: собеседник писал, а время владельца на это не ушло.
    """
    chats: dict[str, list[StampLike]] = {}
    for stamp in stamps:
        chats.setdefault(stamp.thread_id, []).append(stamp)
    found: list[tuple[str | None, Span]] = []
    for lines in chats.values():
        ordered = sorted(lines, key=lambda stamp: stamp.sent_at)
        group = [ordered[0]]
        for stamp in ordered[1:]:
            if stamp.sent_at - group[-1].sent_at <= SESSION_GAP:
                group.append(stamp)
                continue
            found.extend(_session(group))
            group = [stamp]
        found.extend(_session(group))
    return found


def _session(group: Sequence[StampLike]) -> list[tuple[str | None, Span]]:
    """Сессия из сообщений подряд — или ничего, если владелец в ней молчал."""
    if not any(stamp.mine for stamp in group):
        return []
    span = Span(group[0].sent_at, group[-1].sent_at + SESSION_TAIL)
    return [(group[0].sphere_id, span)]


def merge(spans: Iterable[Span]) -> list[Span]:
    """Объединение отрезков: пересечения и стыки — один отрезок."""
    merged: list[Span] = []
    for span in sorted(spans, key=lambda item: item.start):
        if merged and span.start <= merged[-1].end:
            last = merged[-1]
            merged[-1] = Span(last.start, max(last.end, span.end))
        else:
            merged.append(span)
    return merged


def _clip(spans: Iterable[Span], start: datetime, end: datetime) -> list[Span]:
    """Части отрезков внутри окна; пустые выпадают."""
    clipped = [Span(max(span.start, start), min(span.end, end)) for span in spans]
    return [span for span in clipped if span.start < span.end]


def _length(spans: Iterable[Span]) -> timedelta:
    return sum((span.end - span.start for span in merge(spans)), timedelta())


def _minutes(length: timedelta) -> int:
    """Целые минуты, половина — вверх."""
    return int((length.total_seconds() + 30) // 60)


def _midnight(day: date, timezone: ZoneInfo) -> datetime:
    return datetime.combine(day, time(0, 0), tzinfo=timezone)


def _windows(now: datetime, timezone: ZoneInfo) -> list[_Window]:
    """Сегодня, вчера и неделя с понедельника — по поясу владельца (§31.2)."""
    local = now.astimezone(timezone)
    day = local.date()
    yesterday = day - timedelta(days=1)
    monday = day - timedelta(days=day.weekday())
    today_start = _midnight(day, timezone)
    return [
        _Window(f"Сегодня, {texts.format_day(local)}", today_start, local),
        _Window(
            f"Вчера, {texts.format_day(_midnight(yesterday, timezone))}",
            _midnight(yesterday, timezone),
            today_start,
        ),
        _Window(
            f"Неделя с понедельника, {texts.format_date(_midnight(monday, timezone))}",
            _midnight(monday, timezone),
            local,
        ),
    ]


def reading_since(now: datetime, timezone: ZoneInfo) -> datetime:
    """С какого момента читать встречи и сообщения: начало самого раннего окна
    (вчера или понедельник) и ещё сутки запаса."""
    return min(window.start for window in _windows(now, timezone)) - LOOKBACK


def tally(
    meetings: Iterable[MeetingLike],
    stamps: Iterable[StampLike],
    spheres: Sequence[SphereLike],
    now: datetime,
    timezone: ZoneInfo,
) -> list[Period]:
    """Часы по сферам за сегодня, вчера и неделю (§31.2).

    `meetings` — встречи, которые не убраны; `stamps` — сообщения личных
    чатов; `spheres` — живые сферы по порядку заведения: в нём сферы и идут,
    «без сферы» — последней. Сфера, которой в списке нет, — без сферы.
    Минуты округляются у итога сферы и у встреч, переписка — их разность:
    части в сумме дают итог.
    """
    names: Mapping[str, str] = {sphere.id: sphere.name for sphere in spheres}
    order: list[str | None] = [sphere.name for sphere in spheres]
    order.append(None)
    met: dict[str | None, list[Span]] = {}
    for meeting in meetings:
        key = names.get(meeting.sphere_id) if meeting.sphere_id is not None else None
        end = meeting.start + timedelta(minutes=meeting.minutes)
        met.setdefault(key, []).append(Span(meeting.start, end))
    talked: dict[str | None, list[Span]] = {}
    for sphere_id, span in sessions(stamps):
        key = names.get(sphere_id) if sphere_id is not None else None
        talked.setdefault(key, []).append(span)

    periods: list[Period] = []
    for window in _windows(now, timezone):
        end = min(window.end, now)
        found: list[SphereHours] = []
        for key in order:
            meeting_spans = _clip(met.get(key, ()), window.start, end)
            chat_spans = _clip(talked.get(key, ()), window.start, end)
            total = _minutes(_length([*meeting_spans, *chat_spans]))
            if total == 0:
                continue
            meetings_minutes = _minutes(_length(meeting_spans))
            found.append(SphereHours(key, meetings_minutes, total - meetings_minutes))
        periods.append(Period(window.label, tuple(found)))
    return periods


def hours_words(minutes: int) -> str:
    """«2 ч 10 мин», «45 мин», «3 ч»."""
    whole, rest = divmod(minutes, 60)
    if whole and rest:
        return f"{whole} ч {rest} мин"
    if whole:
        return f"{whole} ч"
    return f"{rest} мин"


def _sphere_words(item: SphereHours) -> str:
    """«VoiceFin — 2 ч 10 мин (встречи 2 ч, переписка 10 мин)»: части — только
    те, что есть."""
    parts = []
    if item.meetings:
        parts.append(f"встречи {hours_words(item.meetings)}")
    if item.chat:
        parts.append(f"переписка {hours_words(item.chat)}")
    name = item.name if item.name is not None else texts.NO_SPHERE
    return f"{name} — {hours_words(item.total)} ({', '.join(parts)})"


def lines(periods: Sequence[Period]) -> list[str]:
    """Строки блока «Часы по сферам» (§31.2): окно, сферы с разбивкой и итог.
    Пустое окно — «ни встреч, ни переписки»."""
    found = []
    for period in periods:
        if not period.spheres:
            found.append(f"{period.label}: ни встреч, ни переписки.")
            continue
        said = "; ".join(_sphere_words(item) for item in period.spheres)
        total = sum(item.total for item in period.spheres)
        found.append(f"{period.label}: {said}. Всего {hours_words(total)}.")
    return found
