import { countOpen, countOverdue } from "../lib/format.ts";
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

/** Надзаголовок «Дел»: «11 открытых · одно просрочено»; пусто — «Открытых нет». */
function eyebrow(state: ListState, overdue: number): string | undefined {
  if (state.kind !== "ready") {
    return undefined;
  }
  if (state.tasks.length === 0) {
    return "Открытых нет";
  }
  const open = countOpen(state.tasks.length);
  return overdue > 0 ? `${open} · ${countOverdue(overdue)}` : open;
}

export function TaskList({
  state,
  now,
  doneId,
  onOpen,
  onReload,
  onClose,
}: {
  state: ListState;
  now: Date;
  /** Повторяющаяся задача, которую только что отметили, — до следующего чтения. */
  doneId: string | null;
  onOpen: (task: Task) => void;
  onReload: () => void;
  onClose: () => void;
}) {
  const groups = state.kind === "ready" ? groupTasks(state.tasks, now) : [];
  const overdue = groups.find((g) => g.key === "overdue")?.tasks.length ?? 0;

  return (
    <main className="screen screen--tabs">
      <Header title="Дела" eyebrow={eyebrow(state, overdue)} />
      {state.kind === "loading" ? <Skeleton label="Загружаем задачи" /> : null}
      {state.kind === "failed" ? <ErrorNote message={state.message} onRetry={onReload} /> : null}
      {state.kind === "ready" && state.tasks.length === 0 ? (
        <Empty
          title="Дел пока нет"
          text="Напишите Соломону — и дело появится здесь. Сферу и срок он поставит сам, поправить можно словом."
          examples={["Купить лампочку", "Прислать расчёт Олегу до пятницы"]}
          hint="Голосовые и пересланные сообщения тоже подойдут."
          onClose={onClose}
        />
      ) : null}
      {groups.map((group) => (
        <TaskGroup key={group.key} group={group} now={now} doneId={doneId} onOpen={onOpen} />
      ))}
      {state.kind === "ready" && state.more ? (
        <p className="note">Показаны первые {TASKS_SHOWN}.</p>
      ) : null}
    </main>
  );
}
