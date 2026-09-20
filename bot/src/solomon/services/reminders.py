"""Напоминания: что запланировать при записи задачи и когда постучаться.

Расписание считает бот, хранит база (`techspec/06-reminders.md` §6.1):
`plan` — чистая функция от срока, точности, вида задачи и пояса владельца,
с внедряемым «сейчас». Поэтому все её ветки проверяются без часов и без сети.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

Stage = Literal["before", "due"]

# Задача, названная днём, напоминает о себе утром этого дня: полночь — рано,
# а 18:00 (`techspec/03-schema.md` §3.3) — это уже сам срок.
MORNING_HOUR = 9
# Назван час — предупреждаем за час: этого хватает, чтобы собраться.
AHEAD_OF_TIME = timedelta(hours=1)
# Виды, о которых напоминают. Идея и желание — не дела (§6.1).
REMINDED_KINDS = ("task",)


@dataclass(frozen=True, slots=True)
class Planned:
    """Одно запланированное напоминание: ступень и момент."""

    stage: Stage
    fire_at: datetime

    def as_row(self) -> dict[str, str]:
        """Строка для `record_understanding` — по именам колонок §3.5."""
        return {"stage": self.stage, "fire_at": self.fire_at.isoformat()}


def plan(
    *,
    due_at: datetime | None,
    due_precision: str | None,
    kind: str,
    timezone: ZoneInfo,
    now: datetime,
) -> list[Planned]:
    """Расписание напоминаний для одной задачи (§6.1).

    Пусто — стучаться не о чем или уже некогда: нет срока, срок прошёл, это
    идея или желание. Момент, который на этапе планирования уже прошёл, не
    заводится вовсе, а не срабатывает сразу: «сегодня в 15:00», сказанное
    в 14:30, — это одно напоминание, а не два подряд.
    """
    if kind not in REMINDED_KINDS or due_at is None or due_at <= now:
        return []

    due_local = due_at.astimezone(timezone)
    if due_precision == "time":
        before = due_local - AHEAD_OF_TIME
    else:
        before = due_local.replace(hour=MORNING_HOUR, minute=0, second=0, microsecond=0)

    planned: list[Planned] = []
    # Утро может оказаться позже самого срока, если модель назвала днём
    # что-то раньше девяти: тогда ступень «заранее» теряет смысл.
    if now < before < due_local:
        planned.append(Planned(stage="before", fire_at=before))
    planned.append(Planned(stage="due", fire_at=due_local))
    return planned


def next_fire_at(planned: list[Planned]) -> datetime | None:
    """Ближайшее из запланированного — о нём бот и говорит при записи (§6.4)."""
    if not planned:
        return None
    return min(item.fire_at for item in planned)
