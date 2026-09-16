/**
 * Токен для правил доступа: подпись HS256 и постоянный uuid владельца.
 *
 * Подпись сверяется независимой реализацией (node:crypto), а uuid — вектором,
 * посчитанным другой библиотекой (uuid5 из стандартной библиотеки Python).
 */
import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { test } from "node:test";

import { buildAccessToken, signHs256, uuidForTelegramId } from "./token.ts";

const SECRET = "jwt-secret-for-tests";
const NOW = new Date("2026-09-17T12:00:00Z");

function decodeSegment(segment: string): Record<string, unknown> {
  return JSON.parse(Buffer.from(segment, "base64url").toString("utf8"));
}

test("подпись HS256 совпадает с независимой реализацией", async () => {
  const token = await signHs256({ hello: "мир" }, SECRET);
  const [header, payload, signature] = token.split(".");

  const expected = createHmac("sha256", SECRET)
    .update(`${header}.${payload}`)
    .digest("base64url");

  assert.equal(signature, expected);
  assert.deepEqual(decodeSegment(header!), { alg: "HS256", typ: "JWT" });
  assert.deepEqual(decodeSegment(payload!), { hello: "мир" });
});

test("uuid владельца постоянен и совпадает с uuid5 из другой библиотеки", async () => {
  assert.equal(await uuidForTelegramId(777), "995d4f08-b82d-5a08-a469-116b1080d53b");
  assert.equal(await uuidForTelegramId(777), await uuidForTelegramId(777));
  assert.notEqual(await uuidForTelegramId(777), await uuidForTelegramId(778));
});

test("токен несёт роль, срок и Telegram-id", async () => {
  const { token, expiresIn } = await buildAccessToken(777, SECRET, { now: NOW, ttlSeconds: 3600 });
  const payload = decodeSegment(token.split(".")[1]!);

  assert.equal(payload.role, "authenticated");
  assert.equal(payload.aud, "authenticated");
  assert.equal(payload.sub, await uuidForTelegramId(777));
  assert.equal(payload.telegram_id, 777);
  assert.equal(payload.iat, Math.floor(NOW.getTime() / 1000));
  assert.equal(payload.exp, Math.floor(NOW.getTime() / 1000) + 3600);
  assert.equal(expiresIn, 3600);
});
