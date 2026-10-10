/**
 * Выбор оформления (`techspec/32-redesign.md` §32.1): чистые правила и
 * запись в базу на подменённом клиенте.
 *
 * «Как в Telegram» — тёмная тема Telegram даёт «Банк-люкс», светлая —
 * «Тихую роскошь»; закреплённая тема от Telegram не зависит.
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import type { SupabaseClient } from "@supabase/supabase-js";

import type { Session } from "./session.ts";
import type { Db } from "./supabase.ts";
import {
  SKIN_BG,
  THEME_CHOICES,
  autoNote,
  loadTheme,
  parseThemeChoice,
  saveTheme,
  skinOf,
} from "./theme.ts";

describe("skinOf", () => {
  it("как в Telegram: тёмная — Банк-люкс, светлая — Тихая роскошь", () => {
    assert.equal(skinOf("auto", "dark"), "bank");
    assert.equal(skinOf("auto", "light"), "quiet");
  });

  it("закреплённая тема от Telegram не зависит", () => {
    assert.equal(skinOf("bank", "light"), "bank");
    assert.equal(skinOf("quiet", "dark"), "quiet");
  });
});

describe("parseThemeChoice", () => {
  it("знакомые значения — как есть", () => {
    for (const choice of THEME_CHOICES) {
      assert.equal(parseThemeChoice(choice), choice);
    }
  });

  it("незнакомое, пустое и не строка — как в Telegram", () => {
    assert.equal(parseThemeChoice("glass"), "auto");
    assert.equal(parseThemeChoice(""), "auto");
    assert.equal(parseThemeChoice(null), "auto");
    assert.equal(parseThemeChoice(3), "auto");
  });
});

describe("варианты и подписи", () => {
  it("порядок на экране — как на артборде 06", () => {
    assert.deepEqual(THEME_CHOICES, ["auto", "quiet", "bank"]);
  });

  it("цвет шапки Telegram — фон темы", () => {
    assert.deepEqual(SKIN_BG, { bank: "#050505", quiet: "#F6F3EE" });
  });

  it("подпись «Как в Telegram» называет, что сейчас", () => {
    assert.equal(autoNote("dark"), "Сейчас в Telegram тёмная тема — Банк-люкс");
    assert.equal(autoNote("light"), "Сейчас в Telegram светлая тема — Тихая роскошь");
  });
});

/* ------------------------------------------------ база на подменённом клиенте */

interface Call {
  kind: "rpc" | "select";
  name: string;
  params?: unknown;
}

type Response = { data: unknown; error: { message: string; code?: string } | null; status?: number };

function fakeDb(response: Response): { db: Db; calls: Call[] } {
  const calls: Call[] = [];
  const client = {
    rpc(name: string, params: unknown) {
      calls.push({ kind: "rpc", name, params });
      return Promise.resolve(response);
    },
    from(table: string) {
      const builder = {
        select(columns: string) {
          calls.push({ kind: "select", name: table, params: columns });
          return builder;
        },
        limit() {
          return Promise.resolve(response);
        },
      };
      return builder;
    },
  } as unknown as SupabaseClient;
  const session = {
    env: { url: "https://example.supabase.co", anonKey: "anon" },
    token: () => "token",
    expired: () => false,
    refresh: () => Promise.resolve({ ok: false, message: "не нужен" }),
  } as unknown as Session;
  return { db: { client, session }, calls };
}

describe("loadTheme", () => {
  it("читает тему своей строки настроек", async () => {
    const { db, calls } = fakeDb({ data: [{ theme: "bank" }], error: null, status: 200 });
    assert.deepEqual(await loadTheme(db), { ok: true, choice: "bank" });
    assert.deepEqual(calls, [{ kind: "select", name: "owner_settings", params: "theme" }]);
  });

  it("строки нет — как в Telegram", async () => {
    const { db } = fakeDb({ data: [], error: null, status: 200 });
    assert.deepEqual(await loadTheme(db), { ok: true, choice: "auto" });
  });

  it("отказ базы — текстом", async () => {
    const { db } = fakeDb({ data: null, error: { message: "boom" }, status: 500 });
    assert.deepEqual(await loadTheme(db), {
      ok: false,
      message: "Не получилось прочитать оформление. База ответила отказом. Попробуйте ещё раз.",
    });
  });
});

describe("saveTheme", () => {
  it("зовёт save_theme и верит ответу базы", async () => {
    const row = { owner_telegram_id: 777, timezone: "Asia/Yekaterinburg", theme: "quiet" };
    const { db, calls } = fakeDb({ data: row, error: null, status: 200 });
    assert.deepEqual(await saveTheme(db, "quiet"), { ok: true, choice: "quiet" });
    assert.deepEqual(calls, [{ kind: "rpc", name: "save_theme", params: { theme: "quiet" } }]);
  });

  it("пустой ответ — строки настроек нет, сохранённым не считается", async () => {
    const empty = { owner_telegram_id: null, timezone: null, theme: null };
    const { db } = fakeDb({ data: empty, error: null, status: 200 });
    assert.deepEqual(await saveTheme(db, "bank"), {
      ok: false,
      message:
        "Не получилось сохранить оформление: настроек ещё нет. Напишите боту и выберите снова.",
    });
  });

  it("сеть упала — текстом", async () => {
    const { db } = fakeDb({ data: null, error: { message: "fetch failed" }, status: 0 });
    assert.deepEqual(await saveTheme(db, "bank"), {
      ok: false,
      message: "Не получилось сохранить оформление. Проверьте связь и обновите.",
    });
  });
});
