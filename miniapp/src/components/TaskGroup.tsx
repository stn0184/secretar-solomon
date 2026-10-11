import type { CSSProperties } from "react";

import { type Sphere, sphereColor, sphereOf } from "../lib/spheres.ts";
import type { Task } from "../lib/tasks.ts";
import { TaskRow } from "./TaskRow.tsx";

/**
 * Группа дел (`design.md` §1, прототип 036): заголовок «Просрочено · 2» —
 * название и число через «·», число тише; «Просрочено» целиком цветом
 * «просрочено» (`late`). В «По сферам» — точка цвета сферы перед именем
 * (`sphere`; у «Без сферы» точки нет). Шрифт заголовка — у темы: капитель в
 * «Банк-люксе», антиква в «Тихой роскоши». Ниже — карточка со строками.
 *
 * Одна и та же для групп срока, сфер и «Без часа» на «Сегодня».
 */
export function TaskGroup({
  title,
  tasks,
  spheres,
  now,
  doneId,
  onOpen,
  late = false,
  sphere,
  showSphere,
  dayKnown = false,
}: {
  title: string;
  tasks: Task[];
  spheres: readonly Sphere[];
  now: Date;
  /** Дело, которое только что отметили «Сделано» и база перевела дальше. */
  doneId: string | null;
  onOpen: (task: Task) => void;
  /** Группа «Просрочено» — заголовок цветом «просрочено». */
  late?: boolean;
  /** Группа сферы в «По сферам»: точка её цвета; `null` — «Без сферы», без точки. */
  sphere?: Sphere | null;
  showSphere: boolean;
  dayKnown?: boolean;
}) {
  const marks = spheres.length > 0;
  return (
    <section className="sec">
      <h2
        className={late ? "grp grp--late" : "grp"}
        style={sphere ? ({ "--c": sphereColor(sphere) } as CSSProperties) : undefined}
      >
        {sphere ? <i className="grp__dot" aria-hidden="true" /> : null}
        <span>{title}</span>
        <span className="grp__n">
          <span aria-hidden="true">·</span> {tasks.length}
        </span>
      </h2>
      <div className="card">
        {tasks.map((task) => (
          <TaskRow
            key={task.id}
            task={task}
            sphere={sphereOf(task.sphereId, spheres)}
            marks={marks}
            showSphere={showSphere}
            dayKnown={dayKnown}
            now={now}
            justDone={task.id === doneId}
            onOpen={onOpen}
          />
        ))}
      </div>
    </section>
  );
}
