#!/usr/bin/env node
/**
 * Самопроверка канала передачи переписки от Partner Assistant
 * (`techspec/28-relay.md` §28.2): тот же вызов, что делает Partner Assistant, —
 * `POST /rest/v1/rpc/relay_chat_events` по HTTPS с публичным anon-ключом и
 * ключом передачи в теле.
 *
 *   node scripts/relay-check.mjs
 *
 * Значения — из `.env` в корне: `VITE_SUPABASE_URL`, `VITE_SUPABASE_ANON_KEY`
 * и `CHAT_RELAY_KEY`. Два вызова, оба с пустой пачкой событий — в базу не
 * пишется ничего:
 *
 *   1. ключ из `.env` — ждём 200 и `[]`: функция на месте, anon её зовёт,
 *      хэш ключа записан ботом при запуске и не отозван;
 *   2. заведомо чужой ключ — ждём 403: функция отказывает чужому.
 *
 * Значения ключей не печатаются никогда. Код возврата: 0 — канал работает,
 * 1 — нет (причина в выводе), 2 — не хватает переменной в `.env`.
 */
import { randomBytes } from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { parseEnv } from "node:util";

export const RPC_PATH = "/rest/v1/rpc/relay_chat_events";
const LOCAL_HOSTS = new Set(["127.0.0.1", "localhost", "[::1]"]);

/**
 * Адрес вызова; не HTTPS — `null`: ключ передачи ушёл бы открытым текстом.
 * Локальный адрес (тесты) — исключение.
 *
 * @param {string} url адрес проекта Supabase
 * @returns {string | null}
 */
export function rpcUrl(url) {
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    return null;
  }
  const local = parsed.protocol === "http:" && LOCAL_HOSTS.has(parsed.hostname);
  if (parsed.protocol !== "https:" && !local) return null;
  return `${parsed.origin}${RPC_PATH}`;
}

/**
 * Один вызов функции приёма с пустой пачкой.
 *
 * @param {typeof globalThis.fetch} fetch
 * @param {string} target
 * @param {string} anonKey
 * @param {string} relayKey
 * @returns {Promise<{ status: number, body: unknown }>}
 */
async function call(fetch, target, anonKey, relayKey) {
  const response = await fetch(target, {
    method: "POST",
    headers: {
      apikey: anonKey,
      authorization: `Bearer ${anonKey}`,
      "content-type": "application/json",
    },
    body: JSON.stringify({ relay_key: relayKey, events: [] }),
  });
  let body = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  return { status: response.status, body };
}

/** Что значит отказ на первом вызове — словами для человека. */
function refusal(status) {
  if (status === 403) {
    return (
      "Ключ передачи не принят (403): в базе нет его хэша или он отозван. Бот записывает " +
      "хэш при запуске — выложите бота с ключом: node scripts/deploy-bot.mjs --env."
    );
  }
  if (status === 404) {
    return "Функции relay_chat_events нет (404): миграция этапа 028 не применена (supabase db push).";
  }
  if (status === 401) {
    return "Anon-ключ не подошёл (401): проверьте VITE_SUPABASE_ANON_KEY.";
  }
  return `База ответила ${status} — канал не работает.`;
}

/**
 * Проверка канала: ключ принят, чужой ключ — отказ. Ничего не пишет.
 *
 * @param {{ url: string, anonKey: string, relayKey: string, fetch?: typeof globalThis.fetch }} options
 * @returns {Promise<{ ok: boolean, lines: string[] }>}
 */
export async function checkRelay({ url, anonKey, relayKey, fetch = globalThis.fetch }) {
  const target = rpcUrl(url);
  if (target === null) {
    return {
      ok: false,
      lines: ["Адрес базы не HTTPS: ключ передачи ушёл бы открытым текстом. Проверьте VITE_SUPABASE_URL."],
    };
  }
  try {
    const own = await call(fetch, target, anonKey, relayKey);
    if (own.status !== 200) return { ok: false, lines: [refusal(own.status)] };
    if (!Array.isArray(own.body) || own.body.length !== 0) {
      return { ok: false, lines: ["Функция ответила не пустым списком итогов — это не relay_chat_events этапа 028."] };
    }
    const stranger = await call(fetch, target, anonKey, randomBytes(32).toString("hex"));
    if (stranger.status !== 403) {
      return {
        ok: false,
        lines: [`Чужой ключ получил ${stranger.status} вместо 403: функция принимает кого угодно — канал закрыть.`],
      };
    }
  } catch (error) {
    return { ok: false, lines: [`База не отвечает: ${error instanceof Error ? error.name : "сбой сети"}.`] };
  }
  return {
    ok: true,
    lines: ["Канал работает: ключ передачи принят, чужой ключ отвергнут (403). В базу ничего не записано."],
  };
}

const VARIABLES = ["VITE_SUPABASE_URL", "VITE_SUPABASE_ANON_KEY", "CHAT_RELAY_KEY"];
const BOM = String.fromCharCode(0xfeff);

/**
 * Точка входа без побочных эффектов: `.env`, сеть и вывод — в deps.
 *
 * @param {{ readEnv: () => string | null, fetch: typeof globalThis.fetch, log: (line: string) => void }} deps
 * @returns {Promise<number>}
 */
export async function main(deps) {
  const text = deps.readEnv();
  if (text === null) {
    deps.log("Нет файла .env в корне репозитория — значениям взяться неоткуда.");
    return 2;
  }
  // Блокнот пишет BOM в начало файла — он не часть имени первой переменной.
  const env = parseEnv(text.startsWith(BOM) ? text.slice(BOM.length) : text);
  const missing = VARIABLES.filter((name) => !(env[name] ?? "").trim());
  if (missing.length) {
    deps.log(`Не хватает переменных в .env: ${missing.join(", ")}.`);
    return 2;
  }
  const report = await checkRelay({
    url: env.VITE_SUPABASE_URL.trim(),
    anonKey: env.VITE_SUPABASE_ANON_KEY.trim(),
    relayKey: env.CHAT_RELAY_KEY.trim(),
    fetch: deps.fetch,
  });
  for (const line of report.lines) deps.log(line);
  return report.ok ? 0 : 1;
}

if (import.meta.main) {
  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
  process.exitCode = await main({
    readEnv: () => {
      try {
        return fs.readFileSync(path.join(root, ".env"), "utf8");
      } catch (error) {
        if (error.code === "ENOENT") return null;
        throw error;
      }
    },
    fetch: globalThis.fetch,
    log: (line) => console.log(line),
  });
}
