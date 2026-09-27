import type { Fact } from "../lib/facts.ts";
import { sourceCaption } from "../lib/facts.ts";
import { type ActionKind, Actions } from "./Actions.tsx";
import { GuessChip } from "./Chip.tsx";
import { SourceMessage } from "./SourceMessage.tsx";

const CHEVRON = (
  <svg
    viewBox="0 0 14 14"
    fill="none"
    stroke="currentColor"
    strokeWidth="1.6"
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
  >
    <path d="M3 5.2 7 9.2l4-4" />
  </svg>
);

/**
 * Строка записи памяти: текст, источник с датой, метка «Предположение»,
 * шеврон вниз. Касание раскрывает её на месте — исходное сообщение и
 * действия; повторное сворачивает. Перехода на отдельный экран нет.
 *
 * «Подтвердить» есть только у предположения; «Удалить» — у всех. Отказ
 * базы — текстом под кнопками, запись остаётся на месте (инвариант 4).
 */
export function FactRow({
  fact,
  now,
  open,
  busy,
  error,
  onToggle,
  onConfirm,
  onDelete,
}: {
  fact: Fact;
  now: Date;
  open: boolean;
  busy: ActionKind | null;
  error: string | null;
  onToggle: () => void;
  onConfirm: () => void;
  onDelete: () => void;
}) {
  const guess = fact.status === "guess";
  return (
    <div className={open ? "fact fact--open" : "fact"}>
      <button type="button" className="row row--fact" aria-expanded={open} onClick={onToggle}>
        <span className="row__body">
          <span className="row__title">{fact.text}</span>
          <span className="row__meta">
            <span className="row__src">{sourceCaption(fact)}</span>
            <GuessChip fact={fact} />
          </span>
        </span>
        <span className="row__chev row__chev--down" aria-hidden="true">
          {CHEVRON}
        </span>
      </button>
      {open ? (
        <div className="fact__more">
          <SourceMessage compact message={fact.source} now={now} tone={guess ? "guess" : undefined} />
          <Actions
            inline
            busy={busy}
            error={error}
            primary={
              guess
                ? {
                    kind: "confirm",
                    label: "Подтвердить",
                    busyLabel: "Подтверждаем…",
                    onClick: onConfirm,
                  }
                : undefined
            }
            onDelete={onDelete}
          />
        </div>
      ) : null}
    </div>
  );
}
