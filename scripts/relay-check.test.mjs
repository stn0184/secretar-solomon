/**
 * Тесты самопроверки канала (`scripts/relay-check.mjs`): сеть подменена.
 * Сама функция базы под anon-ключом проверяется тем же `checkRelay` в
 * `supabase/tests/relay.test.ts` — на тестовой базе ворот.
 *
 * Значения ключей выдуманные.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { checkRelay, main, rpcUrl } from "./relay-check.mjs";

const URL_ = "https://fakeref.supabase.co";
const ANON = "anon-public-value-for-tests";
const RELAY = "relay-secret-value-for-tests-0123456789";
const BOM = String.fromCharCode(0xfeff);

/** Подменённый fetch: ответы по очереди, запросы записываются. */
function fakeFetch(...answers) {
  const calls = [];
  const fetch = async (url, init) => {
    calls.push({ url, init, body: JSON.parse(init.body) });
    const answer = answers.shift();
    if (answer instanceof Error) throw answer;
    return new Response(JSON.stringify(answer.body ?? null), { status: answer.status });
  };
  return { fetch, calls };
}

function world(envText, ...answers) {
  const net = fakeFetch(...answers);
  const lines = [];
  return {
    deps: { readEnv: () => envText, fetch: net.fetch, log: (line) => lines.push(line) },
    calls: net.calls,
    output: () => lines.join("\n"),
  };
}

const ENV = `${BOM}# .env владельца\r\nVITE_SUPABASE_URL=${URL_}\r\nVITE_SUPABASE_ANON_KEY=${ANON}\r\nCHAT_RELAY_KEY=${RELAY}\r\n`;

test("адрес вызова — только HTTPS; локальный HTTP — для тестов", () => {
  assert.equal(rpcUrl(`${URL_}/`), `${URL_}/rest/v1/rpc/relay_chat_events`);
  assert.equal(rpcUrl("http://127.0.0.1:5432"), "http://127.0.0.1:5432/rest/v1/rpc/relay_chat_events");
  assert.equal(rpcUrl("http://fakeref.supabase.co"), null);
  assert.equal(rpcUrl("не адрес"), null);
});

test("канал работает: два вызова с пустой пачкой, свой ключ — 200, чужой — 403", async () => {
  const w = world(ENV, { status: 200, body: [] }, { status: 403, body: { code: "28000" } });
  assert.equal(await main(w.deps), 0);
  assert.match(w.output(), /Канал работает/);

  assert.equal(w.calls.length, 2);
  for (const call of w.calls) {
    assert.equal(call.url, `${URL_}/rest/v1/rpc/relay_chat_events`);
    assert.equal(call.init.method, "POST");
    assert.equal(call.init.headers.apikey, ANON);
    assert.equal(call.init.headers.authorization, `Bearer ${ANON}`);
    assert.deepEqual(call.body.events, []);
  }
  assert.equal(w.calls[0].body.relay_key, RELAY);
  assert.notEqual(w.calls[1].body.relay_key, RELAY);
  assert.equal(w.output().includes(RELAY), false);
  assert.equal(w.output().includes(ANON), false);
});

test("отказ на своём ключе называет причину по статусу", async () => {
  for (const [status, hint] of [
    [403, /deploy-bot\.mjs --env/],
    [404, /миграция этапа 028/],
    [401, /VITE_SUPABASE_ANON_KEY/],
    [500, /500/],
  ]) {
    const w = world(ENV, { status, body: { message: "x" } });
    assert.equal(await main(w.deps), 1, String(status));
    assert.match(w.output(), hint);
    assert.equal(w.calls.length, 1, "второго вызова нет");
  }
});

test("чужой ключ принят — тревога; ответ не пустым списком — не та функция", async () => {
  let w = world(ENV, { status: 200, body: [] }, { status: 200, body: [] });
  assert.equal(await main(w.deps), 1);
  assert.match(w.output(), /принимает кого угодно/);

  w = world(ENV, { status: 200, body: ["accepted"] });
  assert.equal(await main(w.deps), 1);
  assert.match(w.output(), /не пустым списком/);
});

test("сеть упала — отказ без трассировки и без ключей", async () => {
  const report = await checkRelay({
    url: URL_,
    anonKey: ANON,
    relayKey: RELAY,
    fetch: fakeFetch(new TypeError(`fetch failed ${RELAY}`)).fetch,
  });
  assert.equal(report.ok, false);
  assert.deepEqual(report.lines, ["База не отвечает: TypeError."]);
});

test("нет .env или переменной — код 2 и имена, без значений", async () => {
  let w = world(null);
  assert.equal(await main(w.deps), 2);
  assert.match(w.output(), /Нет файла \.env/);

  w = world(`VITE_SUPABASE_URL=${URL_}\nVITE_SUPABASE_ANON_KEY=${ANON}\nCHAT_RELAY_KEY=\n`);
  assert.equal(await main(w.deps), 2);
  assert.match(w.output(), /CHAT_RELAY_KEY/);
  assert.equal(w.output().includes(ANON), false);
  assert.equal(w.calls.length, 0);
});

test("HTTP вместо HTTPS — ключ не уходит вовсе", async () => {
  const w = world(`VITE_SUPABASE_URL=http://fakeref.supabase.co\nVITE_SUPABASE_ANON_KEY=${ANON}\nCHAT_RELAY_KEY=${RELAY}\n`);
  assert.equal(await main(w.deps), 1);
  assert.match(w.output(), /не HTTPS/);
  assert.equal(w.calls.length, 0);
});
