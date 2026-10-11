import { formatDay, formatTime } from "../lib/format.ts";
import {
  type TodayData,
  addDays,
  aheadMinutes,
  dayThread,
  isTodayEmpty,
  noHourTasks,
  todaySub,
  totalMinutes,
} from "../lib/hours.ts";
import type { Sphere } from "../lib/spheres.ts";
import type { Task } from "../lib/tasks.ts";
import { DayThread } from "./DayThread.tsx";
import { Empty } from "./Empty.tsx";
import { ErrorNote } from "./ErrorNote.tsx";
import { Header } from "./Header.tsx";
import { HoursFigure } from "./HoursFigure.tsx";
import { Skeleton } from "./Skeleton.tsx";
import { SphereSplit } from "./SphereSplit.tsx";
import { TaskGroup } from "./TaskGroup.tsx";

/** Что знает «Сегодня»: часы и отрезки дня и дела дня читаются вместе (`App`). */
export type TodayState =
  | { kind: "loading" }
  | { kind: "ready"; today: TodayData }
  | { kind: "failed"; message: string };

/**
 * Вкладка «Сегодня» (`techspec/32-redesign.md` §32.3, артборд 01): часы дня
 * крупной цифрой «примерно» и сколько встреч ещё впереди, доли сфер полосой,
 * дела без часа и нить дня. Часы считает база (`sphere_hours`) — с минутами
 * переписки, но переписка отдельно не называется (036); нить — из дел дня.
 * Часов ещё нет, а дела есть — без цифры и полосы, «Часов пока нет»: нулей не
 * показываем. Нет ничего — одобренное пустое (`design.md` §2, артборд 07).
 *
 * «Без часа» — та же строка дела, что в «Делах» (`TaskRow`, прототип 036):
 * день не повторяется, справа — часть дня или ничего. Касание открывает
 * карточку дела; нить не нажимается.
 */
export function TodayTab({
  state,
  spheres,
  now,
  onOpen,
  onReload,
  onClose,
}: {
  state: TodayState;
  spheres: readonly Sphere[];
  now: Date;
  onOpen: (task: Task) => void;
  onReload: () => void;
  onClose: () => void;
}) {
  const eyebrow = `Сегодня · ${formatDay(now)}`;

  if (state.kind !== "ready") {
    return (
      <main className="screen screen--tabs">
        <Header eyebrow={eyebrow} />
        {state.kind === "loading" ? (
          <Skeleton rows={4} label="Загружаем день" />
        ) : (
          <ErrorNote message={state.message} onRetry={onReload} />
        )}
      </main>
    );
  }

  const { today } = state;
  if (isTodayEmpty(today)) {
    return (
      <main className="screen screen--tabs">
        <Header eyebrow={eyebrow} />
        <Empty
          title="Сегодня свободно"
          text="Ни встреч, ни дел на сегодня. Часы появятся сами — из встреч с длительностью и из переписки в личных чатах."
          examples={["Созвон с Игорем завтра с 14 до 16", "Тренировка в субботу в 9"]}
          hint="Напишите или наговорите боту — дело встанет в ленту."
          action="Написать Соломону"
          onClose={onClose}
        />
      </main>
    );
  }

  const total = totalMinutes(today.rows);
  const sub = todaySub(total, aheadMinutes(today.spans, now, addDays(today.day, 1)));
  const inbox = noHourTasks(today.tasks);
  const thread = dayThread(today.tasks, now);

  return (
    <main className="screen screen--tabs">
      <header className="hd">
        <span className="eyebrow">{eyebrow}</span>
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
      {inbox.length > 0 ? (
        <TaskGroup
          title="Без часа"
          tasks={inbox}
          spheres={spheres}
          now={now}
          doneId={null}
          onOpen={onOpen}
          showSphere
          dayKnown
        />
      ) : null}
      {thread.length > 0 ? (
        <section className="sec">
          <h2 className="sec__h">
            <span>День</span>
            <em>сейчас {formatTime(now)}</em>
          </h2>
          <DayThread entries={thread} spheres={spheres} />
        </section>
      ) : null}
    </main>
  );
}
