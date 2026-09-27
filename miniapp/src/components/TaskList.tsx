import { countActive, countOverdue } from "../lib/format.ts";
import { TASKS_SHOWN, type Task, groupTasks } from "../lib/tasks.ts";
import { Empty } from "./Empty.tsx";
import { ErrorNote } from "./ErrorNote.tsx";
import { Header } from "./Header.tsx";
import { Skeleton } from "./Skeleton.tsx";
import { TaskGroup } from "./TaskGroup.tsx";

/** Состояния списка — из `design.md` §2: загрузка, ошибка, пусто, штатно. */
export type ListState =
  | { kind: "loading" }
  | { kind: "ready"; tasks: Task[]; more: boolean }
  | { kind: "failed"; message: string };

const HINTS = ["«В пятницу отправить расчёт Кузнецову»", "«Я обещал Сергею перезвонить во вторник»"];

function subtitle(state: ListState, overdue: number): string | undefined {
  if (state.kind !== "ready" || state.tasks.length === 0) {
    return undefined;
  }
  const active = countActive(state.tasks.length);
  return overdue > 0 ? `${active} · ${countOverdue(overdue)}` : active;
}

export function TaskList({
  state,
  now,
  onOpen,
  onReload,
  onClose,
}: {
  state: ListState;
  now: Date;
  onOpen: (task: Task) => void;
  onReload: () => void;
  onClose: () => void;
}) {
  const groups = state.kind === "ready" ? groupTasks(state.tasks, now) : [];
  const overdue = groups.find((g) => g.key === "overdue")?.tasks.length ?? 0;

  return (
    <main className="screen screen--tabs">
      <Header title="Задачи" subtitle={subtitle(state, overdue)} />
      {state.kind === "loading" ? <Skeleton label="Загружаем задачи" /> : null}
      {state.kind === "failed" ? <ErrorNote message={state.message} onRetry={onReload} /> : null}
      {state.kind === "ready" && state.tasks.length === 0 ? (
        <Empty
          title="Задач пока нет"
          text="Напишите боту — и она появится здесь. Срок и напоминание он разберёт сам."
          hints={HINTS}
          onClose={onClose}
        />
      ) : null}
      {groups.map((group) => (
        <TaskGroup key={group.key} group={group} now={now} onOpen={onOpen} />
      ))}
      {state.kind === "ready" && state.more ? (
        <p className="note">Показаны первые {TASKS_SHOWN}.</p>
      ) : null}
    </main>
  );
}
