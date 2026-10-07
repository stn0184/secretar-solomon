/**
 * Переписка от Partner Assistant (§28): таблица ключей `chat_relays`, приём
 * `relay_chat_events` под ролью `anon` с ключом передачи и самопроверка
 * канала по HTTP.
 *
 * Partner Assistant здесь выдуманный: события собираются тестом, а вызов идёт
 * под `anon`, как из его проекта через PostgREST. Регистрацию ключа и ответ о
 * согласии делает бот ключом service-role — здесь от владельца базы.
 * Переписки выдуманные — Игорь и Олег.
 */
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import type { AddressInfo } from "node:net";
import { test } from "node:test";

import type { PGlite } from "@electric-sql/pglite";

import { checkRelay } from "../../scripts/relay-check.mjs";
import { asRole, withDatabase } from "./database.ts";

const OWNER = 777;
const STRANGER = 999;
const PARTNER = "partner";
const RELAY = "relay:partner";
const KEY = "partner-relay-key-for-tests-only-7f3a9c41d2";
const NEW_KEY = "partner-relay-key-second-for-tests-2b8e1d77";
const IGOR = 1001;
const OLEG = 1002;

const RELAY_SIGNATURE = "public.relay_chat_events(text, jsonb)";
const REGISTER_SIGNATURE = "public.register_chat_relay(text, text)";

interface Source {
  connection_id: string | null;
  is_enabled: boolean;
  asked_at: Date | null;
  consented_at: Date | null;
  declined_at: Date | null;
}

interface Relay {
  name: string;
  key_hash: string;
  revoked_at: Date | null;
}

interface ChatMessage {
  external_id: string;
  direction: string;
  sender: string;
  kind: string;
  text: string;
  erased_at: Date | null;
}

function only<T>(rows: T[]): T {
  assert.equal(rows.length, 1, `ждали одну строку, пришло ${rows.length}`);
  return rows[0]!;
}

function hashOf(key: string): string {
  return createHash("sha256").update(key, "utf8").digest("hex");
}

async function count(db: PGlite, table: string): Promise<number> {
  const { rows } = await db.query<{ n: number }>(`select count(*)::int as n from public.${table}`);
  return rows[0]!.n;
}

/** Человек пользуется Соломоном: бот при запуске пишет его пояс (§3.8). */
async function solomonUser(db: PGlite, owner = OWNER): Promise<void> {
  await db.query("select public.save_owner_timezone($1, 'Asia/Yekaterinburg')", [owner]);
}

/** Бот при запуске записал ключ из окружения (или его нет — `null`). Ответ — сколько отозвано. */
async function register(db: PGlite, key: string | null, name = PARTNER): Promise<number> {
  const { rows } = await db.query<{ revoked: number }>("select public.register_chat_relay($1, $2) as revoked", [
    name,
    key === null ? null : hashOf(key),
  ]);
  return only(rows).revoked;
}

async function relays(db: PGlite): Promise<Relay[]> {
  const { rows } = await db.query<Relay>("select name, key_hash, revoked_at from public.chat_relays order by created_at, key_hash");
  return rows;
}

/** Вызов Partner Assistant: под `anon`, как через PostgREST. */
async function relay(db: PGlite, events: unknown, key: string | null = KEY): Promise<string[]> {
  return await asRole(db, "anon", null, async () => {
    const { rows } = await db.query<{ outcomes: string[] }>(
      "select public.relay_chat_events($1, $2::jsonb) as outcomes",
      [key, JSON.stringify(events)],
    );
    return only(rows).outcomes;
  });
}

/** Отказ вызова целиком с кодом SQLSTATE — PostgREST отдаст его статусом (§28.2). */
async function refused(call: Promise<unknown>, code: string): Promise<Error> {
  let caught: unknown;
  await assert.rejects(call, (error: unknown) => {
    caught = error;
    return (error as { code?: string }).code === code;
  });
  return caught as Error;
}

async function source(db: PGlite, owner = OWNER): Promise<Source | undefined> {
  const { rows } = await db.query<Source>(
    "select * from public.chat_sources where owner_telegram_id = $1 and platform = 'telegram'",
    [owner],
  );
  return rows[0];
}

async function chatMessages(db: PGlite, chat = IGOR): Promise<ChatMessage[]> {
  const { rows } = await db.query<ChatMessage>(
    `select m.* from public.chat_messages m
       join public.chat_threads t on t.id = m.thread_id
      where t.owner_telegram_id = $1 and t.platform = 'telegram' and t.chat_key = $2
      order by m.sent_at, m.external_id`,
    [OWNER, String(chat)],
  );
  return rows;
}

function linked(account: unknown = OWNER): Record<string, unknown> {
  return { type: "linked", account_telegram_id: account };
}

function unlinked(account: unknown = OWNER): Record<string, unknown> {
  return { type: "unlinked", account_telegram_id: account };
}

function message(id: number, changes: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    type: "message",
    account_telegram_id: OWNER,
    chat_id: IGOR,
    chat_name: "Игорь Петров",
    message_id: id,
    direction: "in",
    by_assistant: false,
    sent_at: "2026-10-07T10:00:00Z",
    kind: "text",
    text: "Пришлёшь расчёт в пятницу?",
    ...changes,
  };
}

function edited(id: number, text: string, changes: Record<string, unknown> = {}): Record<string, unknown> {
  return { type: "edited", account_telegram_id: OWNER, chat_id: IGOR, message_id: id, text, ...changes };
}

function deleted(ids: unknown, changes: Record<string, unknown> = {}): Record<string, unknown> {
  return { type: "deleted", account_telegram_id: OWNER, chat_id: IGOR, message_ids: ids, ...changes };
}

/** Владелец ответил на вопрос о согласии — это делает бот (§25.5). */
async function answer(db: PGlite, agreed: boolean): Promise<void> {
  await db.query("select public.mark_consent_asked($1, 'telegram')", [OWNER]);
  await db.query("select public.answer_consent($1, 'telegram', $2)", [OWNER, agreed]);
}

/** Ключ записан, Partner Assistant прислал `linked`, владелец согласился. */
async function ready(db: PGlite): Promise<void> {
  await solomonUser(db);
  await register(db, KEY);
  assert.deepEqual(await relay(db, [linked()]), ["accepted"]);
  await answer(db, true);
}

// --- Схема и права -------------------------------------------------------------

test("приём — одна перегрузка и только у anon; ключ записывает только service_role", async () => {
  await withDatabase(async (db) => {
    for (const [signature, expected] of [
      [RELAY_SIGNATURE, { anon: true, authenticated: false, service_role: false }],
      [REGISTER_SIGNATURE, { anon: false, authenticated: false, service_role: true }],
    ] as const) {
      const name = signature.slice("public.".length, signature.indexOf("("));
      const { rows } = await db.query<{ n: number }>(
        "select count(*)::int as n from pg_proc where proname = $1 and pronamespace = 'public'::regnamespace",
        [name],
      );
      assert.equal(rows[0]?.n, 1, name);
      const rights = await db.query<{ role: string; allowed: boolean }>(
        `select role, has_function_privilege(role, $1, 'execute') as allowed
           from unnest(array['anon', 'authenticated', 'service_role']) as role`,
        [signature],
      );
      assert.deepEqual(Object.fromEntries(rights.rows.map((row) => [row.role, row.allowed])), expected, signature);
    }

    const { rows: definer } = await db.query<{ prosecdef: boolean; proconfig: string[] | null }>(
      "select prosecdef, proconfig from pg_proc where oid = $1::regprocedure",
      [RELAY_SIGNATURE],
    );
    assert.equal(only(definer).prosecdef, true);
    assert.deepEqual(only(definer).proconfig, ['search_path=""']);
  });
});

test("anon зовёт в public только relay_chat_events и не читает ни одной таблицы", async () => {
  await withDatabase(async (db) => {
    await ready(db);
    assert.deepEqual(await relay(db, [message(1)]), ["accepted"]);

    const { rows: callable } = await db.query<{ signature: string }>(
      `select p.oid::regprocedure::text as signature
         from pg_proc p
        where p.pronamespace = 'public'::regnamespace
          and has_function_privilege('anon', p.oid, 'execute')`,
    );
    assert.deepEqual(callable.map((row) => row.signature), ["relay_chat_events(text,jsonb)"]);

    const { rows: tables } = await db.query<{ relname: string; relrowsecurity: boolean }>(
      `select relname, relrowsecurity from pg_class
        where relnamespace = 'public'::regnamespace and relkind in ('r', 'p', 'v', 'm')
        order by relname`,
    );
    assert.ok(tables.length >= 12);
    for (const table of tables) assert.equal(table.relrowsecurity, true, table.relname);

    const { rows: policies } = await db.query<{ policyname: string }>(
      `select policyname from pg_policies
        where schemaname = 'public' and roles && array['anon', 'public']::name[]`,
    );
    assert.deepEqual(policies, []);

    // Данные есть, а anon их не видит: строк нет или нет права.
    for (const { relname } of tables) {
      await asRole(db, "anon", null, async () => {
        try {
          const { rows } = await db.query(`select * from public.${relname}`);
          assert.deepEqual(rows, [], relname);
        } catch (error) {
          assert.match((error as Error).message, /permission denied/, relname);
        }
      });
    }
    for (const table of ["chat_relays", "chat_sources", "chat_messages", "owner_settings"]) {
      assert.ok((await count(db, table)) > 0, table);
    }
  });
});

test("ключ передачи хранится только хэшем; таблицу ключей не читают ни anon, ни приложение", async () => {
  await withDatabase(async (db) => {
    await register(db, KEY);
    const relay = only(await relays(db));
    assert.equal(relay.name, PARTNER);
    assert.equal(relay.key_hash, hashOf(KEY));
    assert.match(relay.key_hash, /^[0-9a-f]{64}$/);
    const { rows } = await db.query<{ row: string }>("select row_to_json(r)::text as row from public.chat_relays r");
    assert.equal(only(rows).row.includes(KEY), false);

    for (const role of ["anon", "authenticated"] as const) {
      await asRole(db, role, OWNER, async () => {
        await assert.rejects(db.query("select * from public.chat_relays"), /permission denied/);
        await assert.rejects(
          db.query("select public.register_chat_relay('partner', $1)", [hashOf("чужой ключ")]),
          /permission denied/,
        );
      });
    }
    await assert.rejects(register(db, "x".repeat(40), "Partner Assistant"), /chat_relays_name_check/);
    await assert.rejects(
      db.query("select public.register_chat_relay('partner', 'not-a-hash')"),
      /chat_relays_hash_check/,
    );
  });
});

// --- Ключ ----------------------------------------------------------------------

test("неверный или пустой ключ — отказ без подробностей, ничего не записано", async () => {
  await withDatabase(async (db) => {
    await solomonUser(db);
    await register(db, KEY);
    for (const key of ["неверный ключ", "", null]) {
      const error = await refused(relay(db, [linked(), message(1)], key), "28000");
      assert.equal(error.message, "relay_chat_events: refused");
    }
    // Без единого ключа в базе приём выключен вовсе.
    await register(db, null);
    await refused(relay(db, [linked()]), "28000");
    assert.equal(await count(db, "chat_sources"), 0);
    assert.equal(await count(db, "chat_messages"), 0);
  });
});

test("новый ключ отзывает прежний; без ключа отозваны все; прежний можно вернуть", async () => {
  await withDatabase(async (db) => {
    await solomonUser(db);
    assert.equal(await register(db, KEY), 0);
    assert.equal(await register(db, KEY), 0, "повтор при перезапуске ничего не отзывает");
    assert.deepEqual(await relay(db, []), []);

    assert.equal(await register(db, NEW_KEY), 1);
    await refused(relay(db, [], KEY), "28000");
    assert.deepEqual(await relay(db, [], NEW_KEY), []);
    assert.deepEqual(
      (await relays(db)).map((row) => [row.key_hash, row.revoked_at !== null]),
      [
        [hashOf(KEY), true],
        [hashOf(NEW_KEY), false],
      ],
    );

    assert.equal(await register(db, null), 1);
    await refused(relay(db, [], NEW_KEY), "28000");
    assert.ok((await relays(db)).every((row) => row.revoked_at !== null));

    assert.equal(await register(db, KEY), 0);
    assert.deepEqual(await relay(db, [], KEY), []);
    await refused(relay(db, [], NEW_KEY), "28000");
    assert.equal((await relays(db)).length, 2, "ключ не заводится второй строкой");
  });
});

// --- Пределы -------------------------------------------------------------------

test("больше 50 событий, текст длиннее 4000 или имя чата длиннее 200 — отказ вызова целиком", async () => {
  await withDatabase(async (db) => {
    await solomonUser(db);
    await register(db, KEY);
    const many = [linked(), ...Array.from({ length: 50 }, (_, index) => message(index + 1))];
    await refused(relay(db, many), "22023");
    await refused(relay(db, [linked(), message(1, { text: "а".repeat(4001) })]), "22023");
    await refused(relay(db, [linked(), edited(1, "а".repeat(4001))]), "22023");
    await refused(relay(db, [linked(), message(1, { chat_name: "И".repeat(201) })]), "22023");
    await refused(relay(db, { type: "linked" }), "22023");
    await refused(relay(db, null), "22023");
    assert.equal(await source(db), undefined, "linked из отвергнутого вызова не применён");

    // Ровно на пределе — принимается.
    const edge = [linked(), ...Array.from({ length: 49 }, (_, index) => message(index + 1))];
    assert.equal((await relay(db, edge)).length, 50);
    const outcomes = await relay(db, [
      message(100, { text: "а".repeat(4000), chat_name: "И".repeat(200) }),
    ]);
    assert.deepEqual(outcomes, ["not_consented"]);
  });
});

// --- linked, согласие и чей аккаунт ----------------------------------------------

test("linked включает telegram через Partner Assistant; вопрос задаст тик, до «Согласен» — не принято", async () => {
  await withDatabase(async (db) => {
    await solomonUser(db);
    await register(db, KEY);

    assert.deepEqual(await relay(db, [linked(), message(1)]), ["accepted", "not_consented"]);
    const linkedSource = await source(db);
    assert.equal(linkedSource?.connection_id, RELAY);
    assert.equal(linkedSource?.is_enabled, true);
    assert.equal(linkedSource?.asked_at, null, "вопрос о согласии задаст тик бота");
    assert.equal(linkedSource?.consented_at, null);
    assert.equal(await count(db, "chat_messages"), 0);
    assert.equal(await count(db, "chat_threads"), 0);

    // «Не надо» — не принято и дальше.
    await answer(db, false);
    assert.deepEqual(await relay(db, [message(2)]), ["not_consented"]);
    assert.equal(await count(db, "chat_messages"), 0);

    // «Согласен» — принято; раньше пришедшее не появляется.
    await db.query("select public.answer_consent($1, 'telegram', true)", [OWNER]);
    assert.deepEqual(await relay(db, [message(3)]), ["accepted"]);
    assert.deepEqual((await chatMessages(db)).map((row) => row.external_id), ["3"]);
  });
});

test("не пользователь Соломона — unknown_account: ничего не заводится", async () => {
  await withDatabase(async (db) => {
    await solomonUser(db);
    await register(db, KEY);
    const stranger = { account_telegram_id: STRANGER };
    const outcomes = await relay(db, [
      linked(STRANGER),
      message(1, stranger),
      edited(1, "т", stranger),
      deleted([1], stranger),
      unlinked(STRANGER),
    ]);
    assert.deepEqual(outcomes, Array(5).fill("unknown_account"));
    assert.equal(await count(db, "chat_sources"), 0);
    assert.equal(await count(db, "chat_threads"), 0);
  });
});

// --- Сообщения -----------------------------------------------------------------

test("после согласия сообщение идёт общим путём §25; повтор — duplicate, без дубля", async () => {
  await withDatabase(async (db) => {
    await ready(db);
    const outcomes = await relay(db, [
      message(1),
      message(2, { direction: "out", text: "Да, в пятницу пришлю" }),
      message(1),
      message(7, { chat_id: String(OLEG), chat_name: "Олег", text: "Верну книгу в среду" }),
    ]);
    assert.deepEqual(outcomes, ["accepted", "accepted", "duplicate", "accepted"]);
    assert.deepEqual(await relay(db, [message(2, { direction: "out" })]), ["duplicate"]);

    const igor = await chatMessages(db);
    assert.deepEqual(
      igor.map((row) => [row.external_id, row.direction, row.sender, row.kind, row.text]),
      [
        ["1", "in", "Игорь Петров", "text", "Пришлёшь расчёт в пятницу?"],
        ["2", "out", "", "text", "Да, в пятницу пришлю"],
      ],
    );
    assert.equal((await chatMessages(db, OLEG)).length, 1);

    // Чат — площадка telegram под ключом чата Telegram: разбор и «Из переписки
    // с … (Telegram)» — как у прямого подключения.
    const { rows } = await db.query<{ platform: string; chat_key: string; name: string }>(
      "select platform, chat_key, name from public.chat_threads order by chat_key",
    );
    assert.deepEqual(
      rows.map((row) => [row.platform, row.chat_key, row.name]),
      [
        ["telegram", "1001", "Игорь Петров"],
        ["telegram", "1002", "Олег"],
      ],
    );
    const { rows: due } = await db.query<{ platform: string }>(
      "select * from public.chats_to_analyze($1, now() + interval '1 hour', now())",
      [OWNER],
    );
    assert.deepEqual(due.map((row) => row.platform), ["telegram", "telegram"]);
  });
});

test("ответ Partner Assistant от имени владельца — out: снимает «ждёт ответа»", async () => {
  await withDatabase(async (db) => {
    await ready(db);
    await relay(db, [message(1, { sent_at: "2026-10-07T10:00:00Z", text: "Во сколько созвон?" })]);
    const { rows: threads } = await db.query<{ id: string }>("select id from public.chat_threads");
    const { rows: ids } = await db.query<{ id: string }>("select id from public.chat_messages");
    await db.query(
      `select public.record_chat_analysis($1, $2, $3::uuid[], '{}'::jsonb, 'claude-opus-5', 1, 1, 1,
                                          'Игорем', $4::jsonb, '[]'::jsonb)`,
      [
        OWNER,
        only(threads).id,
        ids.map((row) => row.id),
        JSON.stringify({ about: "он спрашивал, во сколько созвон", to: "Игорю", since: "2026-10-07T10:00:00Z" }),
      ],
    );
    const waiting = async () =>
      only((await db.query<{ waiting_since: Date | null; last_out_at: Date | null }>("select * from public.chat_threads")).rows);
    assert.ok((await waiting()).waiting_since);

    // Partner Assistant ответил сам, обещав от имени владельца.
    assert.deepEqual(
      await relay(db, [
        message(2, {
          direction: "out",
          by_assistant: true,
          sent_at: "2026-10-07T10:20:00Z",
          text: "В 15:00, и расчёт пришлю завтра",
        }),
      ]),
      ["accepted"],
    );
    const after = await waiting();
    assert.equal(after.waiting_since, null);
    assert.equal(after.last_out_at?.toISOString(), "2026-10-07T10:20:00.000Z");
    assert.equal((await chatMessages(db)).at(-1)?.direction, "out");
  });
});

test("голосовое — с расшифровкой Partner Assistant или без текста", async () => {
  await withDatabase(async (db) => {
    await ready(db);
    assert.deepEqual(
      await relay(db, [
        message(1, { kind: "voice", text: "Перезвоню после обеда" }),
        message(2, { kind: "voice", text: "" }),
        message(3, { kind: "video_note" }),
        message(4, { kind: "photo", text: "" }),
      ]),
      ["accepted", "accepted", "accepted", "accepted"],
    );
    const { rows } = await db.query<{ kind: string; text: string }>(
      "select kind, text from public.chat_messages order by external_id",
    );
    assert.deepEqual(
      rows.map((row) => [row.kind, row.text]),
      [
        ["voice", "Перезвоню после обеда"],
        ["voice", ""],
        ["video_note", "Пришлёшь расчёт в пятницу?"],
        ["photo", ""],
      ],
    );
  });
});

test("правка и удаление — общим путём; без согласия — не принято", async () => {
  await withDatabase(async (db) => {
    await ready(db);
    await relay(db, [message(1), message(2, { text: "Олег вернёт книгу" })]);

    assert.deepEqual(await relay(db, [edited(1, "Пришлёшь расчёт в четверг?"), deleted([2, 3])]), [
      "accepted",
      "accepted",
    ]);
    const rows = await chatMessages(db);
    assert.equal(rows[0]?.text, "Пришлёшь расчёт в четверг?");
    assert.equal(rows[1]?.text, "");
    assert.ok(rows[1]?.erased_at);

    await db.query("select public.answer_consent($1, 'telegram', false)", [OWNER]);
    assert.deepEqual(await relay(db, [edited(1, "другое"), deleted([1])]), ["not_consented", "not_consented"]);
    assert.equal((await chatMessages(db))[0]?.text, "Пришлёшь расчёт в четверг?");
  });
});

// --- unlinked и прямое подключение ---------------------------------------------------

test("unlinked останавливает приём; новый linked возвращает его без нового вопроса", async () => {
  await withDatabase(async (db) => {
    await ready(db);
    assert.deepEqual(await relay(db, [unlinked(), message(1)]), ["accepted", "not_consented"]);
    let state = await source(db);
    assert.equal(state?.is_enabled, false);
    assert.equal(state?.connection_id, RELAY);
    assert.equal(await count(db, "chat_messages"), 0);

    assert.deepEqual(await relay(db, [linked(), message(2)]), ["accepted", "accepted"]);
    state = await source(db);
    assert.equal(state?.is_enabled, true);
    assert.ok(state?.consented_at, "согласие не переспрашивается");
  });
});

test("linked заменяет прямое подключение: согласие спрашивается заново; unlinked прямое не трогает", async () => {
  await withDatabase(async (db) => {
    await solomonUser(db);
    await register(db, KEY);
    // Прямое подключение с согласием (§25.2).
    await db.query("select public.connect_chat_source($1, 'telegram', 'biz-777', true)", [OWNER]);
    await answer(db, true);

    // Partner Assistant занял место в «Автоматизации чатов»: unlinked от него
    // прямое подключение не выключает.
    assert.deepEqual(await relay(db, [unlinked(), message(1)]), ["accepted", "not_consented"]);
    assert.equal((await source(db))?.connection_id, "biz-777");
    assert.equal((await source(db))?.is_enabled, true);

    assert.deepEqual(await relay(db, [linked(), message(2)]), ["accepted", "not_consented"]);
    const state = await source(db);
    assert.equal(state?.connection_id, RELAY);
    assert.equal(state?.is_enabled, true);
    assert.deepEqual([state?.asked_at, state?.consented_at, state?.declined_at], [null, null, null]);

    // Сообщения прежнего подключения больше не принимаются.
    const { rows } = await db.query<{ outcome: string }>(
      `select * from public.store_chat_message($1, 'telegram', 'biz-777', '1001', 'Игорь', '9', 'in', 'Игорь',
                                               now(), 'text', 'т', true)`,
      [OWNER],
    );
    assert.equal(only(rows).outcome, "unknown_connection");
  });
});

// --- Кривые события ------------------------------------------------------------

test("кривое событие — invalid, остальные события вызова принимаются", async () => {
  await withDatabase(async (db) => {
    await ready(db);
    const outcomes = await relay(db, [
      "linked",
      { type: "joined", account_telegram_id: OWNER },
      linked("Игорь"),
      linked(-5),
      message(1, { chat_id: -100123, chat_name: "Дача" }),
      message(2, { chat_id: "группа" }),
      message(3, { message_id: null }),
      message(4, { message_id: "abc" }),
      message(5, { direction: "sideways" }),
      message(6, { direction: "in", by_assistant: true }),
      message(7, { by_assistant: "да" }),
      message(8, { sent_at: "вчера" }),
      message(9, { sent_at: null }),
      message(10, { kind: "sticker" }),
      message(11, { text: 42 }),
      edited(1, "т", { text: null }),
      deleted([]),
      deleted("1"),
      deleted([1, "x"]),
      message(12),
    ]);
    assert.deepEqual(outcomes, [...Array(19).fill("invalid"), "accepted"]);
    assert.deepEqual((await chatMessages(db)).map((row) => row.external_id), ["12"]);
    assert.equal(await count(db, "chat_threads"), 1);
  });
});

// --- Канал: самопроверка по HTTP ----------------------------------------------------

const ANON_KEY = "anon-key-for-tests-only";

/** Код SQLSTATE → статус HTTP, как его отдаёт PostgREST (таблица из его документации). */
function postgrestStatus(code: string | undefined): number {
  if (code === undefined) return 500;
  if (code.startsWith("28")) return 403;
  if (code === "42501") return 401;
  if (code === "42883") return 404;
  if (code.startsWith("08") || code.startsWith("53")) return 503;
  if (code === "P0001") return 400;
  const server = ["09", "25", "2D", "38", "39", "3B", "40", "54", "55", "57", "58", "F0", "HV", "P0", "XX"];
  return server.some((prefix) => code.startsWith(prefix)) ? 500 : 400;
}

async function readBody(request: IncomingMessage): Promise<string> {
  const chunks: Buffer[] = [];
  for await (const chunk of request) chunks.push(chunk as Buffer);
  return Buffer.concat(chunks).toString("utf8");
}

/**
 * PostgREST перед тестовой базой: `POST /rest/v1/rpc/relay_chat_events` с
 * anon-ключом в `apikey` — вызов под ролью `anon` с аргументами из тела по
 * именам. Запросы идут по одному: соединение PGlite одно.
 */
async function withPostgrest(db: PGlite, body: (url: string) => Promise<void>): Promise<void> {
  const answer = (response: ServerResponse, status: number, payload: unknown): void => {
    response.writeHead(status, { "content-type": "application/json" });
    response.end(JSON.stringify(payload));
  };
  const server = createServer((request, response) => {
    void (async () => {
      if (request.method !== "POST" || request.url !== "/rest/v1/rpc/relay_chat_events") {
        answer(response, 404, { code: "PGRST202", message: "not found" });
        return;
      }
      if (request.headers.apikey !== ANON_KEY || request.headers.authorization !== `Bearer ${ANON_KEY}`) {
        answer(response, 401, { message: "Invalid API key" });
        return;
      }
      const args = JSON.parse(await readBody(request)) as { relay_key?: unknown; events?: unknown };
      try {
        const result = await asRole(db, "anon", null, async () => {
          const { rows } = await db.query<{ result: unknown }>(
            "select public.relay_chat_events(relay_key => $1, events => $2::jsonb) as result",
            [args.relay_key ?? null, JSON.stringify(args.events ?? null)],
          );
          return only(rows).result;
        });
        answer(response, 200, result);
      } catch (error) {
        const { code, message } = error as { code?: string; message: string };
        answer(response, postgrestStatus(code), { code, message });
      }
    })();
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address() as AddressInfo;
  try {
    await body(`http://127.0.0.1:${port}`);
  } finally {
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }
}

test("самопроверка канала: ключ принят под anon-ключом, чужой ключ отвергнут, ничего не записано", async () => {
  await withDatabase(async (db) => {
    await solomonUser(db);
    await withPostgrest(db, async (url) => {
      // Ключа в базе ещё нет — бот его не записал.
      let report = await checkRelay({ url, anonKey: ANON_KEY, relayKey: KEY });
      assert.equal(report.ok, false);
      assert.match(report.lines.join("\n"), /403/);

      await register(db, KEY);
      report = await checkRelay({ url, anonKey: ANON_KEY, relayKey: KEY });
      assert.deepEqual(report.ok, true, report.lines.join("\n"));

      report = await checkRelay({ url, anonKey: "wrong-anon-key-for-tests", relayKey: KEY });
      assert.equal(report.ok, false);
      assert.match(report.lines.join("\n"), /401/);

      for (const line of report.lines) {
        assert.equal(line.includes(KEY), false);
        assert.equal(line.includes(ANON_KEY), false);
      }
    });
    assert.equal(await count(db, "chat_sources"), 0);
    assert.equal(await count(db, "chat_messages"), 0);
  });
});
