import type { CSSProperties } from "react";

import { hoursWords } from "../lib/format.ts";
import type { SphereMinutes } from "../lib/hours.ts";
import { type Sphere, sphereColor, sphereOf } from "../lib/spheres.ts";

/**
 * Доли сфер за день (артборд 01): полоса из кусков цвета сфер по их минутам и
 * подписи «VoiceFin 2 ч 15 мин» — в порядке базы: сферы по заведению, «без
 * сферы» последней. Куски вырастают лесенкой (380 мс), при «уменьшить
 * движение» стоят сразу. Сфера без минут сюда не попадает — её нет в ответе.
 *
 * Пока у владельца нет ни одной сферы, подписей нет (036): «Без сферы» с теми
 * же часами, что крупная цифра, ничего не говорит; полоса остаётся.
 */
export function SphereSplit({ rows, spheres }: { rows: readonly SphereMinutes[]; spheres: readonly Sphere[] }) {
  const parts = rows.map((row) => ({
    key: row.sphereId ?? "none",
    name: row.name ?? "Без сферы",
    minutes: row.meetings + row.chat,
    color: sphereColor(sphereOf(row.sphereId, spheres)),
  }));
  return (
    <div className="split">
      <div className="stripe" aria-hidden="true">
        {parts.map((part, index) => (
          <i
            key={part.key}
            className="grow"
            style={{ "--c": part.color, flex: `${part.minutes} 0 0`, "--wait": `${150 + index * 70}ms` } as CSSProperties}
          />
        ))}
      </div>
      {spheres.length > 0 ? (
        <div className="legend">
          {parts.map((part) => (
            <span key={part.key} style={{ "--c": part.color } as CSSProperties}>
              <i aria-hidden="true" />
              {part.name} <b>{hoursWords(part.minutes)}</b>
            </span>
          ))}
        </div>
      ) : null}
    </div>
  );
}
