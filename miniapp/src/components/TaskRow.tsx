import { formatDue, formatRecorded } from "../lib/format.ts";
import { repeatShort } from "../lib/repeat.ts";
import { type GroupKey, type Task, isOverdue } from "../lib/tasks.ts";
import { KindChip, PriorityChip, PromiseChip, ReviewChip } from "./Chip.tsx";

/**
 * Строка списка: полоска приоритета слева, суть, срок словами и метки.
 * Вся строка — кнопка: касание открывает карточку.
 *
 * У повторяющейся задачи за сроком — «↻» и правило коротко, подписью, а
 * не меткой. `justDone` — её только что отметили в карточке: под строкой
 * срок, который вернула база, до следующего чтения списка.
 */
export function TaskRow({
  task,
  group,
  now,
  justDone,
  onOpen,
}: {
  task: Task;
  group: GroupKey;
  now: Date;
  justDone: boolean;
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
          {task.repeat ? (
            <span className="row__rep">
              <i aria-hidden="true">↻</i>
              {repeatShort(task.repeat)}
            </span>
          ) : null}
          <PriorityChip task={task} compact />
          <PromiseChip task={task} compact />
          <ReviewChip task={task} />
        </span>
        {justDone && task.dueAt ? (
          <span className="row__next">
            ✓ Сделано. Следующий раз: {formatDue(task.dueAt, task.duePrecision, now)}
          </span>
        ) : null}
      </span>
      <span className="row__chev" aria-hidden="true">
        ›
      </span>
    </button>
  );
}
