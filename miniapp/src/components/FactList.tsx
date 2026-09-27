import { useState } from "react";

import { type Fact, factsSubtitle, groupFacts } from "../lib/facts.ts";
import type { ActionResult } from "../lib/supabase.ts";
import { confirmRemoveFact } from "../lib/telegram.ts";
import type { ActionKind } from "./Actions.tsx";
import { Empty } from "./Empty.tsx";
import { ErrorNote } from "./ErrorNote.tsx";
import { FactRow } from "./FactRow.tsx";
import { Header } from "./Header.tsx";
import { Skeleton } from "./Skeleton.tsx";

/**
 * Состояния экрана «О себе» — `design.md` §2: до первого открытия вкладки
 * (`idle`), загрузка, ошибка, пусто, штатно.
 */
export type FactsState =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "ready"; facts: Fact[] }
  | { kind: "failed"; message: string };

const HINTS = ["«У меня Toyota Camry»", "«Работаю до шести, по пятницам до пяти»"];

/**
 * Экран «О себе»: записи по категориям, у каждой — раскрытие на месте с
 * действиями. Список приходит из `App`, действия уходят туда же и
 * возвращают итог: экран показывает занятость и отказ у той строки, где
 * нажали.
 */
export function FactList({
  state,
  now,
  onReload,
  onClose,
  onConfirm,
  onRemove,
}: {
  state: FactsState;
  now: Date;
  onReload: () => void;
  onClose: () => void;
  onConfirm: (fact: Fact) => Promise<ActionResult>;
  onRemove: (fact: Fact) => Promise<ActionResult>;
}) {
  const [open, setOpen] = useState<string | null>(null);
  const [busy, setBusy] = useState<{ id: string; kind: ActionKind } | null>(null);
  const [error, setError] = useState<{ id: string; message: string } | null>(null);

  const facts = state.kind === "ready" ? state.facts : [];
  const groups = groupFacts(facts);

  async function run(fact: Fact, kind: ActionKind, action: () => Promise<ActionResult>) {
    setBusy({ id: fact.id, kind });
    setError(null);
    const result = await action();
    setBusy(null);
    if (!result.ok) {
      setError({ id: fact.id, message: result.message });
    }
  }

  async function onDelete(fact: Fact) {
    if (await confirmRemoveFact(fact.text)) {
      await run(fact, "delete", () => onRemove(fact));
    }
  }

  return (
    <main className="screen screen--tabs">
      <Header title="О себе" subtitle={facts.length > 0 ? factsSubtitle(facts) : undefined} />
      {state.kind === "idle" || state.kind === "loading" ? (
        <Skeleton label="Загружаем записи" />
      ) : null}
      {state.kind === "failed" ? <ErrorNote message={state.message} onRetry={onReload} /> : null}
      {state.kind === "ready" && facts.length === 0 ? (
        <Empty
          title="Пока ничего о вас не знаю"
          text="Расскажите боту между делом — какая машина, во сколько заканчиваете работу, как зовут детей. Он запомнит и не будет переспрашивать."
          hints={HINTS}
          note="Что помощник поймёт из поручений сам, будет помечено как предположение — его можно подтвердить или удалить."
          onClose={onClose}
        />
      ) : null}
      {groups.map((group) => (
        <section className="sec" key={group.category}>
          <h2 className="sec__h">
            <span>{group.title}</span>
          </h2>
          <div className="card">
            {group.facts.map((fact) => (
              <FactRow
                key={fact.id}
                fact={fact}
                now={now}
                open={open === fact.id}
                busy={busy?.id === fact.id ? busy.kind : null}
                error={error?.id === fact.id ? error.message : null}
                onToggle={() => setOpen((current) => (current === fact.id ? null : fact.id))}
                onConfirm={() => void run(fact, "confirm", () => onConfirm(fact))}
                onDelete={() => void onDelete(fact)}
              />
            ))}
          </div>
        </section>
      ))}
    </main>
  );
}
