import { hoursWords } from "../lib/format.ts";
import { hoursParts } from "../lib/hours.ts";
import { RollingNumber } from "./RollingNumber.tsx";

/**
 * Часы цифрами с прокруткой разрядов (`design.md` §1): «5 ч 10 мин». Нулевая
 * часть не пишется — «45 мин», «3 ч», как в ответе бота. `big` — крупная
 * цифра «Сегодня» и «Недели»: единицы мельче и цветом темы; без него — часы
 * в строке («По сферам», карточка сферы). Минуты едут на 80 мс позже часов —
 * лесенкой, как на артбордах; при «уменьшить движение» всё сразу.
 *
 * Диктор читает число словами целиком, а не разряды.
 */
export function HoursFigure({ minutes, wait = 0, big = false }: { minutes: number; wait?: number; big?: boolean }) {
  const parts = hoursParts(minutes);
  return (
    <span className={big ? "big" : "fig"} role="img" aria-label={hoursWords(minutes)}>
      {parts.hours !== null ? (
        <>
          <RollingNumber value={parts.hours} wait={wait} />
          <span className="u">ч</span>
        </>
      ) : null}
      {parts.minutes !== null ? (
        <>
          <RollingNumber value={parts.minutes} wait={wait + 80} />
          <span className="u">мин</span>
        </>
      ) : null}
    </span>
  );
}
