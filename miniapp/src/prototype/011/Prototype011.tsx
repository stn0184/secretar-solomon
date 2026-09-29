/**
 * Прототип этапа 011 — повторяющиеся задачи. Dev-only витрина: маршрут
 * `/prototype/011` регистрируется в `main.tsx` под `import.meta.env.DEV`,
 * в сборку не попадает.
 *
 * Экраны собраны из настоящих компонентов (`Header`, `Field`, метки,
 * `Choice`, `SourceMessage`, `Reminders`, `Actions`, `Tabs`) на моках из
 * `mock.ts`. Новое — отметка «↻» в строке списка, строка «Сделано.
 * Следующий раз» после «Сделано», поле «Повтор» в карточке и выбор
 * повтора в форме (шаг, дни флажками, число или последний день): их
 * разметка и стили (`prototype.css`) — предложение, при реализации
 * переезжают в `components/` и `styles.css`.
 *
 * Логики нет: кнопки не нажимаются, форма не отправляется.
 */

import type { ReactNode } from "react";

import { Actions } from "../../components/Actions.tsx";
import { Choice } from "../../components/Choice.tsx";
import { KindChip, PriorityChip, PromiseChip, ReviewChip } from "../../components/Chip.tsx";
import { Field } from "../../components/Field.tsx";
import { Header } from "../../components/Header.tsx";
import { Reminders } from "../../components/Reminders.tsx";
import { SourceMessage } from "../../components/SourceMessage.tsx";
import { Tabs } from "../../components/Tabs.tsx";
import {
  countActive,
  countOverdue,
  dateInputValue,
  formatDay,
  formatDue,
  formatRecorded,
  sameDay,
  timeInputValue,
} from "../../lib/format.ts";
import {
  type GroupKey,
  type Reminder,
  type SourceMessage as Source,
  groupTasks,
  isOverdue,
} from "../../lib/tasks.ts";
import {
  BARE,
  BARE_SOURCE,
  LIST,
  LIST_AFTER_DONE,
  NOW,
  type ProtoTask,
  REPORT,
  REPORT_REMINDERS,
  REPORT_SOURCE,
  STANDUP,
  STANDUP_NEXT,
  STANDUP_REMINDERS,
  STANDUP_SOURCE,
} from "./mock.ts";
import "./prototype.css";

/* ── Тема: `?scheme=dark` — ночная тема Telegram (значения одобренного 006).
   Вне Telegram переменных --tg-theme-* нет, поэтому они ставятся здесь. ── */

const DARK_THEME: Record<string, string> = {
  "--tg-theme-secondary-bg-color": "#17212b",
  "--tg-theme-bg-color": "#232e3c",
  "--tg-theme-section-bg-color": "#232e3c",
  "--tg-theme-section-separator-color": "#2c3a4a",
  "--tg-theme-text-color": "#ffffff",
  "--tg-theme-subtitle-text-color": "#93a6b8",
  "--tg-theme-hint-color": "#6d7f8f",
  "--tg-theme-button-color": "#6ab2f2",
  "--tg-theme-button-text-color": "#0d1117",
  "--tg-theme-destructive-text-color": "#ee7a72",
};

const DARK = new URLSearchParams(window.location.search).get("scheme") === "dark";
const html = document.documentElement;
html.dataset.scheme = DARK ? "dark" : "light";
html.style.colorScheme = DARK ? "dark" : "light";
if (DARK) {
  for (const [name, value] of Object.entries(DARK_THEME)) {
    html.style.setProperty(name, value);
  }
}

const noop = () => {};

/* ── Рама: телефон с шапкой Telegram. Не продукт — бумага под ним. ── */

function Phone({
  n,
  title,
  note,
  overlay,
  children,
}: {
  n: number;
  title: string;
  note: string;
  overlay?: ReactNode;
  children: ReactNode;
}) {
  return (
    <figure className="p11-board">
      <figcaption className="p11-cap">
        <b>
          {n} · {title}
        </b>
        <span>{note}</span>
      </figcaption>
      <div className="p11-phone">
        <div className="p11-tg" aria-hidden="true">
          <span className="p11-tg__side">✕ Закрыть</span>
          <span className="p11-tg__mid">
            <b>Соломон</b>
            <small>мини-приложение</small>
          </span>
          <span className="p11-tg__side p11-tg__side--end">⋯</span>
        </div>
        <div className="p11-viewport">{children}</div>
        {overlay}
      </div>
    </figure>
  );
}

/** Кусок формы без телефона — чтобы сравнить правила бок о бок. */
function Fragment({
  n,
  title,
  note,
  children,
}: {
  n: string;
  title: string;
  note: string;
  children: ReactNode;
}) {
  return (
    <figure className="p11-board">
      <figcaption className="p11-cap p11-cap--frag">
        <b>
          {n} · {title}
        </b>
        <span>{note}</span>
      </figcaption>
      <div className="p11-frag">{children}</div>
    </figure>
  );
}

function PencilIcon() {
  return (
    <svg
      viewBox="0 0 20 20"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M12.8 3.7l3.5 3.5L7.5 16H4v-3.5z" />
      <path d="M11 5.5l3.5 3.5" />
    </svg>
  );
}

/* ── Список: строка как TaskRow, плюс «↻ правило» рядом со сроком ── */

function Row({ task, group, next }: { task: ProtoTask; group: GroupKey; next: boolean }) {
  const overdue = isOverdue(task, NOW);
  const markClass = overdue
    ? "row__mark row__mark--overdue"
    : task.priority === "high"
      ? "row__mark row__mark--high"
      : "row__mark";
  const whenClass = overdue
    ? "row__when row__when--overdue"
    : group === "today"
      ? "row__when row__when--today"
      : "row__when";

  return (
    <button type="button" className="row">
      <span className={markClass} aria-hidden="true" />
      <span className="row__body">
        <span className="row__title">{task.title}</span>
        <span className="row__meta">
          <KindChip task={task} />
          <span className={whenClass}>
            {task.dueAt
              ? formatDue(task.dueAt, task.duePrecision, NOW)
              : formatRecorded(task.createdAt)}
          </span>
          {/* новое: правило коротко, сразу за сроком — цветом подписи, не меткой */}
          {task.rep ? (
            <span className="row__rep">
              <i aria-hidden="true">↻</i>
              {task.rep.short}
            </span>
          ) : null}
          <PriorityChip task={task} compact />
          <PromiseChip task={task} compact />
          <ReviewChip task={task} />
        </span>
        {/* новое: что сделала база после «Сделано» в карточке — до следующего чтения списка */}
        {next && task.dueAt ? (
          <span className="row__next">
            ✓ Сделано. Следующий раз: {formatDue(task.dueAt, task.duePrecision, NOW)}
          </span>
        ) : null}
      </span>
      <span className="row__chev" aria-hidden="true">
        ›
      </span>
    </button>
  );
}

function List({ tasks, justDone }: { tasks: ProtoTask[]; justDone?: string }) {
  const groups = groupTasks(tasks, NOW);
  const overdue = groups.find((g) => g.key === "overdue")?.tasks.length ?? 0;
  const active = countActive(tasks.length);
  const subtitle = overdue > 0 ? `${active} · ${countOverdue(overdue)}` : active;

  return (
    <main className="screen screen--tabs">
      <Header title="Задачи" subtitle={subtitle} />
      {groups.map((group) => (
        <section className="sec" key={group.key}>
          <h2 className={group.key === "overdue" ? "sec__h sec__h--overdue" : "sec__h"}>
            <span>{group.title}</span>
            {group.key === "today" ? <em>{formatDay(NOW)}</em> : null}
          </h2>
          <div className="card">
            {(group.tasks as ProtoTask[]).map((task) => (
              <Row key={task.id} task={task} group={group.key} next={task.id === justDone} />
            ))}
          </div>
        </section>
      ))}
      <Tabs active="tasks" onChange={noop} />
    </main>
  );
}

/* ── Карточка: как TaskCard, плюс поле «Повтор» ── */

function Card({
  task,
  source,
  reminders,
}: {
  task: ProtoTask;
  source: Source;
  reminders: Reminder[];
}) {
  const dueToday = task.dueAt !== null && sameDay(task.dueAt, NOW);
  return (
    <main className="screen">
      <Header title={task.title} onBack={noop} />

      <div className="card">
        <div className="kv">
          <Field label="Срок" note={dueToday && task.dueAt ? formatDay(task.dueAt) : undefined}>
            {task.dueAt ? formatDue(task.dueAt, task.duePrecision, NOW) : "не назван"}
          </Field>
          {/* новое: правило словами, как у бота; у разовой задачи строки нет */}
          {task.rep ? (
            <Field label="Повтор" note="«Сделано» переведёт задачу на следующий раз">
              {task.rep.words}
            </Field>
          ) : null}
          {task.priority === "high" ? (
            <Field label="Приоритет">
              <PriorityChip task={task} compact={false} />
            </Field>
          ) : null}
          {task.promise ? (
            <Field label="Обещание">
              <PromiseChip task={task} compact={false} />
            </Field>
          ) : null}
          {task.people.length > 0 ? <Field label="Люди">{task.people.join(", ")}</Field> : null}
        </div>
        <button type="button" className="kv__edit">
          <PencilIcon />
          Изменить
        </button>
      </div>

      <SourceMessage message={source} now={NOW} />
      <Reminders reminders={reminders} hasDue={task.dueAt !== null} now={NOW} />

      <Actions
        busy={null}
        error={null}
        primary={{ kind: "done", label: "Сделано", busyLabel: "Отмечаем…", onClick: noop }}
        onDelete={noop}
      />
    </main>
  );
}

/** Системное окно Telegram (`showPopup`) — рисует мессенджер, здесь только его вид. */
function DeletePopup({ title }: { title: string }) {
  return (
    <div className="p11-dim">
      <div className="p11-popup" role="dialog">
        <b>Удалить задачу со всеми повторами?</b>
        <p>
          «{title}» исчезнет вместе с напоминаниями — и этот раз, и все следующие. Сообщение в
          переписке останется.
        </p>
        <div className="p11-popup__btns">
          <span>Отмена</span>
          <span className="p11-popup__danger">Удалить</span>
        </div>
      </div>
    </div>
  );
}

/* ── Форма: как TaskEdit, плюс ряд «Повтор» сразу под сроком ── */

type Every = "none" | "day" | "week" | "month" | "year";

const EVERY = [
  ["none", "Нет"],
  ["day", "Дни"],
  ["week", "Недели"],
  ["month", "Месяцы"],
  ["year", "Годы"],
] as const satisfies readonly (readonly [Every, string])[];

const WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"];

/** Дни недели флажками: отметить можно несколько, отмеченные — цветом действия. */
function Days({ on }: { on: number[] }) {
  return (
    <div className="days" role="group" aria-label="Дни недели">
      {WEEKDAYS.map((day, i) => (
        <label className="days__opt" key={day}>
          <input type="checkbox" checked={on.includes(i + 1)} onChange={noop} />
          <span>{day}</span>
        </label>
      ))}
    </div>
  );
}

interface RepeatSetup {
  every: Every;
  /** «Каждую», число, «неделю» — слова вокруг шага в нужном падеже. */
  step?: [string, number, string];
  days?: number[];
  /** У месяцев: число из даты или последний день. */
  monthDay?: { date: string; last: boolean };
  /** У лет: день и месяц — из срока. */
  yearDate?: string;
  /** Правило словами — как его назовёт бот. */
  words?: string;
  /** Выбор изменён: срок станет первым разом. */
  changed?: boolean;
  disabled?: boolean;
}

function RepeatRow({ setup }: { setup: RepeatSetup }) {
  return (
    <div className="form__row">
      <span className="form__k">Повтор</span>
      <div className="rep__every">
        <Choice
          label="Повтор"
          options={EVERY}
          value={setup.every}
          disabled={setup.disabled}
          onChange={noop}
        />
      </div>
      {setup.step ? (
        <div className="rep__step">
          <span>{setup.step[0]}</span>
          <input
            type="number"
            className="input input--step"
            aria-label="Шаг"
            inputMode="numeric"
            min={1}
            max={99}
            value={setup.step[1]}
            onChange={noop}
          />
          <span>{setup.step[2]}</span>
        </div>
      ) : null}
      {setup.days ? <Days on={setup.days} /> : null}
      {setup.monthDay ? (
        <Choice
          label="День месяца"
          options={[
            ["date", setup.monthDay.date],
            ["last", "В последний день"],
          ]}
          value={setup.monthDay.last ? "last" : "date"}
          onChange={noop}
        />
      ) : null}
      {setup.yearDate ? (
        <p className="form__hint">День и месяц — из срока: {setup.yearDate}.</p>
      ) : null}
      {setup.disabled ? (
        <p className="form__hint">Повтор можно выбрать, когда выбран день.</p>
      ) : null}
      {setup.words ? (
        <p className="rep__words">
          Получается: <b>{setup.words}</b>.
          {setup.changed ? " Срок выше станет первым разом." : null}
        </p>
      ) : null}
    </div>
  );
}

function DueRow({ task, hints, day }: { task: ProtoTask; hints: string[]; day?: Date }) {
  const noDue = task.dueAt === null;
  const at = day ?? task.dueAt;
  return (
    <div className="form__row">
      <span className="form__k">Срок</span>
      <div className="form__due">
        <input
          type="date"
          className="input"
          aria-label="День"
          value={dateInputValue(at)}
          disabled={noDue}
          onChange={noop}
        />
        <input
          type="time"
          className="input"
          aria-label="Час"
          value={task.duePrecision === "time" ? timeInputValue(task.dueAt) : ""}
          disabled={noDue}
          onChange={noop}
        />
      </div>
      <label className="check">
        <input type="checkbox" checked={noDue} onChange={noop} />
        <span>Без срока</span>
      </label>
      {hints.map((hint) => (
        <p className="form__hint" key={hint}>
          {hint}
        </p>
      ))}
    </div>
  );
}

function Form({
  task,
  source,
  hints,
  repeat,
}: {
  task: ProtoTask;
  source: Source;
  hints: string[];
  repeat: RepeatSetup;
}) {
  return (
    <main className="screen">
      <Header title="Правка задачи" onBack={noop} backLabel="Задача" />

      <div className="card form">
        <label className="form__row">
          <span className="form__k">Суть</span>
          <textarea className="input input--area" rows={2} value={task.title} onChange={noop} />
        </label>

        <DueRow task={task} hints={hints} />
        <RepeatRow setup={repeat} />

        <div className="form__row">
          <span className="form__k">Вид</span>
          <Choice
            label="Вид"
            options={[
              ["task", "Задача"],
              ["idea", "Идея"],
              ["wish", "Желание"],
            ]}
            value={task.kind}
            onChange={noop}
          />
        </div>

        <div className="form__row">
          <span className="form__k">Срочность</span>
          <Choice
            label="Срочность"
            options={[
              ["low", "Низкая"],
              ["normal", "Обычная"],
              ["high", "Высокая"],
            ]}
            value={task.priority}
            onChange={noop}
          />
        </div>

        <div className="form__row">
          <span className="form__k">Обещание</span>
          <Choice
            label="Обещание"
            options={[
              ["none", "Нет"],
              ["mine", "Я обещал"],
              ["to_me", "Мне обещали"],
            ]}
            value={task.promise ?? "none"}
            onChange={noop}
          />
        </div>

        <label className="form__row">
          <span className="form__k">Люди</span>
          <input
            type="text"
            className="input"
            value={task.people.join(", ")}
            placeholder="Например: Кузнецов, Анна"
            onChange={noop}
          />
          <span className="form__hint">Через запятую.</span>
        </label>
      </div>

      <SourceMessage message={source} now={NOW} />

      <Actions
        busy={null}
        error={null}
        primary={{ kind: "save", label: "Сохранить", busyLabel: "Сохраняем…", onClick: noop }}
        secondary={{ label: "Отмена", onClick: noop }}
      />
    </main>
  );
}

/** Ряды «Срок» и «Повтор» — как они стоят в форме. */
function FormPiece({ hints, day, repeat }: { hints: string[]; day?: Date; repeat: RepeatSetup }) {
  return (
    <div className="card form">
      <DueRow task={REPORT} hints={hints} day={day} />
      <RepeatRow setup={repeat} />
    </div>
  );
}

const DAY_HINT = "Без часа — напомню в 09:00 и в 18:00 этого дня.";

/* ── Полотно: экраны слева направо — путь человека ── */

export function Prototype011() {
  return (
    <div className="p11">
      <header className="p11-head">
        <h1>Прототип 011 — повторяющиеся задачи</h1>
        <p>
          Слева направо — путь владельца: список → карточка повторяющейся задачи → «Сделано» → тот
          же список. Второй ряд — правка повтора в форме, третий — каждое правило бок о бок, внизу
          — пустое. Кнопки не работают, все данные выдуманы, «сейчас» — вторник, 29 сентября, 10:40.
        </p>
        <p className="p11-links">
          Тема: <a href="?">светлая</a> · <a href="?scheme=dark">тёмная</a>
        </p>
      </header>

      <h2 className="p11-sub">«Сделано» у повторяющейся задачи</h2>
      <div className="p11-strip">
        <Phone
          n={1}
          title="Список: «↻» рядом со сроком"
          note="Повторяющаяся задача — одна строка, правило коротко после срока. Просроченный полив ждёт до начала следующего раза (четверг, 00:00) и тогда молча перейдёт."
        >
          <List tasks={LIST} />
        </Phone>

        <Phone
          n={2}
          title="Карточка: поле «Повтор»"
          note="Правило словами, как у бота; подпись под ним — что сделает «Сделано». Кнопки те же, что у разовой."
        >
          <Card task={STANDUP} source={STANDUP_SOURCE} reminders={STANDUP_REMINDERS} />
        </Phone>

        <Phone
          n={3}
          title="После «Сделано»: список"
          note="Задача не ушла: база перевела её на следующий будний день, строка переехала в «На неделе». Зелёная строка — те же слова, что у кнопки под напоминанием; держится до следующего чтения списка."
        >
          <List tasks={LIST_AFTER_DONE} justDone={STANDUP_NEXT.id} />
        </Phone>

        <Phone
          n={4}
          title="«Удалить» у повторяющейся"
          note="Системное окно Telegram, как в 005: удаляется вся серия, отдельного удаления раза нет."
          overlay={<DeletePopup title={STANDUP.title} />}
        >
          <Card task={STANDUP} source={STANDUP_SOURCE} reminders={STANDUP_REMINDERS} />
        </Phone>
      </div>

      <h2 className="p11-sub">Правка повтора</h2>
      <div className="p11-strip">
        <Phone
          n={5}
          title="Карточка еженедельной задачи"
          note="«Изменить» — на прежнем месте; форма откроется на этом же экране."
        >
          <Card task={REPORT} source={REPORT_SOURCE} reminders={REPORT_REMINDERS} />
        </Phone>

        <Phone
          n={6}
          title="Форма: повтор не меняли"
          note="«Повтор» — сразу под сроком: без даты его не выбрать. Под сроком сказано, что дата и час меняют только этот раз."
        >
          <Form
            task={REPORT}
            source={REPORT_SOURCE}
            hints={[DAY_HINT, "Дата и час меняют только этот раз — повтор останется прежним."]}
            repeat={{
              every: "week",
              step: ["Каждую", 1, "неделю"],
              days: [1],
              words: "каждый понедельник",
            }}
          />
        </Phone>
      </div>

      <h2 className="p11-sub">Каждое правило — ряды «Срок» и «Повтор» той же формы</h2>
      <p className="p11-lead">
        Та же задача, срок — понедельник, 5 октября, без часа. Во всех кусках выбор изменён, поэтому
        под правилом — «Срок выше станет первым разом», а строки «только этот раз» под сроком нет.
      </p>
      <div className="p11-strip p11-strip--wrap">
        <Fragment n="7а" title="По дням" note="Шаг — числом; каждые 2 дня бот назовёт «через день».">
          <FormPiece
            hints={[DAY_HINT]}
            repeat={{ every: "day", step: ["Каждые", 2, "дня"], words: "через день", changed: true }}
          />
        </Fragment>

        <Fragment
          n="7б"
          title="По неделям: по будням"
          note="Дни — флажками; при выборе отмечен день из даты, остальные человек отметил сам."
        >
          <FormPiece
            hints={[DAY_HINT]}
            repeat={{
              every: "week",
              step: ["Каждую", 1, "неделю"],
              days: [1, 2, 3, 4, 5],
              words: "по будням",
              changed: true,
            }}
          />
        </Fragment>

        <Fragment
          n="7в"
          title="Раз в две недели, вторник"
          note="Понедельник снят, вторник отмечен: дата в правило не попадает и передвинулась на ближайший вторник."
        >
          <FormPiece
            day={new Date(2026, 9, 6, 18, 0)}
            hints={[
              "Дата передвинута на вторник, 6 октября — в понедельник повтор не попадает.",
              DAY_HINT,
            ]}
            repeat={{
              every: "week",
              step: ["Каждые", 2, "недели"],
              days: [2],
              words: "каждые 2 недели по вторникам",
              changed: true,
            }}
          />
        </Fragment>

        <Fragment
          n="7г"
          title="По месяцам: число из даты"
          note="Число — из срока; второй вариант — последний день месяца."
        >
          <FormPiece
            hints={[DAY_HINT]}
            repeat={{
              every: "month",
              step: ["Каждые", 3, "месяца"],
              monthDay: { date: "5-го", last: false },
              words: "каждые 3 месяца 5-го",
              changed: true,
            }}
          />
        </Fragment>

        <Fragment
          n="7д"
          title="В последний день месяца"
          note="5 октября — не последний день: дата передвинулась на 31 октября."
        >
          <FormPiece
            day={new Date(2026, 9, 31, 18, 0)}
            hints={["Дата передвинута на 31 октября — последний день месяца.", DAY_HINT]}
            repeat={{
              every: "month",
              step: ["Каждый", 1, "месяц"],
              monthDay: { date: "31-го", last: true },
              words: "в последний день месяца",
              changed: true,
            }}
          />
        </Fragment>

        <Fragment n="7е" title="По годам" note="Выбирать нечего: день и месяц — из срока.">
          <FormPiece
            hints={[DAY_HINT]}
            repeat={{
              every: "year",
              step: ["Каждый", 1, "год"],
              yearDate: "5 октября",
              words: "каждый год 5 октября",
              changed: true,
            }}
          />
        </Fragment>
      </div>

      <h2 className="p11-sub">Пустое: у задачи нет срока — повторять нечего</h2>
      <div className="p11-strip">
        <Phone
          n={8}
          title="Форма без срока"
          note="«Без срока» отмечено — выбор повтора заперт на «Нет» и объясняет почему. Так же он сбрасывается, если вид — идея или желание."
        >
          <Form
            task={BARE}
            source={BARE_SOURCE}
            hints={["Без срока напоминать не буду. Час можно указать, когда выбран день."]}
            repeat={{ every: "none", disabled: true }}
          />
        </Phone>
      </div>
    </div>
  );
}
