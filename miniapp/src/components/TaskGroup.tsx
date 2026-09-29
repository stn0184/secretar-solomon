import { formatDay } from "../lib/format.ts";
import type { Task, TaskGroup as Group } from "../lib/tasks.ts";
import { TaskRow } from "./TaskRow.tsx";

/**
 * Группа списка: заголовок капителью и карточка со строками.
 * У «Сегодня» рядом с заголовком — какой сегодня день; «Просрочено» — красным.
 */
export function TaskGroup({
  group,
  now,
  doneId,
  onOpen,
}: {
  group: Group;
  now: Date;
  /** Задача, которую только что отметили «Сделано» и база перевела дальше. */
  doneId: string | null;
  onOpen: (task: Task) => void;
}) {
  return (
    <section className="sec">
      <h2 className={group.key === "overdue" ? "sec__h sec__h--overdue" : "sec__h"}>
        <span>{group.title}</span>
        {group.key === "today" ? <em>{formatDay(now)}</em> : null}
      </h2>
      <div className="card">
        {group.tasks.map((task) => (
          <TaskRow
            key={task.id}
            task={task}
            group={group.key}
            now={now}
            justDone={task.id === doneId}
            onOpen={onOpen}
          />
        ))}
      </div>
    </section>
  );
}
