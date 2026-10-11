/**
 * Прототип этапа 036 — понятный список дел. Dev-only витрина: маршрут
 * `/prototype/036` регистрируется в `main.tsx` под `import.meta.env.DEV`,
 * в сборку не попадает. Тема — `?skin=quiet` («Тихая роскошь», по
 * умолчанию) или `?skin=bank` («Банк-люкс»).
 *
 * Экраны собраны из настоящих компонентов и стилей (`Header`, `Choice`,
 * `Tabs`, `SphereMark`, `Chip`, `HoursFigure`, `SphereSplit`, `DayThread`,
 * `Backdrop`, `TaskList` для пустого) на моках из `mock.ts`. Новое — строка
 * дела (срок справа, полоса приоритета, короткие обещания, без «Без сферы»),
 * заголовок группы с числом и группа «Завтра»: их разметка и стили
 * (`prototype.css`, раздел 1) — предложение, при реализации переезжают в
 * `components/`, `lib/tasks.ts` и `styles.css`.
 *
 * Логики нет: кнопки не нажимаются, переключатель не переключает.
 */

import type { CSSProperties, ReactNode } from "react";

import { Backdrop } from "../../components/Backdrop.tsx";
import { Chip, KindChip, ReviewChip } from "../../components/Chip.tsx";
import { Choice } from "../../components/Choice.tsx";
import { DayThread } from "../../components/DayThread.tsx";
import { Header } from "../../components/Header.tsx";
import { HoursFigure } from "../../components/HoursFigure.tsx";
import { SphereMark } from "../../components/SphereMark.tsx";
import { SphereSplit } from "../../components/SphereSplit.tsx";
import { type Tab, Tabs } from "../../components/Tabs.tsx";
import { TaskList } from "../../components/TaskList.tsx";
import { PART_WORDS, countOpen, countOverdue, formatDay, formatTime, isPart, sameDay } from "../../lib/format.ts";
import {
  type TodayData,
  addDays,
  aheadMinutes,
  dayThread,
  noHourTasks,
  todaySub,
  totalMinutes,
} from "../../lib/hours.ts";
import { repeatShort } from "../../lib/repeat.ts";
import { type Sphere, sphereColor, sphereOf } from "../../lib/spheres.ts";
import { type Task, groupBySphere, isOverdue } from "../../lib/tasks.ts";
import { type Skin, applySkin } from "../../lib/theme.ts";
import { NOW, SPHERES, TASKS, TASKS_NO_SPHERES, TODAY, TODAY_NO_SPHERES } from "./mock.ts";
import "./prototype.css";

/* ── Тема прототипа — из адреса; приложение берёт её из Telegram и базы ── */

const SKIN: Skin = new URLSearchParams(window.location.search).get("skin") === "bank" ? "bank" : "quiet";
applySkin(SKIN);

const noop = () => {};

/* ══ Предложение для lib/tasks.ts: группа «Завтра» и срок для строки ══ */

type DueGroupKey = "overdue" | "today" | "tomorrow" | "week" | "later" | "none" | "ideas";

const DUE_ORDER: readonly DueGroupKey[] = ["overdue", "today", "tomorrow", "week", "later", "none", "ideas"];

const DUE_TITLES: Record<DueGroupKey, string> = {
  overdue: "Просрочено",
  today: "Сегодня",
  tomorrow: "Завтра",
  week: "На неделе",
  later: "Позже",
  none: "Без срока",
  ideas: "Идеи и желания",
};

const DAY_MS = 24 * 60 * 60 * 1000;
const WEEKDAYS_SHORT = ["вс", "пн", "вт", "ср", "чт", "пт", "сб"] as const;
const MONTHS_SHORT = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"] as const;

function daysAhead(dueAt: Date, now: Date): number {
  const a = new Date(dueAt.getFullYear(), dueAt.getMonth(), dueAt.getDate()).getTime();
  const b = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  return Math.round((a - b) / DAY_MS);
}

/** Группа срока: как сейчас, плюс «Завтра» между «Сегодня» и «На неделе». */
function dueGroupOf(task: Task, now: Date): DueGroupKey {
  if (task.kind !== "task") {
    return "ideas";
  }
  if (!task.dueAt) {
    return "none";
  }
  if (isOverdue(task, now)) {
    return "overdue";
  }
  const days = daysAhead(task.dueAt, now);
  if (days === 0) {
    return "today";
  }
  if (days === 1) {
    return "tomorrow";
  }
  return days <= 7 ? "week" : "later";
}

function groupByDue(tasks: readonly Task[], now: Date): { key: string; title: string; tasks: Task[] }[] {
  return DUE_ORDER.flatMap((key) => {
    const inGroup = tasks.filter((t) => dueGroupOf(t, now) === key);
    return inGroup.length > 0 ? [{ key, title: DUE_TITLES[key], tasks: inGroup }] : [];
  });
}

/**
 * Срок справа в строке: «сегодня 15:00», «завтра», «пт», «26 окт»; у части
 * дня — «сегодня вечером». У встречи — только начало: конец — в карточке.
 * `dayKnown` — экран и так про сегодня («Без часа»): день не повторяется,
 * остаётся часть дня («утром») или ничего.
 */
function dueShort(task: Task, now: Date, dayKnown = false): string | null {
  if (!task.dueAt) {
    return null;
  }
  const days = daysAhead(task.dueAt, now);
  let day: string;
  if (days === 0) {
    day = "сегодня";
  } else if (days === 1) {
    day = "завтра";
  } else if (days === -1) {
    day = "вчера";
  } else if (Math.abs(days) <= 6) {
    day = WEEKDAYS_SHORT[task.dueAt.getDay()] ?? "";
  } else {
    day = `${task.dueAt.getDate()} ${MONTHS_SHORT[task.dueAt.getMonth()]}`;
  }
  const tail =
    task.duePrecision === "time"
      ? formatTime(task.dueAt)
      : isPart(task.duePrecision)
        ? PART_WORDS[task.duePrecision]
        : null;
  if (dayKnown && sameDay(task.dueAt, now)) {
    return tail;
  }
  return tail ? `${day} ${tail}` : day;
}

/* ══ Предложение для components/: строка дела и заголовок группы ══ */

/** Обещание коротко: «🤝 Игорь» — вы обещали, «⏳ Олег» — обещали вам. */
function PromiseShort({ task }: { task: Task }) {
  if (!task.promise) {
    return null;
  }
  const person = task.people[0];
  const mine = task.promise === "mine";
  const label = mine ? "Вы обещали" : "Обещали вам";
  return (
    <Chip tone={mine ? "mine" : "theirs"}>
      <span aria-hidden="true">{mine ? "🤝" : "⏳"}</span>
      <span aria-label={person ? `${label}: ${person}` : label}>{person ?? label}</span>
    </Chip>
  );
}

/**
 * Строка дела. Колонка отметки сферы есть, только когда у владельца есть
 * сферы (`marks`); у дела без сферы она пустая — суть не прыгает. Сфера
 * словами — только в виде «По сроку» (`showSphere`): в «По сферам» её
 * называет заголовок группы.
 */
function Row({
  task,
  spheres,
  marks,
  showSphere,
  dayKnown = false,
}: {
  task: Task;
  spheres: readonly Sphere[];
  marks: boolean;
  showSphere: boolean;
  dayKnown?: boolean;
}) {
  const sphere = sphereOf(task.sphereId, spheres);
  const due = dueShort(task, NOW, dayKnown);
  const late = isOverdue(task, NOW);
  const today = !late && task.kind === "task" && task.dueAt !== null && sameDay(task.dueAt, NOW);
  const high = task.priority === "high";
  const meta =
    (showSphere && sphere !== null) ||
    task.repeat !== null ||
    task.kind !== "task" ||
    task.promise !== null ||
    task.needsReview;

  const cls = ["trow", marks ? "" : "trow--bare", high ? "trow--high" : ""].filter(Boolean).join(" ");
  const dueCls = late ? "trow__due trow__due--late" : today ? "trow__due trow__due--today" : "trow__due";

  return (
    <button type="button" className={cls} style={{ "--c": sphereColor(sphere) } as CSSProperties}>
      {marks ? sphere ? <SphereMark sphere={sphere} /> : <span className="trow__nomark" aria-hidden="true" /> : null}
      <span className="trow__b">
        <span className="trow__t">
          {high ? <span className="sr">Высокий приоритет. </span> : null}
          {task.title}
        </span>
        {meta ? (
          <span className="trow__m">
            {showSphere && sphere ? <span>{sphere.name}</span> : null}
            {task.repeat ? (
              <span className="task__rep">
                <i aria-hidden="true">↻</i>
                {repeatShort(task.repeat)}
              </span>
            ) : null}
            <KindChip task={task} />
            <PromiseShort task={task} />
            <ReviewChip task={task} />
          </span>
        ) : null}
      </span>
      <span className={dueCls}>
        {late && due ? <span className="sr">Просрочено: </span> : null}
        {due ?? ""}
      </span>
      <span className="chev" aria-hidden="true">
        ›
      </span>
    </button>
  );
}

/** Заголовок группы: «Просрочено · 2»; у сферы — точка её цвета. */
function GroupHead({ title, count, tone, sphere }: { title: string; count: number; tone?: "late"; sphere?: Sphere | null }) {
  return (
    <h2
      className={tone === "late" ? "grp grp--late" : "grp"}
      style={sphere !== undefined ? ({ "--c": sphereColor(sphere) } as CSSProperties) : undefined}
    >
      {sphere ? <i className="grp__dot" aria-hidden="true" /> : null}
      <span>{title}</span>
      <span className="grp__n">
        <span aria-hidden="true">·</span> {count}
      </span>
    </h2>
  );
}

/* ══ Экраны ══ */

const VIEWS = [
  ["due", "По сроку"],
  ["sphere", "По сферам"],
] as const;

/** «Дела»: как `TaskList`, но со строкой и заголовками этапа 036. */
function TasksScreen({
  tasks,
  spheres,
  view,
}: {
  tasks: readonly Task[];
  spheres: readonly Sphere[];
  view: "due" | "sphere";
}) {
  const byDue = groupByDue(tasks, NOW);
  const overdue = byDue.find((g) => g.key === "overdue")?.tasks.length ?? 0;
  const eyebrow = overdue > 0 ? `${countOpen(tasks.length)} · ${countOverdue(overdue)}` : countOpen(tasks.length);
  const marks = spheres.length > 0;

  return (
    <main className="screen screen--tabs">
      <Header title="Дела" eyebrow={eyebrow} />
      {/* Пока сфер нет совсем — переключателя нет: группировать по сферам нечего. */}
      {marks ? <Choice label="Как сгруппировать" options={VIEWS} value={view} variant="view" onChange={noop} /> : null}
      {view === "due"
        ? byDue.map((group) => (
            <section className="sec" key={group.key}>
              <GroupHead title={group.title} count={group.tasks.length} tone={group.key === "overdue" ? "late" : undefined} />
              <div className="card">
                {group.tasks.map((task) => (
                  <Row key={task.id} task={task} spheres={spheres} marks={marks} showSphere />
                ))}
              </div>
            </section>
          ))
        : groupBySphere([...tasks], spheres, NOW).map((group) => (
            <section className="sec" key={group.key}>
              <GroupHead title={group.title} count={group.tasks.length} sphere={group.sphere} />
              <div className="card">
                {group.tasks.map((task) => (
                  <Row key={task.id} task={task} spheres={spheres} marks={marks} showSphere={false} />
                ))}
              </div>
            </section>
          ))}
    </main>
  );
}

/** «Сегодня»: как `TodayTab`, но «Без часа» — той же строкой, что в «Делах». */
function TodayScreen({ today, spheres }: { today: TodayData; spheres: readonly Sphere[] }) {
  const total = totalMinutes(today.rows);
  const sub = todaySub(total, aheadMinutes(today.spans, NOW, addDays(today.day, 1)));
  const inbox = noHourTasks(today.tasks);
  const thread = dayThread(today.tasks, NOW);
  const marks = spheres.length > 0;

  return (
    <main className="screen screen--tabs">
      <header className="hd">
        <span className="eyebrow">Сегодня · {formatDay(NOW)}</span>
        {total > 0 ? <HoursFigure big minutes={total} wait={100} /> : null}
        <span className="big-sub">
          {sub.text}
          {sub.ahead ? (
            <>
              {" · впереди ещё "}
              <b>{sub.ahead}</b>
            </>
          ) : null}
        </span>
      </header>
      {total > 0 ? <SphereSplit rows={today.rows} spheres={spheres} /> : null}
      <section className="sec">
        <GroupHead title="Без часа" count={inbox.length} />
        <div className="card">
          {inbox.map((task) => (
            <Row key={task.id} task={task} spheres={spheres} marks={marks} showSphere dayKnown />
          ))}
        </div>
      </section>
      <section className="sec">
        <h2 className="sec__h">
          <span>День</span>
          <em>сейчас {formatTime(NOW)}</em>
        </h2>
        <DayThread entries={thread} spheres={spheres} />
      </section>
    </main>
  );
}

/* ══ Рама: телефон с шапкой Telegram. Не продукт — бумага под ним ══ */

function Phone({ n, title, note, tab, children }: { n: string; title: string; note: string; tab: Tab; children: ReactNode }) {
  return (
    <figure className="p36-board">
      <figcaption className="p36-cap">
        <b>
          {n} · {title}
        </b>
        <span>{note}</span>
      </figcaption>
      <div className="p36-phone">
        <div className="p36-tg" aria-hidden="true">
          <span className="p36-tg__side">✕ Закрыть</span>
          <span className="p36-tg__mid">
            <b>Соломон</b>
            <small>мини-приложение</small>
          </span>
          <span className="p36-tg__side p36-tg__side--end">⋯</span>
        </div>
        <div className="p36-vp">
          <Backdrop />
          {children}
          <Tabs active={tab} onChange={noop} />
        </div>
      </div>
    </figure>
  );
}

export function Prototype036() {
  const other = SKIN === "bank" ? "quiet" : "bank";
  return (
    <div className="p36">
      <header className="p36-head">
        <h1>Прототип 036 — понятный список дел · {SKIN === "bank" ? "«Банк-люкс»" : "«Тихая роскошь»"}</h1>
        <p>
          Срок справа, группы с числом и «Завтра», тёплая полоса у важного, короткие обещания, без «Без сферы».
          Все дела, имена и часы выдуманы — это пример, а не ваши данные. «Сейчас» — вторник, 13 октября, 10:40.
          Внутри экрана прокрутка — как на телефоне.
        </p>
        <p className="p36-switch">
          Тема: <b>{SKIN === "bank" ? "Банк-люкс" : "Тихая роскошь"}</b> ·{" "}
          <a href={`?skin=${other}`}>открыть «{other === "bank" ? "Банк-люкс" : "Тихая роскошь"}»</a>
        </p>
      </header>

      <h2 className="p36-sub">Сферы есть: VoiceFin, РЕЙВА, Семья</h2>
      <div className="p36-strip">
        <Phone
          n="1"
          title="«Дела» по сроку"
          tab="tasks"
          note="Срок справа: просрочено — красным, сегодня — цветом темы. «Завтра» — своя группа. У важного — тёплая полоса слева. Сфера словами под сутью; у дела без сферы — ничего."
        >
          <TasksScreen tasks={TASKS} spheres={SPHERES} view="due" />
        </Phone>
        <Phone
          n="2"
          title="«Дела» по сферам"
          tab="tasks"
          note="Заголовок — сфера, её цвет и число дел. Сфера словами в строке не повторяется. Дела без сферы — последней группой «Без сферы»."
        >
          <TasksScreen tasks={TASKS} spheres={SPHERES} view="sphere" />
        </Phone>
        <Phone
          n="3"
          title="«Сегодня»: дела без часа"
          tab="today"
          note="«Без часа · 4» — той же строкой, что в «Делах». День не повторяется: справа только часть дня («утром», «вечером»)."
        >
          <TodayScreen today={TODAY} spheres={SPHERES} />
        </Phone>
      </div>

      <h2 className="p36-sub">Сфер нет совсем — так владелец увидит «Дела» сейчас</h2>
      <div className="p36-strip">
        <Phone
          n="4"
          title="«Дела» без сфер"
          tab="tasks"
          note="Переключателя «По сроку / По сферам» нет. Ни полосок, ни «Без сферы»: строка — суть, метки и срок."
        >
          <TasksScreen tasks={TASKS_NO_SPHERES} spheres={[]} view="due" />
        </Phone>
        <Phone
          n="5"
          title="«Сегодня» без сфер"
          tab="today"
          note="«Без часа» — без отметок. Полоса долей и нить дня этим этапом не меняются — их подписи «Без сферы» остались."
        >
          <TodayScreen today={TODAY_NO_SPHERES} spheres={[]} />
        </Phone>
        <Phone
          n="6"
          title="«Дела», пусто"
          tab="tasks"
          note="Не меняется: настоящий экран TaskList без дел. Переключателя нет."
        >
          <TaskList
            state={{ kind: "ready", tasks: [], more: false, spheres: [] }}
            view="due"
            now={NOW}
            doneId={null}
            onView={noop}
            onOpen={noop}
            onReload={noop}
            onClose={noop}
          />
        </Phone>
      </div>
    </div>
  );
}
