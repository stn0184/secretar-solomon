import { type Sphere, sphereOf } from "../lib/spheres.ts";
import type { Task } from "../lib/tasks.ts";
import { TaskRow } from "./TaskRow.tsx";

/**
 * Группа списка «Дел»: заголовок капителью и число дел справа, ниже —
 * карточка со строками (артборд 04). Одна и та же для групп срока и сфер.
 */
export function TaskGroup({
  title,
  tasks,
  spheres,
  now,
  doneId,
  onOpen,
}: {
  title: string;
  tasks: Task[];
  spheres: readonly Sphere[];
  now: Date;
  /** Дело, которое только что отметили «Сделано» и база перевела дальше. */
  doneId: string | null;
  onOpen: (task: Task) => void;
}) {
  return (
    <section className="sec">
      <h2 className="sec__h">
        <span>{title}</span>
        <em>{tasks.length}</em>
      </h2>
      <div className="card">
        {tasks.map((task) => (
          <TaskRow
            key={task.id}
            task={task}
            sphere={sphereOf(task.sphereId, spheres)}
            now={now}
            justDone={task.id === doneId}
            onOpen={onOpen}
          />
        ))}
      </div>
    </section>
  );
}
