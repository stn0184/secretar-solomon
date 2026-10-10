/**
 * Сферы в приложении (`techspec/32-redesign.md` §32.2, `design.md` §1):
 * живые сферы владельца по порядку заведения, место — цвет из палитры
 * темы, монограмма — отметка в строке дела «Банк-люкса».
 *
 * Сферы и люди выдуманные: VoiceFin, РЕЙВА, Игорь, Олег.
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import type { SupabaseClient } from "@supabase/supabase-js";

import type { Session } from "./session.ts";
import {
  type Sphere,
  loadSpheres,
  monogram,
  parseSpheres,
  sphereColor,
  sphereOf,
  sphereName,
} from "./spheres.ts";
import type { Db } from "./supabase.ts";

describe("parseSpheres", () => {
  it("по порядку заведения, место — с единицы", () => {
    const spheres = parseSpheres([
      { id: "b", name: "РЕЙВА", created_at: "2026-10-01T10:01:00Z" },
      { id: "a", name: "VoiceFin", created_at: "2026-10-01T10:00:00Z" },
      { id: "c", name: "Семья", created_at: "2026-10-02T08:00:00Z" },
    ]);
    assert.deepEqual(spheres, [
      { id: "a", name: "VoiceFin", place: 1 },
      { id: "b", name: "РЕЙВА", place: 2 },
      { id: "c", name: "Семья", place: 3 },
    ]);
  });

  it("одновременно заведённые — по id, чтобы цвет не прыгал между чтениями", () => {
    const at = "2026-10-01T10:00:00Z";
    const spheres = parseSpheres([
      { id: "z", name: "Спорт", created_at: at },
      { id: "m", name: "Семья", created_at: at },
    ]);
    assert.deepEqual(
      spheres.map((s) => s.name),
      ["Семья", "Спорт"],
    );
  });

  it("строка без id или названия пропускается, место не занимает", () => {
    const spheres = parseSpheres([
      { id: "a", name: "VoiceFin", created_at: "2026-10-01T10:00:00Z" },
      { id: 5, name: "битая", created_at: "2026-10-01T10:01:00Z" },
      null,
      { id: "c", name: "Семья", created_at: "2026-10-01T10:02:00Z" },
    ]);
    assert.deepEqual(
      spheres.map((s) => [s.name, s.place]),
      [
        ["VoiceFin", 1],
        ["Семья", 2],
      ],
    );
  });
});

describe("monogram", () => {
  it("заглавная внутри слова — первая и она", () => {
    assert.equal(monogram("VoiceFin"), "VF");
  });

  it("два слова — первые буквы", () => {
    assert.equal(monogram("Дача у озера"), "ДУ");
    assert.equal(monogram("новый бизнес"), "НБ");
  });

  it("одно слово — первая и следующая согласная, кроме й, ь, ъ", () => {
    assert.equal(monogram("РЕЙВА"), "РВ");
    assert.equal(monogram("Семья"), "СМ");
    assert.equal(monogram("Спорт"), "СП");
    assert.equal(monogram("Эйфория"), "ЭФ");
    assert.equal(monogram("Мьюзик"), "МЗ");
    assert.equal(monogram("работа"), "РБ");
  });

  it("согласной нет — вторая буква; одна буква — одна", () => {
    assert.equal(monogram("Ая"), "АЯ");
    assert.equal(monogram("Я"), "Я");
  });

  it("цифры и знаки по краям не мешают", () => {
    assert.equal(monogram("«Альфа» 2"), "А2");
    assert.equal(monogram("2026"), "20");
  });
});

describe("цвет и название", () => {
  const spheres: Sphere[] = [
    { id: "a", name: "VoiceFin", place: 1 },
    { id: "b", name: "РЕЙВА", place: 2 },
  ];

  it("цвет — переменная места палитры темы; без сферы — своя", () => {
    assert.equal(sphereColor(spheres[1]!), "var(--s-2)");
    assert.equal(sphereColor(null), "var(--s-none)");
  });

  it("за двенадцатым местом палитра идёт по кругу", () => {
    assert.equal(sphereColor({ id: "x", name: "X", place: 13 }), "var(--s-1)");
    assert.equal(sphereColor({ id: "x", name: "X", place: 12 }), "var(--s-12)");
  });

  it("сфера дела — по id; убранной или незнакомой нет — дело без сферы", () => {
    assert.equal(sphereOf("b", spheres)?.name, "РЕЙВА");
    assert.equal(sphereOf("gone", spheres), null);
    assert.equal(sphereOf(null, spheres), null);
    assert.equal(sphereName(null), "Без сферы");
    assert.equal(sphereName(spheres[0]!), "VoiceFin");
  });
});

/* ------------------------------------------------ база на подменённом клиенте */

type Response = { data: unknown; error: { message: string; code?: string } | null; status?: number };

/** Цепочка PostgREST: любой метод возвращает её же, `await` — ответ. */
function fakeDb(response: Response): { db: Db; calls: unknown[][] } {
  const calls: unknown[][] = [];
  const chain: unknown = new Proxy(() => {}, {
    get(_target, prop) {
      if (prop === "then") {
        return (resolve: (value: Response) => unknown) => Promise.resolve(response).then(resolve);
      }
      return (...args: unknown[]) => {
        calls.push([prop, ...args]);
        return chain;
      };
    },
  });
  const client = { from: (table: string) => (calls.push(["from", table]), chain) } as unknown as SupabaseClient;
  const session = {
    env: { url: "https://example.supabase.co", anonKey: "anon" },
    token: () => "token",
    expired: () => false,
    refresh: () => Promise.resolve({ ok: false, message: "не нужен" }),
  } as unknown as Session;
  return { db: { client, session }, calls };
}

describe("loadSpheres", () => {
  it("только живые, по порядку заведения", async () => {
    const { db, calls } = fakeDb({
      data: [{ id: "a", name: "VoiceFin", created_at: "2026-10-01T10:00:00Z" }],
      error: null,
      status: 200,
    });
    assert.deepEqual(await loadSpheres(db), {
      ok: true,
      spheres: [{ id: "a", name: "VoiceFin", place: 1 }],
    });
    assert.deepEqual(calls, [
      ["from", "spheres"],
      ["select", "id, name, created_at"],
      ["is", "removed_at", null],
      ["order", "created_at", { ascending: true }],
      ["order", "id", { ascending: true }],
    ]);
  });

  it("отказ базы — текстом", async () => {
    const { db } = fakeDb({ data: null, error: { message: "boom" }, status: 500 });
    assert.deepEqual(await loadSpheres(db), {
      ok: false,
      message: "Не получилось прочитать сферы. База ответила отказом. Попробуйте ещё раз.",
    });
  });
});
