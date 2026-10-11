import type { CSSProperties } from "react";

import { formatDue } from "../lib/format.ts";
import { repeatShort } from "../lib/repeat.ts";
import { type Sphere, sphereColor, sphereName } from "../lib/spheres.ts";
import { type Task, rowDue } from "../lib/tasks.ts";
import { KindChip, PriorityChip, PromiseChip, ReviewChip } from "./Chip.tsx";
import { SphereMark } from "./SphereMark.tsx";

/**
 * Строка дела (`techspec/32-redesign.md` §32.2, артборд 04): отметка сферы,
 * суть, «сфера · срок» со значком из чата, метки вторым рядом и «›». Вся
 * строка — кнопка: касание открывает карточку. Кружка «Сделано» нет —
 * «Сделано» только в карточке.
 *
 * У повторяющегося дела за сроком — «↻» и правило коротко, подписью, а не
 * меткой. `justDone` — его только что отметили в карточке: под строкой срок,
 * который вернула база, до следующего чтения списка.
 */
export function TaskRow({
  task,
  sphere,
  now,
  justDone,
  onOpen,
}: {
  task: Task;
  sphere: Sphere | null;
  now: Date;
  justDone: boolean;
  onOpen: (task: Task) => void;
}) {
  const due = rowDue(task, now);
  const chips =
    task.kind !== "task" || task.priority === "high" || task.promise !== null || task.needsReview;

  return (
    <button
      type="button"
      className="task"
      style={{ "--c": sphereColor(sphere) } as CSSProperties}
      onClick={() => onOpen(task)}
    >
      <SphereMark sphere={sphere} />
      <span className="task__b">
        <span className="task__t">{task.title}</span>
        <span className="task__m">
          <span>{sphereName(sphere)}</span>
          {due ? (
            <span className={due.tone === "late" ? "task__due task__late" : "task__due"}>
              {due.text}
            </span>
          ) : null}
          {task.repeat ? (
            <span className="task__rep">
              <i aria-hidden="true">↻</i>
              {repeatShort(task.repeat)}
            </span>
          ) : null}
        </span>
        {chips ? (
          <span className="task__m">
            <KindChip task={task} />
            <PriorityChip task={task} compact />
            <PromiseChip task={task} compact />
            <ReviewChip task={task} />
          </span>
        ) : null}
        {justDone && task.dueAt ? (
          <span className="task__next">
            ✓ Сделано. Следующий раз: {formatDue(task.dueAt, task.duePrecision, now, task.duration)}
          </span>
        ) : null}
      </span>
      <span className="chev" aria-hidden="true">
        ›
      </span>
    </button>
  );
}
