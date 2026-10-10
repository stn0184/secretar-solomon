import type { CSSProperties } from "react";

import { type Fact, aboutFacts } from "../lib/facts.ts";
import { sphereCards, spheresHoursEyebrow, unspheredMinutes } from "../lib/hours.ts";
import type { Sphere } from "../lib/spheres.ts";
import { sphereOf, unspheredMeta } from "../lib/spheres.ts";
import type { Task } from "../lib/tasks.ts";
import { Empty } from "./Empty.tsx";
import { ErrorNote } from "./ErrorNote.tsx";
import { Header } from "./Header.tsx";
import { HoursFigure } from "./HoursFigure.tsx";
import { Skeleton } from "./Skeleton.tsx";
import { SphereCard } from "./SphereCard.tsx";
import type { WeekState } from "./WeekTab.tsx";

/**
 * Что знает вкладка о сферах: читаются вместе с делами (`App`). `failed` —
 * текст отказа и «Обновить».
 */
export type SpheresState =
  | { kind: "loading" }
  | { kind: "ready"; spheres: readonly Sphere[] }
  | { kind: "failed"; message: string };

/**
 * Вкладка «Сферы» (`techspec/32-redesign.md` §32.3, артборд 03): все сферы
 * разом, по местам — часы за неделю, полоса для сравнения, открытые дела и
 * что Соломон знает. Сфер нет — одобренное пустое состояние (`design.md` §2,
 * артборд 09): пунктирное место под первую сферу и как их назвать.
 *
 * Часы — из того же чтения недели, что «Неделя»; не прочитались — текст и
 * «Обновить» под шапкой, карточки остаются без часов. Записи памяти
 * читаются при первом открытии «Сфер»; пока нет — карточки без записи.
 *
 * Внизу — карточка «Без сферы и о себе»: дела без сферы, записи о самом
 * владельце и часы «без сферы» за неделю; касание открывает «О себе» с прежней
 * правкой (§8.4). Карточки нет, только когда точно известно, что ни дел без
 * сферы, ни записей нет.
 *
 * `tasks` — открытые дела (из «Дел»); `facts` — все записи памяти, `null` —
 * ещё не прочитаны.
 */
export function SpheresTab({
  state,
  tasks,
  facts,
  week,
  onReload,
  onReloadWeek,
  onClose,
  onOpenAbout,
}: {
  state: SpheresState;
  tasks: readonly Task[];
  facts: readonly Fact[] | null;
  week: WeekState;
  onReload: () => void;
  onReloadWeek: () => void;
  onClose: () => void;
  onOpenAbout: () => void;
}) {
  const spheres = state.kind === "ready" ? state.spheres : [];
  const weekRows = week.kind === "ready" ? week.week.rows : null;
  const cards = sphereCards(spheres, weekRows, tasks, facts);
  const unsphered = tasks.filter((task) => sphereOf(task.sphereId, spheres) === null).length;
  const about = facts === null ? null : aboutFacts(facts).length;
  const known = state.kind === "ready" && about !== null;
  const meta = unspheredMeta(unsphered, about);
  const loose = unspheredMinutes(weekRows) ?? 0;
  const showAbout = !known || meta !== "" || loose > 0;

  return (
    <main className="screen screen--tabs">
      <Header
        eyebrow={spheres.length > 0 ? spheresHoursEyebrow(spheres.length) : "Ваши сферы"}
        title="Сферы"
      />
      {state.kind === "loading" ? <Skeleton rows={2} label="Загружаем сферы" /> : null}
      {state.kind === "failed" ? <ErrorNote message={state.message} onRetry={onReload} /> : null}
      {state.kind === "ready" && spheres.length > 0 && week.kind === "failed" ? (
        <ErrorNote message={week.message} onRetry={onReloadWeek} />
      ) : null}
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
      {cards.map((card, index) => (
        <SphereCard key={card.sphere.id} card={card} index={index} />
      ))}
      {showAbout ? (
        <button
          type="button"
          className="sph sph--plain"
          style={{ "--c": "var(--s-none)" } as CSSProperties}
          onClick={onOpenAbout}
        >
          <span className="sph__top">
            <span className="sph__name">Без сферы и о себе</span>
            {loose > 0 ? (
              <span className="sph__h">
                <HoursFigure minutes={loose} wait={150 + cards.length * 80} />
              </span>
            ) : null}
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
