import type { CSSProperties } from "react";

import { type SphereCardData, cardMeta } from "../lib/hours.ts";
import { sphereColor } from "../lib/spheres.ts";
import { HoursFigure } from "./HoursFigure.tsx";

/** Мест палитры и материалов камня — столько же, сколько сфер (§30.1). */
const PLACES = 12;

/**
 * Карточка сферы на «Сферах» (артборд 03): название, часы за неделю, полоса
 * относительно наибольшей сферы, открытые дела (без разбивки часов на
 * встречи и переписку — 036) и первая запись «что Соломон знает» с «ещё N».
 * Одна разметка: в «Банк-люксе» — карта из камня места с чипом и бликом
 * (блик проходит один раз, карты — по очереди), в «Тихой роскоши» — белая
 * карточка с цветной полосой. Карточка не нажимается: правки знаний сферы в
 * приложении нет.
 */
export function SphereCard({ card, index }: { card: SphereCardData; index: number }) {
  const place = ((card.sphere.place - 1) % PLACES) + 1;
  const wait = 150 + index * 80;
  return (
    <section
      className="sph sph--stone"
      aria-label={card.sphere.name}
      style={
        {
          "--c": sphereColor(card.sphere),
          "--mat": `var(--mat-${place})`,
          "--wait": `${500 + index * 160}ms`,
        } as CSSProperties
      }
    >
      <div className="sph__top">
        <span className="sph__chip" aria-hidden="true" />
        <span className="sph__name">{card.sphere.name}</span>
        {card.minutes ? (
          <span className="sph__h">
            <HoursFigure minutes={card.minutes} wait={wait} />
          </span>
        ) : null}
      </div>
      {card.width !== null ? (
        <div className="sph__bar" aria-hidden="true">
          <i className="grow" style={{ width: `${card.width}%`, "--wait": `${wait}ms` } as CSSProperties} />
        </div>
      ) : null}
      <div className="sph__meta">{cardMeta(card)}</div>
      {card.fact ? (
        <div className="sph__fact">
          <b>{card.fact}</b>
          {card.more > 0 ? ` · ещё ${card.more}` : null}
        </div>
      ) : null}
    </section>
  );
}
