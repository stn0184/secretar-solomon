import type { ReactNode } from "react";

/**
 * Пилюля-метка: приоритет, чьё обещание, вид записи, «перепроверьте».
 * Цвет говорит о смысле (`design.md` §1): жёлтый — важно, фиолетовый —
 * предположение помощника, синий — моё обещание.
 */
export type ChipTone = "high" | "mine" | "theirs" | "review" | "kind";

export function Chip({ tone, children }: { tone: ChipTone; children: ReactNode }) {
  return <span className={`chip chip--${tone}`}>{children}</span>;
}
