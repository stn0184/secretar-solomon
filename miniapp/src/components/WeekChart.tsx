import type { CSSProperties } from "react";

import { hoursWords } from "../lib/format.ts";
import { AXIS_MARKS, type WeekRow } from "../lib/hours.ts";
import { type Sphere, sphereColor, sphereOf } from "../lib/spheres.ts";
import { RollingNumber } from "./RollingNumber.tsx";

/** Часы дня «5:25» с прокруткой разрядов; диктор читает «5 ч 25 мин». */
function DayValue({ minutes, wait }: { minutes: number; wait: number }) {
  return (
    <span className={minutes > 0 ? "wd__v" : "wd__v wd__v--zero"} role="img" aria-label={hoursWords(minutes)}>
      <RollingNumber value={Math.floor(minutes / 60)} wait={wait} />:
      <RollingNumber value={minutes % 60} minDigits={2} wait={wait} />
    </span>
  );
}

/**
 * «По дням» (артборд 02): строка на каждый день недели — день и дата, полосы
 * отрезков по оси 7–22 цветом сфер (переписка бледнее, будущая встреча —
 * контуром), справа часы дня цифрами, у будущих дней — «впереди». Сегодня
 * выделено и несёт линию «сейчас». Полосы вырастают лесенкой по дням.
 */
export function WeekChart({ rows, spheres }: { rows: readonly WeekRow[]; spheres: readonly Sphere[] }) {
  return (
    <div className="wk card">
      <div className="wk__axis" aria-hidden="true">
        <span />
        <div>
          {AXIS_MARKS.map((mark) => (
            <span key={mark.hour} style={{ left: `${mark.left}%` }}>
              {mark.hour}
            </span>
          ))}
        </div>
        <span />
      </div>
      {rows.map((row, index) => (
        <div key={row.day.getTime()} className={row.today ? "wd wd--today" : "wd"}>
          <div className="wd__l">
            {row.weekday}
            <small>{row.date}</small>
          </div>
          <div className="track" aria-hidden="true">
            {row.bars.map((bar, barIndex) => (
              <i
                key={barIndex}
                className={bar.kind === "chat" ? "bar grow bar--chat" : bar.future ? "bar grow bar--future" : "bar grow"}
                style={
                  {
                    "--c": sphereColor(sphereOf(bar.sphereId, spheres)),
                    left: `${bar.left}%`,
                    width: `${bar.width}%`,
                    "--wait": `${index * 60}ms`,
                  } as CSSProperties
                }
              />
            ))}
            {row.now !== null ? <i className="nowline" style={{ left: `${row.now}%` }} /> : null}
          </div>
          {row.minutes === null ? (
            <span className="wd__v wd__v--muted">впереди</span>
          ) : (
            <DayValue minutes={row.minutes} wait={120 + index * 60} />
          )}
        </div>
      ))}
    </div>
  );
}
