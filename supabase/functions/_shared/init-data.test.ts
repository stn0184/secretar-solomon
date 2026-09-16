/**
 * Проверка подписи initData Telegram.
 *
 * Подпись в тесте считается независимой реализацией — node:crypto, — поэтому
 * тест ловит ошибку в разборе, а не повторяет её.
 */
import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { test } from "node:test";

import { InitDataError, verifyInitData } from "./init-data.ts";

const BOT_TOKEN = "123456789:test-token";
const NOW = new Date("2026-09-17T12:00:00Z");
const USER = JSON.stringify({ id: 777, first_name: "Тим" });

function initDataFor(
  fields: Record<string, string>,
  token: string = BOT_TOKEN,
): string {
  const checkString = Object.keys(fields)
    .sort()
    .map((key) => `${key}=${fields[key]}`)
    .join("\n");
  const secret = createHmac("sha256", "WebAppData").update(token).digest();
  const hash = createHmac("sha256", secret).update(checkString).digest("hex");
  const params = new URLSearchParams(fields);
  params.set("hash", hash);
  return params.toString();
}

function freshFields(): Record<string, string> {
  return {
    auth_date: String(Math.floor(NOW.getTime() / 1000) - 60),
    query_id: "AAF",
    user: USER,
  };
}

test("настоящая подпись принимается", async () => {
  const verified = await verifyInitData(initDataFor(freshFields()), BOT_TOKEN, { now: NOW });

  assert.equal(verified.telegramId, 777);
  assert.equal(verified.user.first_name, "Тим");
});

test("подменённое поле подпись не проходит", async () => {
  const initData = initDataFor(freshFields());
  const tampered = initData.replace("query_id=AAF", "query_id=AAG");

  await assert.rejects(
    () => verifyInitData(tampered, BOT_TOKEN, { now: NOW }),
    InitDataError,
  );
});

test("чужой токен подпись не проходит", async () => {
  const initData = initDataFor(freshFields(), "123456789:other-token");

  await assert.rejects(
    () => verifyInitData(initData, BOT_TOKEN, { now: NOW }),
    InitDataError,
  );
});

test("без hash — отказ", async () => {
  const params = new URLSearchParams(freshFields());

  await assert.rejects(
    () => verifyInitData(params.toString(), BOT_TOKEN, { now: NOW }),
    InitDataError,
  );
});

test("устаревшие данные не принимаются", async () => {
  const stale = {
    ...freshFields(),
    auth_date: String(Math.floor(NOW.getTime() / 1000) - 60 * 60 * 25),
  };

  await assert.rejects(
    () => verifyInitData(initDataFor(stale), BOT_TOKEN, { now: NOW }),
    InitDataError,
  );
});

test("без user — отказ: без Telegram-id некому выдавать доступ", async () => {
  const fields = { auth_date: String(Math.floor(NOW.getTime() / 1000)), query_id: "AAF" };

  await assert.rejects(
    () => verifyInitData(initDataFor(fields), BOT_TOKEN, { now: NOW }),
    InitDataError,
  );
});
