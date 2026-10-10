/**
 * Контраст текста в обеих темах — не ниже 4,5:1 (`design.md` §1, приёмка
 * этапа 034). Значения берутся прямо из `styles.css`, из блоков
 * `:root[data-skin="bank"]` и `:root[data-skin="quiet"]`: второго списка
 * цветов, который мог бы разойтись с CSS, нет.
 *
 * Полупрозрачная подложка (метка, выбранный вариант) считается поверх
 * карточки, на которой стоит. Формула — WCAG 2.x: относительная яркость
 * sRGB и (L1 + 0,05) / (L2 + 0,05).
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "node:test";

type Rgb = [number, number, number];
type Rgba = [number, number, number, number];

const CSS = readFileSync(new URL("../styles.css", import.meta.url), "utf8");

/** Переменные темы: `--имя` → значение как написано. */
function tokens(skin: string): Map<string, string> {
  const start = CSS.indexOf(`:root[data-skin="${skin}"] {`);
  assert.ok(start >= 0, `нет блока темы ${skin}`);
  const body = CSS.slice(start, CSS.indexOf("\n}", start));
  const map = new Map<string, string>();
  for (const match of body.matchAll(/^\s*(--[a-z0-9-]+):\s*([^;]+);/gm)) {
    map.set(match[1]!, match[2]!.trim());
  }
  return map;
}

/** `#rrggbb`, `rgba(r, g, b, a)` или `var(--x)` той же темы → RGBA. */
function color(map: Map<string, string>, name: string): Rgba {
  const raw = map.get(name);
  assert.ok(raw !== undefined, `нет переменной ${name}`);
  const ref = /^var\((--[a-z0-9-]+)\)$/.exec(raw);
  if (ref) {
    return color(map, ref[1]!);
  }
  const hex = /^#([0-9a-f]{6})$/i.exec(raw);
  if (hex) {
    const n = Number.parseInt(hex[1]!, 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255, 1];
  }
  const rgba = /^rgba\((\d+),\s*(\d+),\s*(\d+),\s*([\d.]+)\)$/.exec(raw);
  assert.ok(rgba, `${name}: не цвет — ${raw}`);
  return [Number(rgba[1]), Number(rgba[2]), Number(rgba[3]), Number(rgba[4])];
}

/** Полупрозрачное поверх непрозрачного. */
function over(top: Rgba, base: Rgb): Rgb {
  const a = top[3];
  return [0, 1, 2].map((i) => top[i]! * a + base[i]! * (1 - a)) as Rgb;
}

function luminance([r, g, b]: Rgb): number {
  const lin = (c: number) => {
    const s = c / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b);
}

function ratio(a: Rgb, b: Rgb): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x) as [number, number];
  return (hi + 0.05) / (lo + 0.05);
}

/**
 * Фон: одна переменная (поверх фона экрана, если прозрачна) или подложка
 * поверх карточки: `["--c-high-w", "--surface"]`.
 */
type Ground = string | [string, string];

function ground(map: Map<string, string>, g: Ground): Rgb {
  const page = color(map, "--bg");
  if (typeof g === "string") {
    return over(color(map, g), over(page, [0, 0, 0]));
  }
  return over(color(map, g[0]), over(color(map, g[1]), over(page, [0, 0, 0])));
}

/** Текст → на чём он стоит. */
const PAIRS: [string, Ground][] = [
  ...["--ink", "--ink-2", "--ink-3"].flatMap((ink): [string, Ground][] => [
    [ink, "--bg"],
    [ink, "--surface"],
    [ink, "--surface-2"],
    [ink, "--btn-bg"],
    [ink, "--input-bg"],
    [ink, "--tabs-bg"],
  ]),
  ["--acc", "--bg"],
  ["--acc", "--surface"],
  ["--acc", ["--acc-w", "--surface"]],
  ["--tab-on", "--tabs-bg"],
  ["--acc-ink", "--acc"],
  ["--num-acc", "--bg"],
  ["--now", "--bg"],
  ["--ok", "--bg"],
  ["--ok", "--surface"],
  ["--c-late", "--bg"],
  ["--c-late", "--surface"],
  ["--c-late", "--btn-bg"],
  ["--c-late", ["--c-late-w", "--surface"]],
  ["--c-high", ["--c-high-w", "--surface"]],
  ["--c-guess", ["--c-guess-w", "--surface"]],
  ["--c-guess", ["--c-guess-w", "--bg"]],
  ["--c-mine", ["--c-mine-w", "--surface"]],
  ["--ink-2", ["--c-kind-w", "--surface"]],
  ["--ink-2", "--seg-bg"],
  ["--seg-on-ink", "--seg-on"],
];

/** Монограмма сферы — текст только в «Банк-люксе», в «Тихой роскоши» отметка без букв. */
const SPHERES = [...Array.from({ length: 12 }, (_, i) => `--s-${i + 1}`), "--s-none"];

for (const skin of ["bank", "quiet"]) {
  describe(`контраст: ${skin}`, () => {
    const map = tokens(skin);
    for (const [text, bg] of PAIRS) {
      const label = typeof bg === "string" ? bg : bg.join(" над ");
      it(`${text} на ${label}`, () => {
        const value = ratio(over(color(map, text), ground(map, bg)), ground(map, bg));
        assert.ok(value >= 4.5, `${text} на ${label}: ${value.toFixed(2)}:1`);
      });
    }
  });
}

describe("контраст: монограммы сфер в «Банк-люксе»", () => {
  const map = tokens("bank");
  for (const sphere of SPHERES) {
    it(`--mark-ink на ${sphere}`, () => {
      const value = ratio(over(color(map, "--mark-ink"), [0, 0, 0]), ground(map, sphere));
      assert.ok(value >= 4.5, `${sphere}: ${value.toFixed(2)}:1`);
    });
  }
});

describe("главная кнопка «Банк-люкса» — градиент: текст на каждом его цвете", () => {
  const map = tokens("bank");
  const stops = [...(map.get("--btn-main") ?? "").matchAll(/#[0-9a-f]{6}/gi)].map((m) => m[0]);
  it("три цвета в градиенте", () => assert.equal(stops.length, 3));
  for (const stop of stops) {
    it(`--acc-ink на ${stop}`, () => {
      const n = Number.parseInt(stop.slice(1), 16);
      const bg: Rgb = [(n >> 16) & 255, (n >> 8) & 255, n & 255];
      const value = ratio(over(color(map, "--acc-ink"), bg), bg);
      assert.ok(value >= 4.5, `${stop}: ${value.toFixed(2)}:1`);
    });
  }
});
