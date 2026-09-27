/**
 * Разбор строк базы — общие мелочи для `tasks.ts` и `facts.ts`.
 *
 * PostgREST отдаёт строки как есть; здесь они приводятся к типам без
 * доверия: незнакомое значение перечисления — запасное, негодная дата —
 * `null`. Чистый модуль без React и сети.
 */

/** Строка ответа как словарь, если это вообще объект. */
export function recordOf(row: unknown): Record<string, unknown> | null {
  return typeof row === "object" && row !== null ? (row as Record<string, unknown>) : null;
}

/** Значение из перечисления или запасное — база шире, чем знает приложение. */
export function oneOf<T extends string>(value: unknown, allowed: readonly T[], fallback: T): T {
  return typeof value === "string" && (allowed as readonly string[]).includes(value)
    ? (value as T)
    : fallback;
}

/** Дата из строки ISO; пусто или не разобралось — `null`. */
export function dateOrNull(value: unknown): Date | null {
  if (typeof value !== "string") {
    return null;
  }
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}
