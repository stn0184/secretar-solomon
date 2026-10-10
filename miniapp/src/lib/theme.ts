/**
 * Оформление: две темы на одной разметке (`design.md` §1,
 * `techspec/32-redesign.md` §32.1).
 *
 * Выбор — «Как в Telegram» (`auto`, по умолчанию), «Тихая роскошь»
 * (`quiet`) или «Банк-люкс» (`bank`). При `auto` тёмная схема Telegram
 * даёт «Банк-люкс», светлая — «Тихую роскошь», и тема меняется вместе со
 * схемой. Выбор хранится в базе (`owner_settings.theme`), чтобы открываться
 * так же на любом устройстве; копия в `localStorage` нужна только первому
 * кадру — правдой она не считается, при старте её поправит база.
 *
 * Тема на экране — атрибут `data-skin` на `<html>`: по нему CSS берёт
 * значения переменных. Шапка и фон Telegram красятся в фон темы.
 *
 * Правила выбора — чистые функции, проверяются на Node; запись — через
 * `query`, отказ — текстом (`supabase.ts`).
 */

import type { SupabaseClient } from "@supabase/supabase-js";

import { oneOf, recordOf } from "./parse.ts";
import { type Db, failed, query } from "./supabase.ts";
import { type ColorScheme, setChromeColor } from "./telegram.ts";

export type ThemeChoice = "auto" | "bank" | "quiet";
export type Skin = "bank" | "quiet";

/** Варианты в «Оформлении» — в порядке артборда 06. */
export const THEME_CHOICES: readonly ThemeChoice[] = ["auto", "quiet", "bank"];

export const SKIN_NAME: Record<Skin, string> = {
  bank: "Банк-люкс",
  quiet: "Тихая роскошь",
};

/** Фон темы — он же цвет шапки Telegram (`--bg` в `styles.css`). */
export const SKIN_BG: Record<Skin, string> = {
  bank: "#050505",
  quiet: "#F6F3EE",
};

/** Выбор из базы или копии; незнакомое — «Как в Telegram». */
export function parseThemeChoice(value: unknown): ThemeChoice {
  return oneOf<ThemeChoice>(value, THEME_CHOICES, "auto");
}

/** Какая тема на экране: закреплённая — она, «Как в Telegram» — по схеме. */
export function skinOf(choice: ThemeChoice, scheme: ColorScheme): Skin {
  if (choice !== "auto") {
    return choice;
  }
  return scheme === "dark" ? "bank" : "quiet";
}

/** Подпись у «Как в Telegram»: что выбрано прямо сейчас. */
export function autoNote(scheme: ColorScheme): string {
  const words = scheme === "dark" ? "тёмная" : "светлая";
  return `Сейчас в Telegram ${words} тема — ${SKIN_NAME[skinOf("auto", scheme)]}`;
}

/* ------------------------------------------------------------ экран */

/** Тема на экране: атрибут для CSS, схема для полей ввода, цвет шапки Telegram. */
export function applySkin(skin: Skin): void {
  const root = document.documentElement;
  root.dataset.skin = skin;
  root.style.colorScheme = skin === "bank" ? "dark" : "light";
  setChromeColor(SKIN_BG[skin]);
}

const CACHE_KEY = "solomon.theme";

/** Последний выбор на этом устройстве — для первого кадра. Хранилища нет — «Как в Telegram». */
export function readCachedChoice(): ThemeChoice {
  try {
    return parseThemeChoice(window.localStorage.getItem(CACHE_KEY));
  } catch {
    return "auto";
  }
}

/** Запомнить выбор на устройстве; хранилище закрыто — не беда, правда в базе. */
export function cacheChoice(choice: ThemeChoice): void {
  try {
    window.localStorage.setItem(CACHE_KEY, choice);
  } catch {
    // Хранилища нет (закрыто в настройках браузера) — первый кадр будет по Telegram.
  }
}

/* ------------------------------------------------------------ база */

export type ThemeResult = { ok: true; choice: ThemeChoice } | { ok: false; message: string };

/** Выбор владельца из его строки настроек; строки нет — «Как в Telegram». */
export async function loadTheme(db: Db): Promise<ThemeResult> {
  const result = await query(db, (client: SupabaseClient) =>
    client.from("owner_settings").select("theme").limit(1),
  );
  if (!result.ok) {
    return failed("Не получилось прочитать оформление", result);
  }
  const rows: unknown[] = Array.isArray(result.data) ? result.data : [];
  return { ok: true, choice: parseThemeChoice(recordOf(rows[0])?.theme) };
}

/**
 * Сохранить выбор: `save_theme` под токеном (`techspec/03-schema.md` §3.8).
 * Сохранённым считается то, что вернула база (инвариант 4); пустой ответ —
 * строки настроек ещё нет, бот её не записал.
 */
export async function saveTheme(db: Db, choice: ThemeChoice): Promise<ThemeResult> {
  const result = await query(db, (client: SupabaseClient) =>
    client.rpc("save_theme", { theme: choice }),
  );
  if (!result.ok) {
    return failed("Не получилось сохранить оформление", result);
  }
  // Функция возвращает строку таблицы; пустая приходит объектом из null.
  const row = recordOf(Array.isArray(result.data) ? result.data[0] : result.data);
  if (!row || typeof row.theme !== "string") {
    return {
      ok: false,
      message:
        "Не получилось сохранить оформление: настроек ещё нет. Напишите боту и выберите снова.",
    };
  }
  return { ok: true, choice: parseThemeChoice(row.theme) };
}
