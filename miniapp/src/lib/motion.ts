/**
 * Движение (`design.md` §1): прокрутка цифр по разрядам, полосы, плывущий фон.
 *
 * Само движение — CSS (`styles.css`, раздел «Движение»): переходы и анимации
 * только `transform`, при `prefers-reduced-motion: reduce` их нет и всё стоит
 * в конечном виде. Здесь — чистая раскладка числа на разряды для
 * `RollingNumber`; проверяется на Node.
 */

/**
 * Разряды числа слева направо: 310 → [3, 1, 0]. `minDigits` дополняет нулями
 * слева — минуты «05». Дробное — вниз, отрицательное и не число — ноль: часы
 * не бывают меньше нуля, а пустая цифра на экране хуже нуля.
 */
export function digitsOf(value: number, minDigits = 1): number[] {
  const whole = Number.isFinite(value) && value > 0 ? Math.floor(value) : 0;
  return [...String(whole).padStart(minDigits, "0")].map(Number);
}
