import type { CSSProperties } from "react";

import type { SphereBar } from "../lib/hours.ts";
import { type Sphere, sphereColor, sphereOf } from "../lib/spheres.ts";
import { HoursFigure } from "./HoursFigure.tsx";

/**
 * «По сферам» (артборд 02): сфера — название, часы за неделю цифрами и
 * полоса относительно наибольшей. Разбивки «встречи · переписка» нет (036):
 * минуты переписки — внутри часов. Порядок — как отдала база: по заведению,
 * «без сферы» последней. Полосы вырастают лесенкой.
 */
export function SphereBars({ bars, spheres }: { bars: readonly SphereBar[]; spheres: readonly Sphere[] }) {
  return (
    <div className="hbars">
      {bars.map((bar, index) => {
        const wait = 200 + index * 80;
        return (
          <div
            key={bar.sphereId ?? "none"}
            className="hb"
            style={{ "--c": sphereColor(sphereOf(bar.sphereId, spheres)) } as CSSProperties}
          >
            <span className="hb__n">
              <i aria-hidden="true" />
              {bar.name}
            </span>
            <span className="hb__v">
              <HoursFigure minutes={bar.minutes} wait={wait} />
            </span>
            <span className="hb__track" aria-hidden="true">
              <i className="grow" style={{ width: `${bar.width}%`, "--wait": `${wait}ms` } as CSSProperties} />
            </span>
          </div>
        );
      })}
    </div>
  );
}
