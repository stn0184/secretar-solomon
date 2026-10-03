"""Вопрос о деле без срока — чистые функции (`techspec/19-undated.md`).

Здесь решается всё, что не требует ни базы, ни сети: открыто ли окно,
какой сегодня день у владельца, границы отбора для `undated_to_ask` и
какими словами спросить. Сам шаг тика — в `services/reminders.py`, рядом с
напоминаниями: вопрос уходит тем же минутным циклом (§19.2).

Окно, тишина и неделя — константы, а не настройка (§19.6).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo

from solomon import texts
from solomon.db.reminders import UndatedTask

# Окно по поясу владельца (§19.2): с 10:00 и до 20:00, не включая 20:00.
# Вечером и ночью несостоявшийся вопрос не догоняет.
WINDOW_START = time(10, 0)
WINDOW_END = time(20, 0)
# Тишина перед вопросом: владелец не писал, бот не присылал напоминаний.
QUIET = timedelta(minutes=15)
# Повтор о том же деле — через столько дней по календарю (§19.1).
REPEAT_DAYS = 7
# Живой открытый вопрос, который бот не перебивает, — тот же срок, что у
# вопроса в разговоре (`services/tasks.py`, QUESTION_TTL, §10.3). Отдельной
# константой, а не импортом: `services/tasks.py` сам зовёт напоминания, и
# импорт пошёл бы по кругу. Совпадение держит тест.
QUESTION_LIFE = timedelta(hours=24)


@dataclass(frozen=True, slots=True)
class AskBounds:
    """Границы отбора `undated_to_ask` (§19.4), все — моменты с поясом.

    `day_start` — сегодняшняя полночь владельца: тронутое после неё сегодня
    не спрашивается, и вопрос после неё — «сегодня уже спрашивал».
    `asked_before` — полночь шесть дней назад: спрошенное раньше неё
    спрашивается снова. `question_since` — сутки назад, `quiet_since` —
    15 минут назад.
    """

    day_start: datetime
    asked_before: datetime
    question_since: datetime
    quiet_since: datetime


def local_day(now: datetime, timezone: tzinfo) -> date:
    """Сегодняшний день владельца — по нему процесс помнит, что уже спросил."""
    return now.astimezone(timezone).date()


def in_window(now: datetime, timezone: tzinfo) -> bool:
    """Открыто ли окно вопроса: с 10:00 до 20:00 по поясу владельца."""
    clock = now.astimezone(timezone).time()
    return WINDOW_START <= clock < WINDOW_END


def _midnight(day: date, timezone: tzinfo) -> datetime:
    return datetime.combine(day, time(0, 0), tzinfo=timezone)


def bounds(now: datetime, timezone: tzinfo) -> AskBounds:
    """Границы для базы из «сейчас» и пояса владельца (§19.4).

    Неделя — по календарю: спросил в понедельник в любой час — следующий
    вопрос не раньше следующего понедельника, поэтому граница — полночь,
    а не «семь суток назад».
    """
    today = local_day(now, timezone)
    return AskBounds(
        day_start=_midnight(today, timezone),
        asked_before=_midnight(today - timedelta(days=REPEAT_DAYS - 1), timezone),
        question_since=now - QUESTION_LIFE,
        quiet_since=now - QUIET,
    )


def question_text(task: UndatedTask, now: datetime, timezone: tzinfo) -> str:
    """Текст вопроса (§19.3): первый называет день записи, повторный — что срока нет.

    День записи — по `created_at` в поясе владельца: «Вчера» или «30
    сентября».
    """
    if task.asked_at is not None:
        return texts.undated_again(task.title)
    recorded = task.created_at.astimezone(timezone).date()
    if recorded == local_day(now, timezone) - timedelta(days=1):
        return texts.undated_first(task.title, texts.UNDATED_YESTERDAY)
    return texts.undated_first(task.title, texts.format_date(task.created_at.astimezone(timezone)))
