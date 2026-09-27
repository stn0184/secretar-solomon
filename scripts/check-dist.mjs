#!/usr/bin/env node
/**
 * Сборка Mini App не должна нести секретов — инвариант 1 (CLAUDE.md).
 *
 * В `dist/` разрешены ровно две вшитые переменные: адрес проекта Supabase и
 * анонимный ключ. Всё остальное — ключ service-role, токен бота, ключ
 * Claude — попасть туда не может, и если попало, публиковать нельзя.
 *
 * Проверяется текстом по всем файлам сборки, включая карты кода:
 *   - префикс ключей Claude `sk-ant-` и токен бота по форме
 *     `<8–10 цифр>:<35 знаков>`;
 *   - каждый JWT (`eyJ….eyJ….`) разбирается, и клейм `role` сверяется:
 *     анонимный ключ несёт `anon`, ключ service-role — `service_role`,
 *     а по одному внешнему виду они неотличимы;
 *   - слово `service_role` — только в коде и разметке: в картах кода
 *     лежит исходник supabase-js, а его доккомментарии это слово
 *     упоминают. Сам ключ — JWT, и его карта не спрячет.
 *
 * Запуск: node scripts/check-dist.mjs [папка]   (по умолчанию miniapp/dist)
 * Код возврата 1 — находка есть; зовётся из workflow до deploy и из ворот.
 */
import fs from "node:fs";
import path from "node:path";

const dir = process.argv[2] ?? "miniapp/dist";

const PLAIN = [
  { name: "ключ service-role", re: /service_role/g, skipMaps: true },
  { name: "ключ Claude", re: /sk-ant-/g },
  { name: "токен бота", re: /\b\d{8,10}:[A-Za-z0-9_-]{35}\b/g },
];

const JWT = /eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}/g;

/** Все файлы папки, рекурсивно. */
function walk(root) {
  const out = [];
  for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
    const full = path.join(root, entry.name);
    if (entry.isDirectory()) out.push(...walk(full));
    else out.push(full);
  }
  return out;
}

/** Клейм `role` из JWT; не разобрался — пусто. */
function roleOf(token) {
  try {
    const payload = token.split(".")[1];
    const json = Buffer.from(payload, "base64url").toString("utf8");
    const claims = JSON.parse(json);
    return typeof claims.role === "string" ? claims.role : "";
  } catch {
    return "";
  }
}

if (!fs.existsSync(dir)) {
  console.error(`check-dist: папки ${dir} нет — сначала сборка`);
  process.exit(1);
}

const findings = [];
for (const file of walk(dir)) {
  const text = fs.readFileSync(file, "utf8");
  const isMap = file.endsWith(".map");
  for (const { name, re, skipMaps } of PLAIN) {
    if (skipMaps && isMap) continue;
    if (re.test(text)) findings.push(`${file}: ${name}`);
    re.lastIndex = 0;
  }
  for (const token of text.match(JWT) ?? []) {
    const role = roleOf(token);
    if (role && role !== "anon") findings.push(`${file}: JWT с ролью ${role}`);
  }
}

if (findings.length) {
  console.error("check-dist: в сборке найдены секреты — публиковать нельзя");
  for (const f of findings) console.error(`  ${f}`);
  process.exit(1);
}
console.log(`check-dist: ${dir} чист`);
