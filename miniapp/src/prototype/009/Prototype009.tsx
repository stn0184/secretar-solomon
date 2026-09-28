/**
 * Прототип этапа 009 — правка задачи из приложения. Dev-only витрина:
 * маршрут `/prototype/009` регистрируется в `main.tsx` под
 * `import.meta.env.DEV`, в сборку не попадает.
 *
 * Экраны собраны из настоящих компонентов карточки (`Header`, `Field`,
 * метки, `SourceMessage`, `Reminders`, `Actions`) на моках из `mock.ts`.
 * Новое — кнопка «Изменить» в карточке, вопрос бота рядом с
 * «Перепроверьте» и форма правки: их разметка и стили (`prototype.css`)
 * — предложение, при реализации переезжают в `components/` и `styles.css`.
 *
 * Логики нет: кнопки не нажимаются, форма не отправляется.
 */

import type { ReactNode } from "react";

import { Actions } from "../../components/Actions.tsx";
import { PriorityChip, PromiseChip, ReviewChip } from "../../components/Chip.tsx";
import { Field } from "../../components/Field.tsx";
import { Header } from "../../components/Header.tsx";
import { Reminders } from "../../components/Reminders.tsx";
import { SourceMessage } from "../../components/SourceMessage.tsx";
import { formatDay, formatDue, formatMoment, sameDay } from "../../lib/format.ts";
import type { Reminder, SourceMessage as Source, Task } from "../../lib/tasks.ts";
import {
  BARE_SOURCE,
  NOW,
  QUESTION,
  REMINDERS_AFTER,
  REMINDERS_BEFORE,
  SOURCE,
  TASK_AFTER,
  TASK_BARE,
  TASK_BEFORE,
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

type Question = { text: string; askedAt: Date };

const KIND_WORD: Record<Task["kind"], string> = { task: "Задача", idea: "Идея", wish: "Желание" };

/* ── Рама: телефон с шапкой Telegram. Не продукт — бумага под ним. ── */

function Phone({
  n,
  title,
  note,
  children,
}: {
  n: number;
  title: string;
  note: string;
  children: ReactNode;
}) {
  return (
    <figure className="p9-board">
      <figcaption className="p9-cap">
        <b>
          {n} · {title}
        </b>
        <span>{note}</span>
      </figcaption>
      <div className="p9-phone">
        <div className="p9-tg" aria-hidden="true">
          <span className="p9-tg__side">‹ Назад</span>
          <span className="p9-tg__mid">
            <b>Соломон</b>
            <small>мини-приложение</small>
          </span>
          <span className="p9-tg__side p9-tg__side--end">⋯</span>
        </div>
        <div className="p9-viewport">{children}</div>
      </div>
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

/* ── Карточка задачи: как TaskCard, плюс «Изменить» и вопрос бота ── */

function Card({
  task,
  source,
  reminders,
  question,
}: {
  task: Task;
  source: Source;
  reminders: Reminder[];
  question?: Question;
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
          {task.kind !== "task" ? <Field label="Вид">{KIND_WORD[task.kind]}</Field> : null}
          {task.priority === "high" ? (
            <Field label="Приоритет">
              <PriorityChip task={task} compact={false} />
            </Field>
          ) : null}
          {task.priority === "low" ? <Field label="Приоритет">низкий</Field> : null}
          {task.promise ? (
            <Field label="Обещание">
              <PromiseChip task={task} compact={false} />
            </Field>
          ) : null}
          {task.people.length > 0 ? <Field label="Люди">{task.people.join(", ")}</Field> : null}
          {task.needsReview ? (
            <Field label="Разбор">
              <ReviewChip task={task} />
              {question ? (
                <span className="kv__q">
                  Бот спросил {formatMoment(question.askedAt, NOW)}: «{question.text}»
                </span>
              ) : null}
            </Field>
          ) : null}
        </div>
        {/* новое: вход в форму — строкой под полями, которые она правит */}
        <button type="button" className="kv__edit">
          <PencilIcon />
          Изменить
        </button>
      </div>

      <SourceMessage message={source} now={NOW} />
      <Reminders reminders={reminders} hasDue={task.dueAt !== null} now={NOW} />

      {/* подписи «Изменить текст или срок пока можно только через бота» больше нет */}

      <Actions
        busy={null}
        error={null}
        primary={{ kind: "done", label: "Сделано", busyLabel: "Закрываем…", onClick: noop }}
        onDelete={noop}
      />
    </main>
  );
}

/* ── Форма правки: на том же экране, вместо карточки ── */

/** Выбор из вариантов — обычные radio, подписью служит сама кнопка. */
function Segments({
  name,
  options,
  value,
}: {
  name: string;
  options: [string, string][];
  value: string;
}) {
  return (
    <div className="seg" role="radiogroup">
      {options.map(([key, label]) => (
        <label className="seg__opt" key={key}>
          <input type="radio" name={name} value={key} defaultChecked={key === value} />
          <span>{label}</span>
        </label>
      ))}
    </div>
  );
}

/** Значения полей ввода — `YYYY-MM-DD` и `HH:MM` в поясе устройства (моки фиксированы). */
function dateValue(at: Date | null): string {
  if (!at) {
    return "";
  }
  const mm = String(at.getMonth() + 1).padStart(2, "0");
  const dd = String(at.getDate()).padStart(2, "0");
  return `${at.getFullYear()}-${mm}-${dd}`;
}

function timeValue(task: Task): string {
  if (!task.dueAt || task.duePrecision !== "time") {
    return "";
  }
  const hh = String(task.dueAt.getHours()).padStart(2, "0");
  const mi = String(task.dueAt.getMinutes()).padStart(2, "0");
  return `${hh}:${mi}`;
}

function Form({ task, source, question }: { task: Task; source: Source; question?: Question }) {
  const noDue = task.dueAt === null;
  const hint = noDue
    ? "Без срока напоминать не буду. Час можно указать, когда выбран день."
    : task.duePrecision === "time"
      ? "Напомню за час и в срок."
      : "Без часа — напомню в 09:00 и в 18:00 этого дня.";
  const id = task.id;

  return (
    <main className="screen">
      <Header title="Правка задачи" onBack={noop} backLabel="Задача" />

      {task.needsReview ? (
        <div className="review">
          <div className="review__top">
            <ReviewChip task={task} />
            {question ? <span>{formatMoment(question.askedAt, NOW)}</span> : null}
          </div>
          {question ? <p className="review__q">Бот спросил: «{question.text}»</p> : null}
          <p className="review__note">
            «Сохранить» снимет пометку и вопрос — даже если ничего не менять.
          </p>
        </div>
      ) : null}

      <div className="card form">
        <label className="form__row">
          <span className="form__k">Суть</span>
          <textarea className="input input--area" rows={2} defaultValue={task.title} />
        </label>

        <div className="form__row">
          <span className="form__k">Срок</span>
          <div className="form__due">
            <input
              type="date"
              className="input"
              aria-label="День"
              defaultValue={dateValue(task.dueAt)}
              disabled={noDue}
            />
            <input
              type="time"
              className="input"
              aria-label="Час"
              defaultValue={timeValue(task)}
              disabled={noDue}
            />
          </div>
          <label className="check">
            <input type="checkbox" defaultChecked={noDue} />
            <span>Без срока</span>
          </label>
          <p className="form__hint">{hint}</p>
        </div>

        <div className="form__row">
          <span className="form__k">Вид</span>
          <Segments
            name={`kind-${id}`}
            value={task.kind}
            options={[
              ["task", "Задача"],
              ["idea", "Идея"],
              ["wish", "Желание"],
            ]}
          />
        </div>

        <div className="form__row">
          <span className="form__k">Срочность</span>
          <Segments
            name={`priority-${id}`}
            value={task.priority}
            options={[
              ["low", "Низкая"],
              ["normal", "Обычная"],
              ["high", "Высокая"],
            ]}
          />
        </div>

        <div className="form__row">
          <span className="form__k">Обещание</span>
          <Segments
            name={`promise-${id}`}
            value={task.promise ?? "none"}
            options={[
              ["none", "Нет"],
              ["mine", "Я обещал"],
              ["to_me", "Мне обещали"],
            ]}
          />
        </div>

        <label className="form__row">
          <span className="form__k">Люди</span>
          <input
            type="text"
            className="input"
            defaultValue={task.people.join(", ")}
            placeholder="Например: Кузнецов, Анна"
          />
          <span className="form__hint">Через запятую.</span>
        </label>
      </div>

      <SourceMessage message={source} now={NOW} />

      <div className="actions">
        <div className="actions__row">
          <button type="button" className="btn">
            Сохранить
          </button>
          <button type="button" className="btn btn--ghost">
            Отмена
          </button>
        </div>
      </div>
    </main>
  );
}

/* ── Полотно: экраны слева направо — путь человека ── */

export function Prototype009() {
  return (
    <div className="p9">
      <header className="p9-head">
        <h1>Прототип 009 — правка задачи из приложения</h1>
        <p>
          Слева направо — путь владельца: карточка → форма → карточка после «Сохранить»; ниже —
          пустое: задача, у которой, кроме сути, ничего нет. Кнопки не работают, все данные
          выдуманы, «сейчас» — понедельник, 28 сентября, 10:40.
        </p>
        <p className="p9-links">
          Тема: <a href="?">светлая</a> · <a href="?scheme=dark">тёмная</a>
        </p>
      </header>

      <div className="p9-strip">
        <Phone
          n={1}
          title="Карточка: пометка и вопрос бота"
          note="Вопрос бота — рядом с «Перепроверьте». Новая строка «Изменить» под полями; подписи «только через бота» больше нет."
        >
          <Card task={TASK_BEFORE} source={SOURCE} reminders={REMINDERS_BEFORE} question={QUESTION} />
        </Phone>

        <Phone
          n={2}
          title="Форма правки"
          note="Открыта по «Изменить» на том же экране, с текущими значениями. Сверху — что смутило бота, внизу — исходное сообщение для сверки."
        >
          <Form task={TASK_BEFORE} source={SOURCE} question={QUESTION} />
        </Phone>

        <Phone
          n={3}
          title="Карточка после «Сохранить»"
          note="То, что вернула база: срок — понедельник, 12:00, приоритет высокий, пометка и вопрос сняты, напоминания пересчитаны."
        >
          <Card task={TASK_AFTER} source={SOURCE} reminders={REMINDERS_AFTER} />
        </Phone>
      </div>

      <h2 className="p9-sub">Пустое: у задачи только суть</h2>
      <div className="p9-strip">
        <Phone
          n={4}
          title="Карточка без срока и деталей"
          note="Полей почти нет — «Изменить» на том же месте, под полями."
        >
          <Card task={TASK_BARE} source={BARE_SOURCE} reminders={[]} />
        </Phone>

        <Phone
          n={5}
          title="Форма: срока нет"
          note="«Без срока» отмечено, день и час заперты; «Люди» — пустое поле с примером."
        >
          <Form task={TASK_BARE} source={BARE_SOURCE} />
        </Phone>
      </div>
    </div>
  );
}
