import { countOpen, countOverdue } from "../lib/format.ts";
import type { Sphere } from "../lib/spheres.ts";
import { TASKS_SHOWN, type Task, groupBySphere, groupTasks } from "../lib/tasks.ts";
import { Choice } from "./Choice.tsx";
import { Empty } from "./Empty.tsx";
import { ErrorNote } from "./ErrorNote.tsx";
import { Header } from "./Header.tsx";
import { Skeleton } from "./Skeleton.tsx";
import { TaskGroup } from "./TaskGroup.tsx";

/**
 * Состояния «Дел» — `design.md` §2: загрузка, ошибка, пусто, штатно. Дела
 * читаются вместе со сферами: строке нужны название и цвет сферы.
 */
export type ListState =
  | { kind: "loading" }
  | { kind: "ready"; tasks: Task[]; more: boolean; spheres: Sphere[] }
  | { kind: "failed"; message: string };

/** Как сгруппирован список: по сроку (как было) или по сферам (§32.2). */
export type ListView = "due" | "sphere";

const VIEWS = [
  ["due", "По сроку"],
  ["sphere", "По сферам"],
] as const satisfies readonly (readonly [ListView, string])[];

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

/**
 * «Дела» (артборд 04): надзаголовок со счётом, переключатель «По сроку / По
 * сферам» и группы. Переключатель только перегруппировывает тот же список —
 * данных и действий не добавляет; на пустом экране его нет.
 */
export function TaskList({
  state,
  view,
  now,
  doneId,
  onView,
  onOpen,
  onReload,
  onClose,
}: {
  state: ListState;
  view: ListView;
  now: Date;
  /** Повторяющееся дело, которое только что отметили, — до следующего чтения. */
  doneId: string | null;
  onView: (view: ListView) => void;
  onOpen: (task: Task) => void;
  onReload: () => void;
  onClose: () => void;
}) {
  const tasks = state.kind === "ready" ? state.tasks : [];
  const spheres = state.kind === "ready" ? state.spheres : [];
  const byDue = groupTasks(tasks, now);
  const overdue = byDue.find((g) => g.key === "overdue")?.tasks.length ?? 0;
  const groups =
    view === "sphere"
      ? groupBySphere(tasks, spheres, now).map((g) => ({ key: g.key, title: g.title, tasks: g.tasks }))
      : byDue;

  return (
    <main className="screen screen--tabs">
      <Header title="Дела" eyebrow={eyebrow(state, overdue)} />
      {state.kind === "loading" ? <Skeleton label="Загружаем дела" /> : null}
      {state.kind === "failed" ? <ErrorNote message={state.message} onRetry={onReload} /> : null}
      {state.kind === "ready" && tasks.length === 0 ? (
        <Empty
          title="Дел пока нет"
          text="Напишите Соломону — и дело появится здесь. Сферу и срок он поставит сам, поправить можно словом."
          examples={["Купить лампочку", "Прислать расчёт Олегу до пятницы"]}
          hint="Голосовые и пересланные сообщения тоже подойдут."
          onClose={onClose}
        />
      ) : null}
      {tasks.length > 0 ? (
        <Choice label="Как сгруппировать" options={VIEWS} value={view} variant="view" onChange={onView} />
      ) : null}
      {groups.map((group) => (
        <TaskGroup
          key={group.key}
          title={group.title}
          tasks={group.tasks}
          spheres={spheres}
          now={now}
          doneId={doneId}
          onOpen={onOpen}
        />
      ))}
      {state.kind === "ready" && state.more ? (
        <p className="note">Показаны первые {TASKS_SHOWN}.</p>
      ) : null}
    </main>
  );
}
