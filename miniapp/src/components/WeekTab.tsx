import { formatTime, formatWeekRange } from "../lib/format.ts";
import { type WeekData, sphereBars, totalMinutes, weekRows } from "../lib/hours.ts";
import type { Sphere } from "../lib/spheres.ts";
import { Empty } from "./Empty.tsx";
import { ErrorNote } from "./ErrorNote.tsx";
import { Header } from "./Header.tsx";
import { HoursFigure } from "./HoursFigure.tsx";
import { Skeleton } from "./Skeleton.tsx";
import { SphereBars } from "./SphereBars.tsx";
import { WeekChart } from "./WeekChart.tsx";

/**
 * Что знает «Неделя»: часы недели, часы по дням и отрезки недели — одно
 * чтение с «Сферами» (`App`); `idle` — ещё не спрашивали.
 */
export type WeekState =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "ready"; week: WeekData }
  | { kind: "failed"; message: string };

/**
 * Вкладка «Неделя» (`techspec/32-redesign.md` §32.3, артборд 02): итог недели
 * крупной цифрой «примерно, с понедельника», строка на каждый день с часами
 * цифрами и «впереди» у будущих, ниже — сферы полосами. Переписка отдельно
 * не называется (036): её минуты — внутри часов.
 * Часы считает база (`sphere_hours`): за неделю и за каждый прошедший день.
 * Часов за неделю нет — одобренное пустое (`design.md` §2, артборд 08): ни
 * цифры, ни пустой диаграммы.
 */
export function WeekTab({
  state,
  spheres,
  now,
  onReload,
  onClose,
}: {
  state: WeekState;
  spheres: readonly Sphere[];
  now: Date;
  onReload: () => void;
  onClose: () => void;
}) {
  const eyebrow = `Неделя · ${formatWeekRange(now)}`;

  if (state.kind !== "ready") {
    return (
      <main className="screen screen--tabs">
        <Header eyebrow={eyebrow} />
        {state.kind === "failed" ? (
          <ErrorNote message={state.message} onRetry={onReload} />
        ) : (
          <Skeleton rows={7} label="Загружаем неделю" />
        )}
      </main>
    );
  }

  const { week } = state;
  const total = totalMinutes(week.rows);
  if (total === 0) {
    return (
      <main className="screen screen--tabs">
        <Header eyebrow={eyebrow} />
        <Empty
          title="За неделю часов нет"
          text="Соломон считает часы по встречам, у которых есть время, и по переписке в личных чатах. Таймеров нет — ничего засекать не нужно."
          examples={["Встреча с Олегом в среду с 11 до 13", "Это по работе: созвон в пятницу с 10 до 11"]}
          hint="Встреча без конца займёт час — поправить можно словом: «до 16»."
          action="Написать Соломону"
          onClose={onClose}
        />
      </main>
    );
  }

  return (
    <main className="screen screen--tabs">
      <header className="hd">
        <span className="eyebrow">{eyebrow}</span>
        <HoursFigure big minutes={total} wait={100} />
        <span className="big-sub">примерно, с понедельника</span>
      </header>
      <section className="sec">
        <h2 className="sec__h">
          <span>По дням</span>
          <em>сейчас {formatTime(now)}</em>
        </h2>
        <WeekChart rows={weekRows(week.days, week.spans, week.dayMinutes, now)} spheres={spheres} />
      </section>
      <section className="sec">
        <h2 className="sec__h">
          <span>По сферам</span>
          <em>с понедельника</em>
        </h2>
        <SphereBars bars={sphereBars(week.rows)} spheres={spheres} />
      </section>
    </main>
  );
}
