import { countActive, countOverdue } from "../lib/format.ts";
import { TASKS_SHOWN, type Task, groupTasks } from "../lib/tasks.ts";
import { Empty } from "./Empty.tsx";
import { ErrorNote } from "./ErrorNote.tsx";
import { Header } from "./Header.tsx";
import { TaskGroup } from "./TaskGroup.tsx";

/** Состояния списка — из `design.md` §2: загрузка, ошибка, пусто, штатно. */
export type ListState =
  | { kind: "loading" }
  | { kind: "ready"; tasks: Task[]; more: boolean }
  | { kind: "failed"; message: string };

function subtitle(state: ListState, overdue: number): string | undefined {
  if (state.kind !== "ready" || state.tasks.length === 0) {
    return undefined;
  }
  const active = countActive(state.tasks.length);
  return overdue > 0 ? `${active} · ${countOverdue(overdue)}` : active;
}

/** Три серые плашки на месте строк — загрузка без спиннера. */
function Skeleton() {
  return (
    <div className="card skeleton" aria-busy="true" aria-label="Загружаем задачи">
      <div className="skeleton__row" />
      <div className="skeleton__row" />
      <div className="skeleton__row" />
    </div>
  );
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
    <main className="screen">
      <Header title="Задачи" subtitle={subtitle(state, overdue)} />
      {state.kind === "loading" ? <Skeleton /> : null}
      {state.kind === "failed" ? <ErrorNote message={state.message} onRetry={onReload} /> : null}
      {state.kind === "ready" && state.tasks.length === 0 ? <Empty onClose={onClose} /> : null}
      {groups.map((group) => (
        <TaskGroup key={group.key} group={group} now={now} onOpen={onOpen} />
      ))}
      {state.kind === "ready" && state.more ? (
        <p className="note">Показаны первые {TASKS_SHOWN}.</p>
      ) : null}
    </main>
  );
}
