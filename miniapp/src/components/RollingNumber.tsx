import type { CSSProperties } from "react";

import { digitsOf } from "../lib/motion.ts";

/** Колонка 0–9 одного разряда: едет к своей цифре сдвигом по высоте. */
const COLUMN = Array.from({ length: 10 }, (_, digit) => <span key={digit}>{digit}</span>);

/**
 * Число, которое прокручивается по разрядам, как NumberFlow (`design.md` §1):
 * каждый разряд — колонка 0–9, сдвинутая к своей цифре. Сменилось значение —
 * колонки едут от прежней цифры к новой (переход `transform`, 320 мс, разряды
 * слева направо с шагом 40 мс); при первом показе — от нуля. Разряды
 * держатся по месту справа: у 9 → 10 единицы остаются той же колонкой.
 *
 * При `prefers-reduced-motion: reduce` переходов нет — число сразу в конечном
 * виде (`styles.css`). Экранный диктор читает число целиком, а не колонки.
 *
 * `minDigits` — разрядов не меньше (минуты «05»); `wait` — задержка всего
 * числа, чтобы соседние цифры экрана шли лесенкой.
 */
export function RollingNumber({
  value,
  minDigits = 1,
  wait = 0,
}: {
  value: number;
  minDigits?: number;
  wait?: number;
}) {
  const digits = digitsOf(value, minDigits);
  return (
    <span className="num" role="img" aria-label={digits.join("")}>
      {digits.map((digit, index) => (
        <span className="dg" key={digits.length - index} aria-hidden="true">
          <span
            className="dg__col"
            style={
              {
                "--d": digit,
                "--i": index,
                "--wait": `${wait}ms`,
              } as CSSProperties
            }
          >
            {COLUMN}
          </span>
        </span>
      ))}
    </span>
  );
}
