/**
 * Память о пользователе: записи по категориям, подтверждение и удаление.
 *
 * Фильтра по владельцу здесь нет намеренно, как у задач: строки режет сама
 * база правилами доступа (`techspec/04-access.md` §4.2) по клейму
 * `telegram_id` в токене. Подтвердить (`update status`) и удалить — обычные
 * запросы под той же политикой, функций не нужно (`techspec/08-memory.md` §8.4).
 *
 * Источник записи читается тем же запросом вложенной строкой `messages`:
 * приложению нужен и текст сообщения (раскрытие записи), и его вид — по нему
 * подпись «с ваших слов» (сказано прямо) или «из сообщения» (выведено).
 * Подпись зависит от вида сообщения, а не от статуса: подтверждённое
 * предположение остаётся «из сообщения».
 *
 * Отказ — это текст на экране, а не исключение (`supabase.ts`); действие
 * считается сделанным только когда база вернула строку (инвариант 4).
 * Группировка и разбор строк — чистые функции, проверяются на Node.
 */

import type { SupabaseClient } from "@supabase/supabase-js";

import { formatDate, plural } from "./format.ts";
import { dateOrNull, oneOf, recordOf } from "./parse.ts";
import { type ActionResult, type Db, failed, query } from "./supabase.ts";

export type FactCategory = "family" | "home" | "car" | "work" | "habit" | "preference" | "other";
export type FactStatus = "fact" | "guess";

/** Сообщение, из которого взялась запись: текст, когда пришло и как разобрано. */
export interface FactSource {
  text: string;
  receivedAt: Date;
  /** `kind` разбора (`about_me`, `task`, …); пусто, если разбора не было. */
  kind: string | null;
}

export interface Fact {
  id: string;
  category: FactCategory;
  text: string;
  status: FactStatus;
  createdAt: Date;
  source: FactSource | null;
}

/** Порядок категорий на экране — фиксированный (одобренный прототип 006). */
export const CATEGORY_ORDER: readonly FactCategory[] = [
  "family",
  "home",
  "car",
  "work",
  "habit",
  "preference",
  "other",
];

export const CATEGORY_TITLES: Record<FactCategory, string> = {
  family: "Семья",
  home: "Дом",
  car: "Машина",
  work: "Работа",
  habit: "Привычки",
  preference: "Предпочтения",
  other: "Другое",
};

export type FactsResult = { ok: true; facts: Fact[] } | { ok: false; message: string };

/* ----------------------------------------------------------------- разбор */

// Источник — вложенная строка `messages` по внешнему ключу `source_message_id`;
// вид сообщения берётся прямо из jsonb разбора, чтобы не тащить его целиком.
const COLUMNS =
  "id, category, text, status, created_at, source:messages(text, received_at, kind:analysis->>kind)";

function parseSource(row: unknown): FactSource | null {
  const r = recordOf(row);
  if (!r) {
    return null;
  }
  const receivedAt = dateOrNull(r.received_at);
  if (typeof r.text !== "string" || !receivedAt) {
    return null;
  }
  return { text: r.text, receivedAt, kind: typeof r.kind === "string" ? r.kind : null };
}

/** Строка `facts` → запись. Не годится (нет id или текста) — `null`. */
export function parseFact(row: unknown): Fact | null {
  const r = recordOf(row);
  if (!r || typeof r.id !== "string" || typeof r.text !== "string") {
    return null;
  }
  return {
    id: r.id,
    category: oneOf<FactCategory>(r.category, CATEGORY_ORDER, "other"),
    text: r.text,
    // Незнакомый статус читается как предположение: лучше лишняя метка, чем
    // непроверенное, выданное за факт.
    status: oneOf<FactStatus>(r.status, ["fact", "guess"], "guess"),
    createdAt: dateOrNull(r.created_at) ?? new Date(0),
    source: parseSource(r.source),
  };
}

/* ------------------------------------------------------------ группировка */

export interface FactGroup {
  category: FactCategory;
  title: string;
  facts: Fact[];
}

/** Разложить по категориям в фиксированном порядке; пустые не возвращаются. */
export function groupFacts(facts: Fact[]): FactGroup[] {
  const buckets = new Map<FactCategory, Fact[]>();
  for (const fact of facts) {
    const bucket = buckets.get(fact.category);
    if (bucket) {
      bucket.push(fact);
    } else {
      buckets.set(fact.category, [fact]);
    }
  }
  return CATEGORY_ORDER.flatMap((category) => {
    const bucket = buckets.get(category);
    return bucket ? [{ category, title: CATEGORY_TITLES[category], facts: bucket }] : [];
  });
}

/* ------------------------------------------------------------------ слова */

/** «7 записей», «1 запись», «2 записи». */
export function countFacts(n: number): string {
  return `${n} ${plural(n, "запись", "записи", "записей")}`;
}

/** «3 предположения», «1 предположение», «5 предположений». */
export function countGuesses(n: number): string {
  return `${n} ${plural(n, "предположение", "предположения", "предположений")}`;
}

/** Подпись под заголовком: «7 записей · 3 предположения»; без предположений — только счёт. */
export function factsSubtitle(facts: Fact[]): string {
  const guesses = facts.filter((fact) => fact.status === "guess").length;
  const total = countFacts(facts.length);
  return guesses > 0 ? `${total} · ${countGuesses(guesses)}` : total;
}

/** Сказано прямо: источник — сообщение вида `about_me`. */
export function isOwnWords(fact: Fact): boolean {
  return fact.source?.kind === "about_me";
}

/** «с ваших слов · 12 сентября» / «из сообщения · 24 сентября» — дата записи. */
export function sourceCaption(fact: Fact): string {
  const origin = isOwnWords(fact) ? "с ваших слов" : "из сообщения";
  return `${origin} · ${formatDate(fact.createdAt)}`;
}

/* ------------------------------------------------------------------ база */

/** Прочитать записи владельца, старые первыми. Лимита нет: их десятки, не сотни. */
export async function loadFacts(db: Db): Promise<FactsResult> {
  const result = await query(db, (client: SupabaseClient) =>
    client.from("facts").select(COLUMNS).order("created_at", { ascending: true }),
  );
  if (!result.ok) {
    return failed("Не получилось прочитать записи", result);
  }

  const facts: Fact[] = [];
  for (const row of result.data ?? []) {
    const fact = parseFact(row);
    if (fact === null) {
      return {
        ok: false,
        message: "Не получилось прочитать записи. База вернула запись без текста.",
      };
    }
    facts.push(fact);
  }
  return { ok: true, facts };
}

/** «Подтвердить»: предположение становится фактом; база вернула строку — значит стало. */
export async function confirmFact(db: Db, factId: string): Promise<ActionResult> {
  const result = await query(db, (client: SupabaseClient) =>
    client.from("facts").update({ status: "fact" }).eq("id", factId).select("id"),
  );
  if (!result.ok) {
    return failed("Не получилось подтвердить запись", result);
  }
  if ((result.data ?? []).length === 0) {
    return {
      ok: false,
      message: "Запись не найдена — возможно, её уже удалили. Обновите список.",
    };
  }
  return { ok: true };
}

/** «Удалить»: обычный delete под RLS; строка удаляется, а не прячется. */
export async function removeFact(db: Db, factId: string): Promise<ActionResult> {
  const result = await query(db, (client: SupabaseClient) =>
    client.from("facts").delete().eq("id", factId).select("id"),
  );
  if (!result.ok) {
    return failed("Не получилось удалить запись", result);
  }
  if ((result.data ?? []).length === 0) {
    return {
      ok: false,
      message: "Запись не найдена — возможно, её уже удалили. Обновите список.",
    };
  }
  return { ok: true };
}
