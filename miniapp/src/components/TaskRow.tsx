import { formatDue, formatRecorded } from "../lib/format.ts";
import { type GroupKey, type Task, isOverdue } from "../lib/tasks.ts";
import { KindChip, PriorityChip, PromiseChip, ReviewChip } from "./Chip.tsx";

/**
 * Строка списка: полоска приоритета слева, суть, срок словами и метки.
 * Вся строка — кнопка: касание открывает карточку.
 */
export function TaskRow({
  task,
  group,
  now,
  onOpen,
}: {
  task: Task;
  group: GroupKey;
  now: Date;
  onOpen: (task: Task) => void;
}) {
  const overdue = isOverdue(task, now);
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
    <button type="button" className="row" onClick={() => onOpen(task)}>
      <span className={markClass} aria-hidden="true" />
      <span className="row__body">
        <span className="row__title">{task.title}</span>
        <span className="row__meta">
          <KindChip task={task} />
          <span className={whenClass}>
            {task.dueAt
              ? formatDue(task.dueAt, task.duePrecision, now)
              : formatRecorded(task.createdAt)}
          </span>
          <PriorityChip task={task} compact />
          <PromiseChip task={task} compact />
          <ReviewChip task={task} />
        </span>
      </span>
      <span className="row__chev" aria-hidden="true">
        ›
      </span>
    </button>
  );
}
