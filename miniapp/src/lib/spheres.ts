/**
 * Сферы владельца в приложении (`techspec/30-spheres.md`, `techspec/32-redesign.md`
 * §32.2): дела показываются со сферой, «Дела» группируются по сферам, сферу
 * дела можно сменить в карточке.
 *
 * Читаются только живые сферы (`removed_at is null`), по порядку заведения.
 * Место сферы в этом порядке — её цвет из палитры темы (`design.md` §1:
 * владелец цвет не выбирает); убранная сфера место освобождает. Фильтра по
 * владельцу здесь нет намеренно, как у задач: строки режет правило доступа
 * по клейму токена (`techspec/04-access.md` §4.2).
 *
 * Разбор строк, место, цвет и монограмма — чистые функции, проверяются на Node.
 */

import type { SupabaseClient } from "@supabase/supabase-js";

import { plural } from "./format.ts";
import { recordOf } from "./parse.ts";
import { type Db, failed, query } from "./supabase.ts";

export interface Sphere {
  id: string;
  name: string;
  /** Место по порядку заведения среди живых сфер, с единицы. */
  place: number;
}

export type SpheresResult = { ok: true; spheres: Sphere[] } | { ok: false; message: string };

/** Мест в палитре темы (`--s-1` … `--s-12`); сфер больше не бывает (§30.1). */
const PALETTE = 12;

/**
 * Строки `spheres` → сферы с местами. Порядок — по `created_at`, при
 * равенстве — по `id`: цвет сферы не прыгает между чтениями. Строка без id
 * или названия пропускается и места не занимает.
 */
export function parseSpheres(rows: readonly unknown[]): Sphere[] {
  const valid = rows.flatMap((row) => {
    const r = recordOf(row);
    if (!r || typeof r.id !== "string" || typeof r.name !== "string") {
      return [];
    }
    return [{ id: r.id, name: r.name, createdAt: typeof r.created_at === "string" ? r.created_at : "" }];
  });
  valid.sort((a, b) =>
    a.createdAt === b.createdAt ? a.id.localeCompare(b.id) : a.createdAt < b.createdAt ? -1 : 1,
  );
  return valid.map((s, index) => ({ id: s.id, name: s.name, place: index + 1 }));
}

/** Цвет сферы — переменная её места в палитре темы; без сферы — своя. */
export function sphereColor(sphere: Sphere | null): string {
  if (!sphere) {
    return "var(--s-none)";
  }
  return `var(--s-${((sphere.place - 1) % PALETTE) + 1})`;
}

/** Сфера дела по id; убранной или незнакомой нет — дело показывается без сферы. */
export function sphereOf(id: string | null, spheres: readonly Sphere[]): Sphere | null {
  return id === null ? null : (spheres.find((s) => s.id === id) ?? null);
}

/** Название для строки и карточки. */
export function sphereName(sphere: Sphere | null): string {
  return sphere ? sphere.name : "Без сферы";
}

/** Надзаголовок «Сфер»: «4 из 12» — сколько заведено из возможных (§30.1). */
export function spheresEyebrow(count: number): string {
  return `${count} из ${PALETTE}`;
}

/**
 * Подпись карточки «Без сферы и о себе»: «1 дело · 6 записей о вас». Записи
 * ещё не прочитаны (`null`) — только дела; пустая часть не называется.
 */
export function unspheredMeta(tasks: number, facts: number | null): string {
  const parts = [
    tasks > 0 ? `${tasks} ${plural(tasks, "дело", "дела", "дел")}` : "",
    facts ? `${facts} ${plural(facts, "запись", "записи", "записей")} о вас` : "",
  ];
  return parts.filter(Boolean).join(" · ");
}

const CONSONANT = /[бвгджзклмнпрстфхцчшщbcdfghjklmnpqrstvwxz]/i;

/**
 * Монограмма сферы — отметка в строке дела «Банк-люкса» (`design.md` §3),
 * два знака заглавными: два слова — их первые буквы («Дача у озера» → «ДУ»);
 * заглавная внутри слова — первая и она («VoiceFin» → «VF»); иначе первая и
 * следующая согласная, кроме й, ь, ъ («РЕЙВА» → «РВ», «Семья» → «СМ»);
 * согласной нет — вторая буква. Одна буква — она одна.
 */
export function monogram(name: string): string {
  const words = name.match(/[\p{L}\p{N}]+/gu) ?? [];
  const first = words[0];
  if (!first) {
    return "·";
  }
  const chars = [...first];
  const second = words[1];
  if (second) {
    return `${chars[0]}${[...second][0]}`.toUpperCase();
  }
  if (chars.length === 1) {
    return chars[0]!.toUpperCase();
  }
  const inner = chars.findIndex(
    (ch, i) => i > 0 && isUpper(ch) && isLower(chars[i - 1]!),
  );
  if (inner > 0) {
    return `${chars[0]}${chars[inner]}`.toUpperCase();
  }
  const consonant = chars.slice(1).find((ch) => CONSONANT.test(ch));
  return `${chars[0]}${consonant ?? chars[1]}`.toUpperCase();
}

function isUpper(ch: string): boolean {
  return ch !== ch.toLowerCase() && ch === ch.toUpperCase();
}

function isLower(ch: string): boolean {
  return ch !== ch.toUpperCase() && ch === ch.toLowerCase();
}

/* ------------------------------------------------------------------ база */

/** Живые сферы владельца по порядку заведения (`techspec/03-schema.md` §3.17). */
export async function loadSpheres(db: Db): Promise<SpheresResult> {
  const result = await query(db, (client: SupabaseClient) =>
    client
      .from("spheres")
      .select("id, name, created_at")
      .is("removed_at", null)
      .order("created_at", { ascending: true })
      .order("id", { ascending: true }),
  );
  if (!result.ok) {
    return failed("Не получилось прочитать сферы", result);
  }
  return { ok: true, spheres: parseSpheres(Array.isArray(result.data) ? result.data : []) };
}
