import type { ReactNode } from "react";

import type { Task } from "../lib/tasks.ts";

/**
 * Пилюля-метка: приоритет, чьё обещание, вид записи, «перепроверьте».
 * Цвет говорит о смысле (`design.md` §1): жёлтый — важно, фиолетовый —
 * предположение помощника, синий — моё обещание.
 */
export type ChipTone = "high" | "mine" | "theirs" | "review" | "kind";

export function Chip({ tone, children }: { tone: ChipTone; children: ReactNode }) {
  return <span className={`chip chip--${tone}`}>{children}</span>;
}

/* Метки задачи — те же слова, что у бота в подтверждении. `compact` — строка
   списка: слова длиннее и с именем, на карточке имя стоит в своей строке. */

/** Только «Высокий»: обычный и низкий приоритет в списке не помечаются. */
export function PriorityChip({ task, compact }: { task: Task; compact: boolean }) {
  if (task.priority !== "high") {
    return null;
  }
  return <Chip tone="high">{compact ? "Высокий приоритет" : "Высокий"}</Chip>;
}

/** «Я обещал» / «Обещали мне», в списке — с первым названным человеком. */
export function PromiseChip({ task, compact }: { task: Task; compact: boolean }) {
  if (!task.promise) {
    return null;
  }
  const word = task.promise === "mine" ? "Я обещал" : "Обещали мне";
  const person = compact ? task.people[0] : undefined;
  return (
    <Chip tone={task.promise === "mine" ? "mine" : "theirs"}>
      {person ? `${word}: ${person}` : word}
    </Chip>
  );
}

/** Помощник не уверен в разборе — цветом предположения. */
export function ReviewChip({ task }: { task: Task }) {
  return task.needsReview ? <Chip tone="review">Перепроверьте</Chip> : null;
}

const KIND_LABEL: Record<Task["kind"], string | null> = {
  task: null,
  idea: "Идея",
  wish: "Желание",
};

/** Вид записи: задача метки не получает, она по умолчанию. */
export function KindChip({ task }: { task: Task }) {
  const label = KIND_LABEL[task.kind];
  return label ? <Chip tone="kind">{label}</Chip> : null;
}
