import { formatDue, formatRecorded } from "../lib/format.ts";
import { type GroupKey, type Task, isOverdue } from "../lib/tasks.ts";
import { Chip } from "./Chip.tsx";

/** Как метки называют вид записи — задача метки не получает, она по умолчанию. */
const KIND_LABEL: Record<Task["kind"], string | null> = {
  task: null,
  idea: "Идея",
  wish: "Желание",
};

/** Метки строки — те же слова, что у бота в подтверждении. */
export function taskChips(task: Task, compact: boolean) {
  const chips = [];
  if (task.priority === "high") {
    chips.push(
      <Chip key="high" tone="high">
        {compact ? "Высокий приоритет" : "Высокий"}
      </Chip>,
    );
  }
  if (task.promise === "mine") {
    chips.push(
      <Chip key="mine" tone="mine">
        {compact && task.people[0] ? `Я обещал: ${task.people[0]}` : "Я обещал"}
      </Chip>,
    );
  } else if (task.promise === "to_me") {
    chips.push(
      <Chip key="theirs" tone="theirs">
        {compact && task.people[0] ? `Обещали мне: ${task.people[0]}` : "Обещали мне"}
      </Chip>,
    );
  }
  if (task.needsReview) {
    chips.push(
      <Chip key="review" tone="review">
        Перепроверьте
      </Chip>,
    );
  }
  return chips;
}

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
  const kindLabel = KIND_LABEL[task.kind];

  return (
    <button type="button" className="row" onClick={() => onOpen(task)}>
      <span className={markClass} aria-hidden="true" />
      <span className="row__body">
        <span className="row__title">{task.title}</span>
        <span className="row__meta">
          {kindLabel ? <Chip tone="kind">{kindLabel}</Chip> : null}
          <span className={whenClass}>
            {task.dueAt
              ? formatDue(task.dueAt, task.duePrecision, now)
              : formatRecorded(task.createdAt)}
          </span>
          {taskChips(task, true)}
        </span>
      </span>
      <span className="row__chev" aria-hidden="true">
        ›
      </span>
    </button>
  );
}
