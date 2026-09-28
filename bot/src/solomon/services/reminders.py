"""Напоминания: расписание при записи задачи и когда постучаться.

Расписание считает база (`techspec/11-edit.md` §11.3): правило §6.1 живёт в
SQL-функции `reminder_plan`, и её зовут и бот, и правка из приложения. Сюда
она приходит протоколом `Planner`, как клиент модели в `understanding.py`:
тест подставляет свой план, а само правило проверяется тестами базы
(`supabase/tests/reminder_plan.test.ts`). В боте остаётся строка «Напомню»
(§6.4) — ближайшее из того плана, что уходит в базу.

Здесь же цикл отправки (§6.2), кнопка «Сделано» (§6.3) и строка «Перенёс»
(§11.4). Отправка приходит параметром-протоколом: сервис не знает ни про
aiogram, ни про сеть, и тест подставляет свою запись вместо неё.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from supabase import Client

from solomon import texts
from solomon.config import Settings
from solomon.db import reminders as db_reminders
from solomon.db.reminders import DueReminder, Planned
from solomon.db.rpc import DatabaseError
from solomon.db.tasks import Task
from solomon.services.understanding import Clock

logger = logging.getLogger(__name__)


class Planner(Protocol):
    """Расписание одной задачи по §6.1 — у базы. Пояс владельца знает сборка."""

    async def __call__(
        self,
        *,
        due_at: datetime | None,
        due_precision: str | None,
        kind: str,
        now: datetime,
    ) -> list[Planned]: ...


def database_planner(settings: Settings, db: Client) -> Planner:
    """Обычный планировщик: `reminder_plan` в базе с поясом из настроек."""

    async def planner(
        *, due_at: datetime | None, due_precision: str | None, kind: str, now: datetime
    ) -> list[Planned]:
        return await db_reminders.reminder_plan(
            db,
            due_at=due_at,
            due_precision=due_precision,
            kind=kind,
            timezone=settings.owner_timezone.key,
            now=now,
        )

    return planner


async def mirror_timezone(settings: Settings, db: Client) -> bool:
    """Записать пояс владельца в базу при запуске (§11.3).

    Источник правды — `.env`; база держит зеркало для `edit_task`. Не
    записалось — строка в журнале, бот работает дальше: без зеркала откажет
    только правка срока в приложении, а не приём поручений.
    """
    try:
        await db_reminders.save_owner_timezone(
            db,
            owner_telegram_id=settings.owner_telegram_id,
            timezone=settings.owner_timezone.key,
        )
    except DatabaseError as error:
        logger.error("Пояс владельца не записан в базу: %s", error)
        return False
    logger.info("Пояс владельца в базе: %s", settings.owner_timezone.key)
    return True


def next_fire_at(planned: list[Planned]) -> datetime | None:
    """Ближайшее из запланированного — о нём бот и говорит при записи (§6.4)."""
    if not planned:
        return None
    return min(item.fire_at for item in planned)


# Позднейшая ступень: созрели обе — говорит та, что ближе к сроку (§6.2).
STAGE_ORDER = {"before": 0, "due": 1}
# Тик цикла. Минута — это обещанная точность напоминания и ничем не занятый
# процесс между тиками: чаще не нужно, реже — заметно человеку.
TICK_SECONDS = 60


def by_task(reminders: Iterable[DueReminder]) -> dict[str, list[DueReminder]]:
    """Созревшее по задачам, в порядке прихода: одна задача — одно сообщение."""
    grouped: dict[str, list[DueReminder]] = {}
    for reminder in reminders:
        grouped.setdefault(reminder.task_id, []).append(reminder)
    return grouped


def latest(group: list[DueReminder]) -> DueReminder:
    """Чьими словами говорит сообщение: позднейшая ступень, потом момент."""
    return max(group, key=lambda item: (STAGE_ORDER.get(item.stage, 0), item.fire_at))


class Notifier(Protocol):
    """Отправка напоминания владельцу. Возвращает id сообщения в Telegram."""

    async def __call__(self, *, text: str, task_id: str) -> int: ...


class DueLister(Protocol):
    """Что созрело у владельца к этому моменту."""

    async def __call__(self, *, owner_telegram_id: int, now: datetime) -> list[DueReminder]: ...


class SentMarker(Protocol):
    """Отметка «ушло»: после того, как Telegram сообщение принял."""

    async def __call__(
        self, *, owner_telegram_id: int, reminder_ids: Sequence[str], telegram_message_id: int
    ) -> None: ...


class TaskCloser(Protocol):
    """Закрытие задачи по кнопке вместе с её неотправленными напоминаниями."""

    async def __call__(self, *, owner_telegram_id: int, task_id: str) -> Task | None: ...


@dataclass(frozen=True, slots=True)
class Completion:
    """Чем кончилось нажатие «Сделано» и что сказать человеку."""

    ok: bool
    answer: str


class ReminderService:
    """Цикл напоминаний и кнопка под ними. Собирается один раз при запуске."""

    def __init__(
        self,
        settings: Settings,
        due: DueLister,
        mark_sent: SentMarker,
        close_task: TaskCloser,
        notify: Notifier,
        clock: Clock | None = None,
    ) -> None:
        self._settings = settings
        self._due = due
        self._mark_sent = mark_sent
        self._close_task = close_task
        self._notify = notify
        self._clock = clock or self._now

    def _now(self) -> datetime:
        return datetime.now(self._settings.owner_timezone)

    @classmethod
    def with_database(cls, settings: Settings, db: Client, notify: Notifier) -> ReminderService:
        """Обычная сборка: настоящая база и настоящая отправка в Telegram."""

        async def due(*, owner_telegram_id: int, now: datetime) -> list[DueReminder]:
            return await db_reminders.due_reminders(
                db, owner_telegram_id=owner_telegram_id, now=now
            )

        async def mark_sent(
            *, owner_telegram_id: int, reminder_ids: Sequence[str], telegram_message_id: int
        ) -> None:
            await db_reminders.mark_sent(
                db,
                owner_telegram_id=owner_telegram_id,
                reminder_ids=reminder_ids,
                telegram_message_id=telegram_message_id,
            )

        async def close_task(*, owner_telegram_id: int, task_id: str) -> Task | None:
            return await db_reminders.mark_task_done(
                db, owner_telegram_id=owner_telegram_id, task_id=task_id
            )

        return cls(
            settings=settings,
            due=due,
            mark_sent=mark_sent,
            close_task=close_task,
            notify=notify,
        )

    async def tick(self, now: datetime | None = None) -> int:
        """Один заход: отправить созревшее и пометить отправленным (§6.2).

        Возвращает число ушедших сообщений. Отказ базы на отборе выходит
        наружу — цикл его ловит и живёт дальше; отказ на одной задаче не
        мешает остальным.
        """
        moment = now or self._clock()
        owner = self._settings.owner_telegram_id
        due = await self._due(owner_telegram_id=owner, now=moment)
        sent = 0
        for task_id, group in by_task(due).items():
            if await self._send_one(task_id, group, moment):
                sent += 1
        return sent

    async def _send_one(self, task_id: str, group: list[DueReminder], now: datetime) -> bool:
        """Одна задача — одно сообщение, и только потом отметка.

        Упало между отправкой и отметкой — следующий тик постучится второй
        раз: дубль лучше потерянного напоминания (инвариант 5).
        """
        speaker = latest(group)
        try:
            message_id = await self._notify(text=self._text_for(speaker, now), task_id=task_id)
        except Exception as error:  # noqa: BLE001 - любой отказ Telegram не роняет тик
            # Не ушло — sent_at не ставим, и следующий тик попробует снова.
            logger.warning("Напоминание по задаче %s не ушло: %s", task_id, error)
            return False
        try:
            await self._mark_sent(
                owner_telegram_id=self._settings.owner_telegram_id,
                reminder_ids=[item.id for item in group],
                telegram_message_id=message_id,
            )
        except DatabaseError as error:
            logger.warning("Напоминание по задаче %s ушло, но не помечено: %s", task_id, error)
        return True

    def _text_for(self, reminder: DueReminder, now: datetime) -> str:
        """Текст напоминания: суть и срок в поясе владельца (§6.2)."""
        timezone = self._settings.owner_timezone
        due = None
        overdue = False
        if reminder.due_at is not None:
            local = reminder.due_at.astimezone(timezone)
            due = texts.format_due_moment(local, now.astimezone(timezone))
            # «Срок был» — о сроке, а не об опоздании самого напоминания.
            overdue = reminder.due_at < now
        return texts.reminder(title=reminder.title, due=due, overdue=overdue)

    async def tick_quietly(self) -> None:
        """Тик, который не роняет бота: любая ошибка — строка в лог (§6.2)."""
        try:
            await self.tick()
        except Exception:  # цикл переживает любую ошибку тика (§6.2)
            logger.exception("Тик напоминаний не удался")

    async def run(self, interval_seconds: float = TICK_SECONDS) -> None:
        """Цикл рядом с long polling: первый тик сразу, дальше раз в минуту.

        Сразу — потому что после простоя созревшее должно уйти первым тиком
        (`spec.md` §3.4). Остановка процесса отменяет цикл штатно:
        `CancelledError` не ловится и уходит наружу.
        """
        logger.info("Цикл напоминаний запущен: тик раз в %s с", interval_seconds)
        while True:
            await self.tick_quietly()
            await asyncio.sleep(interval_seconds)

    async def complete(self, task_id: str) -> Completion:
        """Нажата кнопка «Сделано»: закрыть задачу и снять её напоминания (§6.3).

        Владелец берётся из настроек: `task_id` приходит из callback, то есть
        снаружи, и доверять ему нельзя — база сверяет владельца сама и на
        чужую задачу отвечает «не нашёл» (инвариант 2).
        """
        try:
            task = await self._close_task(
                owner_telegram_id=self._settings.owner_telegram_id, task_id=task_id
            )
        except DatabaseError as error:
            logger.warning("Задача %s не закрыта: %s", task_id, error)
            return Completion(ok=False, answer=texts.NOT_CLOSED)
        if task is None:
            logger.info("Кнопка «Сделано» по неизвестной задаче %s", task_id)
            return Completion(ok=False, answer=texts.DONE_UNKNOWN)
        logger.info("Задача %s закрыта кнопкой", task.id)
        return Completion(ok=True, answer=texts.DONE_ANSWER)
