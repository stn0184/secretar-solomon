import type { CSSProperties } from "react";

import { formatDue } from "../lib/format.ts";
import { repeatShort } from "../lib/repeat.ts";
import { type Sphere, sphereColor } from "../lib/spheres.ts";
import { type Task, rowDue } from "../lib/tasks.ts";
import { KindChip, PromiseChip, ReviewChip } from "./Chip.tsx";
import { SphereMark } from "./SphereMark.tsx";

/**
 * Строка дела (`design.md` §1, прототип 036) — одна в «Делах» и в «Без часа»
 * на «Сегодня»: `[отметка сферы] суть · срок справа · «›»`, под сутью — один
 * ряд: имя сферы (`showSphere`), «↻ правило», метки. Ряда нет, если показать
 * нечего. Вся строка — кнопка: касание открывает карточку. Кружка «Сделано»
 * нет — «Сделано» только в карточке.
 *
 * Колонка отметки есть, только когда у владельца есть сферы (`marks`); у дела
 * без сферы она пустая — суть не прыгает. «Без сферы» в строке не пишется
 * никогда. Высокий приоритет — тёплая полоса у левого края, без метки: диктору
 * слова остаются. Срок — цветом: просрочено, сегодня или спокойно; `dayKnown`
 * — экран и так про сегодня, день не повторяется.
 *
 * `justDone` — повторяющееся дело только что отметили в карточке: под сутью
 * срок, который вернула база, до следующего чтения списка.
 */
export function TaskRow({
  task,
  sphere,
  marks,
  showSphere,
  dayKnown = false,
  now,
  justDone,
  onOpen,
}: {
  task: Task;
  sphere: Sphere | null;
  /** У владельца есть сферы — колонка отметки есть у каждой строки. */
  marks: boolean;
  /** Имя сферы под сутью — в «По сроку» и на «Сегодня»; в «По сферам» его называет группа. */
  showSphere: boolean;
  dayKnown?: boolean;
  now: Date;
  justDone: boolean;
  onOpen: (task: Task) => void;
}) {
  const due = rowDue(task, now, dayKnown);
  const high = task.priority === "high";
  const named = showSphere && sphere !== null;
  const meta = named || task.repeat !== null || task.kind !== "task" || task.promise !== null || task.needsReview;
  const cls = ["task", marks ? "" : "task--bare", high ? "task--high" : ""].filter(Boolean).join(" ");
  const dueCls = due?.tone ? `task__due task__due--${due.tone}` : "task__due";

  return (
    <button
      type="button"
      className={cls}
      style={{ "--c": sphereColor(sphere) } as CSSProperties}
      onClick={() => onOpen(task)}
    >
      {marks ? sphere ? <SphereMark sphere={sphere} /> : <span className="task__nomark" aria-hidden="true" /> : null}
      <span className="task__b">
        <span className="task__t">
          {high ? <span className="sr">Высокий приоритет. </span> : null}
          {task.title}
        </span>
        {meta ? (
          <span className="task__m">
            {named ? <span>{sphere.name}</span> : null}
            {task.repeat ? (
              <span className="task__rep">
                <i aria-hidden="true">↻</i>
                {repeatShort(task.repeat)}
              </span>
            ) : null}
            <KindChip task={task} />
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
      <span className={dueCls}>
        {due?.tone === "late" ? <span className="sr">Просрочено: </span> : null}
        {due?.text ?? ""}
      </span>
      <span className="chev" aria-hidden="true">
        ›
      </span>
    </button>
  );
}
