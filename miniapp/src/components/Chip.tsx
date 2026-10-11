import type { ReactNode } from "react";

import type { Fact } from "../lib/facts.ts";
import type { Task } from "../lib/tasks.ts";

/**
 * Пилюля-метка: приоритет, чьё обещание, вид записи, «перепроверьте».
 * Цвет говорит о смысле (`design.md` §1): жёлтый — важно, фиолетовый —
 * предположение помощника, синий — моё обещание; «Обещали мне» и вид —
 * спокойной подложкой. Тон подобран под тему.
 */
export type ChipTone = "high" | "mine" | "theirs" | "review" | "kind";

export function Chip({ tone, children }: { tone: ChipTone; children: ReactNode }) {
  return <span className={`chip chip--${tone}`}>{children}</span>;
}

/* Метки задачи — те же слова, что у бота в подтверждении. В строке списка
   обещание коротко — «🤝 Игорь» (`compact`), полные слова — в карточке. */

/** Только «Высокий» и только в карточке: в строке важное — тёплой полосой слева. */
export function PriorityChip({ task }: { task: Task }) {
  return task.priority === "high" ? <Chip tone="high">Высокий</Chip> : null;
}

/**
 * «Я обещал» / «Обещали мне» — в карточке. В строке (`compact`) коротко:
 * «🤝 Игорь» — вы обещали, «⏳ Олег» — обещали вам; имя — первое названное и
 * как записано, без склонения. Людей нет — значок и те же слова. Диктору
 * слова остаются и при имени.
 */
export function PromiseChip({ task, compact }: { task: Task; compact: boolean }) {
  if (!task.promise) {
    return null;
  }
  const mine = task.promise === "mine";
  const word = mine ? "Я обещал" : "Обещали мне";
  const tone = mine ? "mine" : "theirs";
  if (!compact) {
    return <Chip tone={tone}>{word}</Chip>;
  }
  const person = task.people[0];
  return (
    <Chip tone={tone}>
      <span aria-hidden="true">{mine ? "🤝" : "⏳"}</span>
      {person ? (
        <span>
          <span className="sr">{word}: </span>
          {person}
        </span>
      ) : (
        <span>{word}</span>
      )}
    </Chip>
  );
}

/** Помощник не уверен в разборе — цветом предположения. */
export function ReviewChip({ task }: { task: Task }) {
  return task.needsReview ? <Chip tone="review">Перепроверьте</Chip> : null;
}

/** Запись памяти выведена из поручения, а не сказана прямо — тот же цвет сомнения. */
export function GuessChip({ fact }: { fact: Fact }) {
  return fact.status === "guess" ? <Chip tone="review">Предположение</Chip> : null;
}

const KIND_LABEL: Record<Task["kind"], string | null> = {
  task: null,
  idea: "💡 Идея",
  wish: "💡 Желание",
};

/** Вид записи: задача метки не получает, она по умолчанию; 💡 — как в чате (§29.1). */
export function KindChip({ task }: { task: Task }) {
  const label = KIND_LABEL[task.kind];
  return label ? <Chip tone="kind">{label}</Chip> : null;
}
