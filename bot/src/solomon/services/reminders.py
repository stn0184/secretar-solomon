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

Повторяющиеся задачи (`techspec/13-repeat.md`): тик сначала перекатывает
пропущенные разы (§13.4), кнопка «Сделано» несёт раз и переводит задачу на
следующий (§13.3) — и то и другое делает база.

Вопрос о деле без срока (`techspec/19-undated.md`) — последний шаг того же
тика (§19.2): окно, границы и слова — в `services/asks.py`, отбор и запись —
в базе, здесь — порядок «отправить → записать» и память о дате вопроса.

Утренний план (`techspec/20-morning-plan.md`) — шаг сразу после
перекатывания, до созревших напоминаний (§20.2): окно, границы дня и строки —
в `services/morning.py`, дела дня и память о плане — в базе, здесь — тот же
порядок «отправить → записать» и дата плана в памяти процесса.

Вопрос о прошедшем деле (`techspec/22-overdue.md`) — абзацем в утреннем
плане и отдельным шагом тика перед вопросом о деле без срока (§22.2): окно,
границы и слова — в `services/overdue.py`, отбор и запись — в базе, здесь —
«отправить → записать» и память о делах, о которых процесс спрашивал сегодня.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, tzinfo
from typing import Protocol

from supabase import Client

from solomon import texts
from solomon.config import Settings
from solomon.db import morning as db_morning
from solomon.db import reminders as db_reminders
from solomon.db.morning import DayTask
from solomon.db.reminders import DueReminder, MovedTask, OverdueTask, Planned, UndatedTask
from solomon.db.rpc import DatabaseError
from solomon.db.tasks import ACTIVE_STATUS, TIME_PRECISION, TaskDetails
from solomon.services import asks, morning, overdue
from solomon.services.repeat import occurrence_seconds
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


def past_due(due_at: datetime, precision: str | None, now: datetime, timezone: tzinfo) -> bool:
    """Прошёл ли срок — «Срок был» вместо «Срок» (`techspec/21-part-of-day.md` §21.3).

    У срока со временем — раньше начала текущей минуты: напоминание, ушедшее
    в минуту срока тиком с секундами, — ещё «Срок». У дела на день и части
    дня — день срока раньше сегодняшнего по поясу владельца: часть,
    догнавшая после простоя в тот же день, — всё ещё «Срок».
    """
    if precision == TIME_PRECISION:
        return due_at < now.replace(second=0, microsecond=0)
    return due_at.astimezone(timezone).date() < now.astimezone(timezone).date()


class Notifier(Protocol):
    """Отправка напоминания владельцу. Возвращает id сообщения в Telegram.

    `occurrence` — раз повторяющейся задачи в секундах Unix: он уезжает в
    кнопку «Сделано» (§13.3); у разовой его нет.
    """

    async def __call__(self, *, text: str, task_id: str, occurrence: int | None = None) -> int: ...


class DueLister(Protocol):
    """Что созрело у владельца к этому моменту."""

    async def __call__(self, *, owner_telegram_id: int, now: datetime) -> list[DueReminder]: ...


class SentMarker(Protocol):
    """Отметка «ушло»: после того, как Telegram сообщение принял."""

    async def __call__(
        self, *, owner_telegram_id: int, reminder_ids: Sequence[str], telegram_message_id: int
    ) -> None: ...


class MovedLister(Protocol):
    """Задачи с отметкой «срок перенесён» — у владельца, сейчас."""

    async def __call__(self, *, owner_telegram_id: int) -> list[MovedTask]: ...


class MovedClearer(Protocol):
    """Снять прочитанную отметку. `False` — она сменилась и остаётся."""

    async def __call__(self, *, owner_telegram_id: int, task_id: str, seen: datetime) -> bool: ...


class Announcer(Protocol):
    """Строка в чат владельца без кнопки. Возвращает id сообщения."""

    async def __call__(self, *, text: str) -> int: ...


class TaskCloser(Protocol):
    """Кнопка «Сделано»: разовую задачу закрыть, повторяющуюся перевести (§13.3).

    Возвращает задачу, какой она стала, — или какой была, если раз не тот.
    """

    async def __call__(
        self, *, owner_telegram_id: int, task_id: str, occurrence: int | None = None
    ) -> TaskDetails | None: ...


class Roller(Protocol):
    """Перекатывание пропущенных разов (§13.4). Возвращает, сколько задач ушло."""

    async def __call__(self, *, owner_telegram_id: int, now: datetime) -> int: ...


class UndatedFinder(Protocol):
    """О каком деле без срока спросить сейчас (§19.4): одно дело или `None`."""

    async def __call__(
        self, *, owner_telegram_id: int, bounds: asks.AskBounds
    ) -> UndatedTask | None: ...


class AskRecorder(Protocol):
    """Записать ушедший вопрос (§19.4). `None` — дело уже не то, не записано."""

    async def __call__(
        self, *, owner_telegram_id: int, task_id: str, question: str, telegram_message_id: int
    ) -> TaskDetails | None: ...


class OverdueFinder(Protocol):
    """О каком прошедшем деле спросить сейчас (§22.4): одно дело или `None`."""

    async def __call__(
        self, *, owner_telegram_id: int, bounds: overdue.OverdueBounds
    ) -> OverdueTask | None: ...


class OverdueRecorder(Protocol):
    """Записать ушедший вопрос о прошедшем деле (§22.4). `None` — не записано.

    `telegram_message_id` у вопроса в плане — `None`: свайп на план —
    обычное сообщение.
    """

    async def __call__(
        self,
        *,
        owner_telegram_id: int,
        task_id: str,
        question: str,
        telegram_message_id: int | None,
    ) -> TaskDetails | None: ...


class PlanChecker(Protocol):
    """Был ли у владельца утренний план за этот день (§20.4)."""

    async def __call__(self, *, owner_telegram_id: int, day: date) -> bool: ...


class DayTaskLister(Protocol):
    """Дела владельца со сроком в границах дня (§20.1), в порядке базы."""

    async def __call__(
        self, *, owner_telegram_id: int, bounds: morning.DayBounds
    ) -> list[DayTask]: ...


class PlanRecorder(Protocol):
    """Записать ушедший план (§20.4). `False` — план за день уже записан."""

    async def __call__(
        self, *, owner_telegram_id: int, day: date, telegram_message_id: int
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class Completion:
    """Чем кончилось нажатие «Сделано» и что сказать человеку.

    `mark` — отметка под напоминанием: у повторяющейся задачи она называет
    следующий раз, какой вернула база (§13.3).
    """

    ok: bool
    answer: str
    mark: str = texts.DONE_MARK


class ReminderService:
    """Цикл напоминаний и кнопка под ними. Собирается один раз при запуске."""

    def __init__(
        self,
        settings: Settings,
        due: DueLister,
        mark_sent: SentMarker,
        close_task: TaskCloser,
        notify: Notifier,
        moved: MovedLister,
        clear_moved: MovedClearer,
        announce: Announcer,
        clock: Clock | None = None,
        roll: Roller | None = None,
        undated: UndatedFinder | None = None,
        record_ask: AskRecorder | None = None,
        plan_sent: PlanChecker | None = None,
        day_tasks: DayTaskLister | None = None,
        record_plan: PlanRecorder | None = None,
        overdue_task: OverdueFinder | None = None,
        record_overdue: OverdueRecorder | None = None,
    ) -> None:
        self._settings = settings
        self._due = due
        self._mark_sent = mark_sent
        self._close_task = close_task
        self._notify = notify
        self._moved = moved
        self._clear_moved = clear_moved
        self._announce = announce
        # Без перекатывания пропущенный раз стоит до «Сделано» — как до этапа
        # 011. Обычная сборка его подключает.
        self._roll = roll
        # Без них вопроса о деле без срока нет — как до этапа 018 (§19.2).
        self._undated = undated
        self._record_ask = record_ask
        # День владельца, когда процесс задал вопрос: второго в этот день не
        # будет, даже если база вопрос не записала (§19.4). Бот один (§16.3).
        self._asked_on: date | None = None
        # Без них утреннего плана нет — как до этапа 019 (§20.2).
        self._plan_sent = plan_sent
        self._day_tasks = day_tasks
        self._record_plan = record_plan
        # День владельца, когда план ушёл или нашёлся в базе: до завтра шаг
        # в базу не ходит, и второго плана нет, даже если запись не удалась.
        self._planned_on: date | None = None
        # Без них вопроса о прошедшем деле нет — ни в плане, ни отдельно, как
        # до этапа 022 (§22.2).
        self._overdue_task = overdue_task
        self._record_overdue = record_overdue
        # Дела, о которых процесс спрашивал в этот день владельца (§22.4): по
        # ним вопрос получает «Ещё», и по ним ловится незаписанный вопрос —
        # тогда до завтра шаг больше не спрашивает. Бот один (§16.3).
        self._overdue_on: date | None = None
        self._overdue_asked: set[str] = set()
        self._overdue_stuck = False
        self._clock = clock or self._now

    def _now(self) -> datetime:
        return datetime.now(self._settings.owner_timezone)

    @classmethod
    def with_database(
        cls, settings: Settings, db: Client, notify: Notifier, announce: Announcer
    ) -> ReminderService:
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

        async def close_task(
            *, owner_telegram_id: int, task_id: str, occurrence: int | None = None
        ) -> TaskDetails | None:
            return await db_reminders.mark_task_done(
                db, owner_telegram_id=owner_telegram_id, task_id=task_id, occurrence=occurrence
            )

        async def roll(*, owner_telegram_id: int, now: datetime) -> int:
            return await db_reminders.roll_repeats(db, owner_telegram_id=owner_telegram_id, now=now)

        async def moved(*, owner_telegram_id: int) -> list[MovedTask]:
            return await db_reminders.moved_tasks(db, owner_telegram_id=owner_telegram_id)

        async def clear_moved(*, owner_telegram_id: int, task_id: str, seen: datetime) -> bool:
            return await db_reminders.clear_due_moved(
                db, owner_telegram_id=owner_telegram_id, task_id=task_id, seen=seen
            )

        async def undated(*, owner_telegram_id: int, bounds: asks.AskBounds) -> UndatedTask | None:
            return await db_reminders.undated_to_ask(
                db,
                owner_telegram_id=owner_telegram_id,
                day_start=bounds.day_start,
                asked_before=bounds.asked_before,
                question_since=bounds.question_since,
                quiet_since=bounds.quiet_since,
            )

        async def record_ask(
            *, owner_telegram_id: int, task_id: str, question: str, telegram_message_id: int
        ) -> TaskDetails | None:
            return await db_reminders.record_ask(
                db,
                owner_telegram_id=owner_telegram_id,
                task_id=task_id,
                question=question,
                telegram_message_id=telegram_message_id,
            )

        async def plan_sent(*, owner_telegram_id: int, day: date) -> bool:
            return await db_morning.morning_plan_sent(
                db, owner_telegram_id=owner_telegram_id, day=day
            )

        async def day_tasks(*, owner_telegram_id: int, bounds: morning.DayBounds) -> list[DayTask]:
            return await db_morning.day_tasks(
                db,
                owner_telegram_id=owner_telegram_id,
                day_start=bounds.day_start,
                day_end=bounds.day_end,
            )

        async def record_plan(
            *, owner_telegram_id: int, day: date, telegram_message_id: int
        ) -> bool:
            return await db_morning.record_morning_plan(
                db,
                owner_telegram_id=owner_telegram_id,
                day=day,
                telegram_message_id=telegram_message_id,
            )

        async def overdue_task(
            *, owner_telegram_id: int, bounds: overdue.OverdueBounds
        ) -> OverdueTask | None:
            return await db_reminders.overdue_to_ask(
                db,
                owner_telegram_id=owner_telegram_id,
                day_start=bounds.day_start,
                asked_before=bounds.asked_before,
                question_since=bounds.question_since,
                quiet_since=bounds.quiet_since,
            )

        async def record_overdue(
            *,
            owner_telegram_id: int,
            task_id: str,
            question: str,
            telegram_message_id: int | None,
        ) -> TaskDetails | None:
            return await db_reminders.record_overdue_ask(
                db,
                owner_telegram_id=owner_telegram_id,
                task_id=task_id,
                question=question,
                telegram_message_id=telegram_message_id,
            )

        return cls(
            settings=settings,
            due=due,
            mark_sent=mark_sent,
            close_task=close_task,
            notify=notify,
            moved=moved,
            clear_moved=clear_moved,
            announce=announce,
            roll=roll,
            undated=undated,
            record_ask=record_ask,
            plan_sent=plan_sent,
            day_tasks=day_tasks,
            record_plan=record_plan,
            overdue_task=overdue_task,
            record_overdue=record_overdue,
        )

    async def tick(self, now: datetime | None = None) -> int:
        """Один заход: перекатывание (§13.4), утренний план (§20.2), созревшее
        (§6.2), строки «Перенёс» (§11.4), затем вопрос о прошедшем деле
        (§22.2) и последним — вопрос о деле без срока (§19.2): каждый из двух
        вопросов — только если до него в этом тике ничего не ушло.

        Порядок нарочно такой: новый раз получает свои ступени до выборки, и
        созревшая уходит этим же тиком; план называет дела уже на сегодняшнем
        разе и идёт до напоминаний — сначала обзор дня; напоминание, ушедшее в
        этом тике, уже помечено, и «Напомню» в строке о переносе его не
        назовёт. Возвращает число ушедших сообщений, план — среди них. Сбой
        перекатывания или сбора плана — строка в журнал, тик идёт дальше.
        Отказ базы на отборе выходит наружу — цикл его ловит и живёт дальше;
        отказ на одной задаче не мешает остальным.
        """
        moment = now or self._clock()
        owner = self._settings.owner_telegram_id
        await self._roll_quietly(moment)
        sent = 1 if await self._send_plan(moment) else 0
        due = await self._due(owner_telegram_id=owner, now=moment)
        for task_id, group in by_task(due).items():
            if await self._send_one(task_id, group, moment):
                sent += 1
        for task in await self._moved(owner_telegram_id=owner):
            if await self._announce_one(task, moment):
                sent += 1
        # В этом тике уже ушли план, напоминание или «Перенёс» — тишины нет
        # (§19.2, §20.2, §22.2). Пока идут вопросы о прошедших делах, вопрос о
        # деле без срока ждёт: ушедший вопрос — тоже не тишина.
        if sent == 0 and await self._ask_overdue(moment):
            sent += 1
        if sent == 0 and await self._ask_undated(moment):
            sent += 1
        return sent

    async def _send_plan(self, now: datetime) -> bool:
        """Утренний план (§20.4): отправить и только потом записать.

        Окно и «сегодня план уже был» решает бот; был ли план в базе и дела
        дня — база, по границам из `services/morning.py`. Сбой сбора — строка
        в журнал, тик идёт к напоминаниям, и следующий тик до 12:00 попробует
        снова. Не ушло — ничего не записано, следующий тик пришлёт снова.
        Ушло — процесс помнит день, и второго плана сегодня не будет, даже
        если запись не удалась. Возвращает, ушёл ли план.

        Абзац плана — вопрос о прошедшем деле (§22.2): отбор с границами
        плана, сбой отбора — план без абзаца. Записывается вопрос после плана
        и без id сообщения.
        """
        if self._plan_sent is None or self._day_tasks is None or self._record_plan is None:
            return False
        timezone = self._settings.owner_timezone
        if not morning.in_window(now, timezone):
            return False
        bounds = morning.day_bounds(now, timezone)
        if self._planned_on == bounds.day:
            return False
        owner = self._settings.owner_telegram_id
        try:
            if await self._plan_sent(owner_telegram_id=owner, day=bounds.day):
                self._planned_on = bounds.day
                return False
            tasks = await self._day_tasks(owner_telegram_id=owner, bounds=bounds)
        except DatabaseError as error:
            logger.error("Утренний план не собран: %s", error)
            return False
        asked = await self._pick_overdue(now, overdue.plan_bounds(now, timezone))
        question = None if asked is None else self._overdue_question(asked, now)
        try:
            message_id = await self._announce(
                text=morning.plan_text(tasks, timezone, question=question)
            )
        except Exception as error:  # noqa: BLE001 - любой отказ Telegram не роняет тик
            logger.warning("Утренний план не ушёл: %s", error)
            return False
        self._planned_on = bounds.day
        # Суть дел в журнал не пишется: только сколько их (§20.4).
        logger.info("Утренний план ушёл: дел %s", len(tasks))
        try:
            recorded = await self._record_plan(
                owner_telegram_id=owner, day=bounds.day, telegram_message_id=message_id
            )
        except DatabaseError as error:
            logger.error("Утренний план ушёл, но не записан: %s", error)
        else:
            if not recorded:
                logger.warning("Утренний план ушёл, но не записан: план за этот день уже есть")
        if asked is not None:
            await self._record_overdue_question(asked, now, None)
        return True

    async def _ask_overdue(self, now: datetime) -> bool:
        """Вопрос о прошедшем деле отдельным сообщением (§22.2, §22.4).

        Окно, «до 12:00 — только после плана» и память о делах дня решает
        бот; живой вопрос, тишину и само дело — `overdue_to_ask` по границам
        шага. Сбой отбора — строка в журнал, шаг кончился. Не ушло — ничего
        не записано, следующий тик спросит снова. Ушло — записать с id
        сообщения: свайп на вопрос называет задачу. Возвращает, ушёл ли
        вопрос.
        """
        if self._overdue_task is None or self._record_overdue is None:
            return False
        timezone = self._settings.owner_timezone
        if not overdue.in_window(now, timezone):
            return False
        today = asks.local_day(now, timezone)
        # Первый вопрос дня звучит в плане, а не перед ним: пока план может
        # прийти (до 12:00), шаг ждёт сегодняшнего плана; с 12:00 план не
        # догоняется, и шаг идёт без него.
        if morning.in_window(now, timezone) and self._planned_on != today:
            return False
        self._overdue_today(today)
        if self._overdue_stuck:
            return False
        task = await self._pick_overdue(now, overdue.step_bounds(now, timezone))
        if task is None:
            return False
        question = self._overdue_question(task, now)
        try:
            message_id = await self._notify(text=question, task_id=task.task_id)
        except Exception as error:  # noqa: BLE001 - любой отказ Telegram не роняет тик
            logger.warning("Вопрос о прошедшем деле %s не ушёл: %s", task.task_id, error)
            return False
        await self._record_overdue_question(task, now, message_id)
        return True

    def _overdue_today(self, today: date) -> set[str]:
        """Дела, о которых процесс спрашивал сегодня; новый день — память пуста."""
        if self._overdue_on != today:
            self._overdue_on = today
            self._overdue_asked = set()
            self._overdue_stuck = False
        return self._overdue_asked

    async def _pick_overdue(
        self, now: datetime, bounds: overdue.OverdueBounds
    ) -> OverdueTask | None:
        """Прошедшее дело для вопроса (§22.4) или `None`; сбой — строка в журнал.

        Отбор вернул дело, о котором процесс сегодня уже спрашивал, — значит,
        запись вопроса не легла: до завтра о прошедших делах больше не
        спрашиваем, иначе вопрос уходил бы каждую минуту.
        """
        if self._overdue_task is None or self._record_overdue is None:
            return None
        owner = self._settings.owner_telegram_id
        try:
            task = await self._overdue_task(owner_telegram_id=owner, bounds=bounds)
        except DatabaseError as error:
            logger.error("Прошедшее дело для вопроса не выбрано: %s", error)
            return None
        if task is None:
            return None
        if task.task_id in self._overdue_today(asks.local_day(now, self._settings.owner_timezone)):
            self._overdue_stuck = True
            logger.warning(
                "Вопрос о прошедшем деле %s сегодня уже был и не записан: до завтра не спрашиваю",
                task.task_id,
            )
            return None
        return task

    def _overdue_question(self, task: OverdueTask, now: datetime) -> str:
        """Слова вопроса; «Ещё» — если сегодня процесс уже спрашивал (§22.3)."""
        timezone = self._settings.owner_timezone
        more = bool(self._overdue_today(asks.local_day(now, timezone)))
        return overdue.question_text(task, now, timezone, more=more)

    async def _record_overdue_question(
        self, task: OverdueTask, now: datetime, message_id: int | None
    ) -> None:
        """Ушедший вопрос — в память дня, в журнал и в базу (§22.4).

        Открытым вопросом задачи пишется `OVERDUE_QUESTION`, а не текст
        сообщения: по нему бот узнаёт свой вопрос в ответе (§22.5). Сбой
        записи — строка в журнал: вопрос уже ушёл.
        """
        if self._record_overdue is None:
            return
        self._overdue_today(asks.local_day(now, self._settings.owner_timezone)).add(task.task_id)
        # Суть дела в журнал не пишется: id, какой это вопрос и где (§22.4).
        which = "повторный" if task.asked_at is not None else "первый"
        where = "в плане" if message_id is None else "отдельно"
        logger.info("Вопрос о прошедшем деле %s: %s, %s", task.task_id, which, where)
        try:
            recorded = await self._record_overdue(
                owner_telegram_id=self._settings.owner_telegram_id,
                task_id=task.task_id,
                question=texts.OVERDUE_QUESTION,
                telegram_message_id=message_id,
            )
        except DatabaseError as error:
            logger.error("Вопрос о прошедшем деле %s ушёл, но не записан: %s", task.task_id, error)
            return
        if recorded is None:
            logger.warning(
                "Вопрос о прошедшем деле %s ушёл, но не записан: дело закрыли или перенесли",
                task.task_id,
            )

    async def _ask_undated(self, now: datetime) -> bool:
        """Вопрос о деле без срока (§19.4): отправить и только потом записать.

        Окно и «сегодня процесс уже спрашивал» решает бот, остальное —
        `undated_to_ask` по границам из `services/asks.py`. Сбой отбора —
        строка в журнал, тик живёт. Не ушло — ничего не записано, следующий
        тик спросит снова. Ушло — процесс помнит день, и второго вопроса
        сегодня не будет, даже если запись не удалась. Возвращает, ушёл ли
        вопрос.
        """
        if self._undated is None or self._record_ask is None:
            return False
        timezone = self._settings.owner_timezone
        today = asks.local_day(now, timezone)
        if self._asked_on == today or not asks.in_window(now, timezone):
            return False
        owner = self._settings.owner_telegram_id
        try:
            task = await self._undated(owner_telegram_id=owner, bounds=asks.bounds(now, timezone))
        except DatabaseError as error:
            logger.error("Дело без срока для вопроса не выбрано: %s", error)
            return False
        if task is None:
            return False
        try:
            message_id = await self._notify(
                text=asks.question_text(task, now, timezone), task_id=task.task_id
            )
        except Exception as error:  # noqa: BLE001 - любой отказ Telegram не роняет тик
            logger.warning("Вопрос о задаче %s не ушёл: %s", task.task_id, error)
            return False
        self._asked_on = today
        # Суть дела в журнал не пишется: id и какой это вопрос (§19.4).
        which = "повторный" if task.asked_at is not None else "первый"
        logger.info("Вопрос о задаче %s: %s", task.task_id, which)
        try:
            recorded = await self._record_ask(
                owner_telegram_id=owner,
                task_id=task.task_id,
                question=texts.UNDATED_QUESTION,
                telegram_message_id=message_id,
            )
        except DatabaseError as error:
            logger.error("Вопрос о задаче %s ушёл, но не записан: %s", task.task_id, error)
            return True
        if recorded is None:
            logger.warning(
                "Вопрос о задаче %s ушёл, но не записан: дело получило срок или закрыто",
                task.task_id,
            )
        return True

    async def _roll_quietly(self, now: datetime) -> None:
        """Перевести просроченные разы на наступившие (§13.4); сбой — строка в журнал."""
        if self._roll is None:
            return
        try:
            rolled = await self._roll(owner_telegram_id=self._settings.owner_telegram_id, now=now)
        except DatabaseError as error:
            logger.error("Повторяющиеся задачи не перекатились: %s", error)
            return
        if rolled:
            logger.info("Перекатилось повторяющихся задач: %s", rolled)

    async def _announce_one(self, task: MovedTask, now: datetime) -> bool:
        """Строка «Перенёс» и только потом снятие прочитанной отметки.

        Не ушло — отметка остаётся до следующего тика. Ушло, а снять не
        вышло — строка в журнал и, возможно, повтор: дубль лучше потери.
        """
        try:
            await self._announce(text=self._moved_text(task, now))
        except Exception as error:  # noqa: BLE001 - любой отказ Telegram не роняет тик
            logger.warning("Строка о переносе задачи %s не ушла: %s", task.id, error)
            return False
        try:
            cleared = await self._clear_moved(
                owner_telegram_id=self._settings.owner_telegram_id,
                task_id=task.id,
                seen=task.due_moved_at,
            )
        except DatabaseError as error:
            logger.warning(
                "Строка о переносе задачи %s ушла, но отметка не снята: %s", task.id, error
            )
            return True
        if not cleared:
            logger.info("Отметка переноса задачи %s сменилась — скажу о новом сроке", task.id)
        return True

    def _moved_text(self, task: MovedTask, now: datetime) -> str:
        """Строка из того, что лежит в базе сейчас, в поясе владельца (§11.4).

        Срок в прошлом — без «Напомню» (§6.4); ближайшее напоминание, которое
        уже должно было уйти (бот лежал), тоже не обещается. У повторяющейся
        задачи — строка «Повтор» (§13.6).
        """
        if task.due_at is None:
            return texts.moved_reply(title=task.title, due=None, remind_at=None)
        timezone = self._settings.owner_timezone
        due = texts.format_due(task.due_at.astimezone(timezone), task.due_precision)
        remind_at = None
        if task.due_at > now and task.next_fire_at is not None and task.next_fire_at > now:
            remind_at = texts.format_remind_at(
                task.next_fire_at.astimezone(timezone), now.astimezone(timezone)
            )
        repeat = texts.repeat_words(task.repeat) if task.repeat is not None else None
        return texts.moved_reply(title=task.title, due=due, remind_at=remind_at, repeat=repeat)

    async def _send_one(self, task_id: str, group: list[DueReminder], now: datetime) -> bool:
        """Одна задача — одно сообщение, и только потом отметка.

        Упало между отправкой и отметкой — следующий тик постучится второй
        раз: дубль лучше потерянного напоминания (инвариант 5).
        """
        speaker = latest(group)
        occurrence = None
        if speaker.repeat is not None and speaker.occurrence_at is not None:
            occurrence = occurrence_seconds(speaker.occurrence_at)
        try:
            message_id = await self._notify(
                text=self._text_for(speaker, now), task_id=task_id, occurrence=occurrence
            )
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
        """Текст напоминания: суть и срок в поясе владельца (§6.2, §21.3)."""
        timezone = self._settings.owner_timezone
        due = None
        overdue = False
        if reminder.due_at is not None:
            local = reminder.due_at.astimezone(timezone)
            precision = reminder.due_precision
            due = texts.format_due_moment(local, precision, now.astimezone(timezone))
            # «Срок был» — о сроке, а не об опоздании самого напоминания.
            overdue = past_due(reminder.due_at, precision, now, timezone)
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

    async def complete(self, task_id: str, occurrence: int | None = None) -> Completion:
        """Нажата кнопка «Сделано»: закрыть задачу и снять её напоминания (§6.3).

        Владелец берётся из настроек: `task_id` приходит из callback, то есть
        снаружи, и доверять ему нельзя — база сверяет владельца сама и на
        чужую задачу отвечает «не нашёл» (инвариант 2).

        Повторяющаяся задача не закрывается, а переходит на следующий раз
        (§13.3): база переводит её, только если она стоит на разе
        `occurrence`, и возвращает задачу — отметка называет срок из ответа,
        и когда перевела она, и когда задача уже ушла дальше.
        """
        try:
            task = await self._close_task(
                owner_telegram_id=self._settings.owner_telegram_id,
                task_id=task_id,
                occurrence=occurrence,
            )
        except DatabaseError as error:
            logger.warning("Задача %s не закрыта: %s", task_id, error)
            return Completion(ok=False, answer=texts.NOT_CLOSED)
        if task is None:
            logger.info("Кнопка «Сделано» по неизвестной задаче %s", task_id)
            return Completion(ok=False, answer=texts.DONE_UNKNOWN)
        if task.repeat is not None and task.status == ACTIVE_STATUS and task.due_at is not None:
            timezone = self._settings.owner_timezone
            due = texts.format_due(task.due_at.astimezone(timezone), task.due_precision)
            logger.info("Задача %s по кнопке на следующем разе", task.id)
            return Completion(
                ok=True,
                answer=texts.NEXT_ANSWER.format(due=due),
                mark=texts.DONE_NEXT.format(due=due),
            )
        logger.info("Задача %s закрыта кнопкой", task.id)
        return Completion(ok=True, answer=texts.DONE_ANSWER)
