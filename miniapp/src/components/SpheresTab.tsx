import type { CSSProperties } from "react";

import type { Sphere } from "../lib/spheres.ts";
import { spheresEyebrow, unspheredMeta } from "../lib/spheres.ts";
import { Empty } from "./Empty.tsx";
import { ErrorNote } from "./ErrorNote.tsx";
import { Header } from "./Header.tsx";
import { Skeleton } from "./Skeleton.tsx";

/**
 * Что знает вкладка о сферах: читаются вместе с делами (`App`). `failed` —
 * текст отказа и «Обновить».
 */
export type SpheresState =
  | { kind: "loading" }
  | { kind: "ready"; spheres: readonly Sphere[] }
  | { kind: "failed"; message: string };

/**
 * Вкладка «Сферы» (`techspec/32-redesign.md` §32.2). Сфер нет — одобренное
 * пустое состояние (`design.md` §2, артборд 09): пунктирное место под первую
 * сферу и как их назвать. Сферы есть — шапка «N из 12»; карточки сфер с
 * часами, делами и знаниями добавит этап 035 (§32.3).
 *
 * Внизу — карточка «Без сферы и о себе»: дела без сферы и записи о самом
 * владельце; касание открывает «О себе» с прежней правкой (§8.4). Отдельной
 * вкладки у «О себе» больше нет. Карточки нет, только когда точно известно,
 * что ни дел без сферы, ни записей нет.
 *
 * `unsphered` — сколько дел без сферы; `facts` — сколько записей о себе,
 * `null` — ещё не прочитаны.
 */
export function SpheresTab({
  state,
  unsphered,
  facts,
  onReload,
  onClose,
  onOpenAbout,
}: {
  state: SpheresState;
  unsphered: number;
  facts: number | null;
  onReload: () => void;
  onClose: () => void;
  onOpenAbout: () => void;
}) {
  const spheres = state.kind === "ready" ? state.spheres : [];
  const known = state.kind === "ready" && facts !== null;
  const meta = unspheredMeta(unsphered, facts);
  const showAbout = !known || meta !== "";

  return (
    <main className="screen screen--tabs">
      <Header
        eyebrow={spheres.length > 0 ? spheresEyebrow(spheres.length) : "Ваши сферы"}
        title="Сферы"
      />
      {state.kind === "loading" ? <Skeleton rows={2} label="Загружаем сферы" /> : null}
      {state.kind === "failed" ? <ErrorNote message={state.message} onRetry={onReload} /> : null}
      {state.kind === "ready" && spheres.length === 0 ? (
        <Empty
          blank={
            <div className="blank">
              <b>Первая сфера</b>
              Появится здесь, когда вы назовёте её в чате
            </div>
          }
          title="Сфер пока нет"
          text="Назовите в чате свои сферы — бизнесы, семью, интересы. Соломон будет раскладывать по ним дела, переписку и часы."
          examples={["Мои сферы: работа, семья, спорт", "Это по работе: отвечаю за продажи"]}
          hint="Сфер может быть до 12. Дело без сферы — тоже нормально."
          onClose={onClose}
        />
      ) : null}
      {showAbout ? (
        <button
          type="button"
          className="sph sph--plain"
          style={{ "--c": "var(--s-none)" } as CSSProperties}
          onClick={onOpenAbout}
        >
          <span className="sph__top">
            <span className="sph__name">Без сферы и о себе</span>
            <span className="chev" aria-hidden="true">
              ›
            </span>
          </span>
          {meta ? <span className="sph__meta">{meta}</span> : null}
        </button>
      ) : null}
    </main>
  );
}
