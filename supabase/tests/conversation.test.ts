/**
 * Отправитель пересланного сообщения на настоящем Postgres (этап 016):
 * колонка `messages.forwarded_from` и восьмой аргумент `record_message`.
 *
 * Колонка нужна недавнему разговору (блок 6): без неё чужие слова из
 * пересланного выглядели бы в промпте словами владельца. Правила —
 * `techspec/17-conversation.md` §17.5 и `techspec/03-schema.md` §3.2, §3.4.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import type { PGlite } from "@electric-sql/pglite";

import { withDatabase, withDatabaseBefore } from "./database.ts";

const OWNER = 777;
const MIGRATION = "20261002100000_conversation.sql";

interface Recorded {
  id: string;
  text: string;
  forwarded_from: string | null;
}

let telegramMessageId = 0;

/** Запись сообщения, как её делает бот: восемь аргументов, отправитель последним. */
async function record(
  db: PGlite,
  body: string,
  forwardedFrom: string | null,
  messageId?: number,
): Promise<Recorded> {
  telegramMessageId += 1;
  const { rows } = await db.query<Recorded>(
    `select id, text, forwarded_from
       from public.record_message($1, $2, $3, $4, 'text', null, null, $5)`,
    [OWNER, OWNER, messageId ?? telegramMessageId, body, forwardedFrom],
  );
  assert.equal(rows.length, 1);
  return rows[0]!;
}

// --- Запись отправителя --------------------------------------------------------

test("у пересланного в строке имя отправителя, у своего — пусто", () =>
  withDatabase(async (db) => {
    const forwarded = await record(db, "Во сколько?", "Рената");
    const own = await record(db, "что у меня в четверг?", null);

    assert.equal(forwarded.forwarded_from, "Рената");
    assert.equal(own.forwarded_from, null);
  }));

test("вызов без восьмого аргумента — как раньше: отправителя нет", () =>
  withDatabase(async (db) => {
    const { rows } = await db.query<Recorded>(
      "select id, text, forwarded_from from public.record_message($1, $2, 1, 'купить лампочку')",
      [OWNER, OWNER],
    );

    assert.equal(rows[0]!.text, "купить лампочку");
    assert.equal(rows[0]!.forwarded_from, null);
  }));

test("повтор того же обновления возвращает прежнюю строку и отправителя не меняет", () =>
  withDatabase(async (db) => {
    const first = await record(db, "Во сколько?", "Рената", 500);
    const again = await record(db, "другой текст", "Георгий", 500);

    assert.deepEqual(again, first);
    const { rows } = await db.query<{ count: number }>(
      "select count(*)::int as count from public.messages where owner_telegram_id = $1",
      [OWNER],
    );
    assert.equal(rows[0]!.count, 1);
  }));

// --- Функция и доступ ----------------------------------------------------------

test("у record_message одна перегрузка — с forwarded_from в конце, — и зовёт её только service_role", () =>
  withDatabase(async (db) => {
    // Две перегрузки с умолчаниями PostgREST не различит (§17.5): прежняя удалена.
    const { rows } = await db.query<{ oid: number; args: string }>(
      `select p.oid, pg_get_function_identity_arguments(p.oid) as args
         from pg_proc p join pg_namespace n on n.oid = p.pronamespace
        where n.nspname = 'public' and p.proname = 'record_message'`,
    );
    assert.equal(rows.length, 1);
    assert.match(rows[0]!.args, /duration_seconds integer, forwarded_from text$/);

    for (const [role, allowed] of [
      ["anon", false],
      ["authenticated", false],
      ["service_role", true],
    ] as const) {
      const { rows: granted } = await db.query<{ ok: boolean }>(
        "select has_function_privilege($1, $2::oid, 'execute') as ok",
        [role, rows[0]!.oid],
      );
      assert.equal(granted[0]!.ok, allowed, `execute у ${role}`);
    }
  }));

// --- Миграция поверх старых строк ----------------------------------------------

test("у сообщений, записанных до миграции, отправителя нет — текст на месте", () =>
  withDatabaseBefore(MIGRATION, async (db, migrate) => {
    const { rows } = await db.query<{ id: string }>(
      "select id from public.record_message($1, $2, 1, 'Во сколько?')",
      [OWNER, OWNER],
    );

    await migrate();

    const { rows: after } = await db.query<Recorded>(
      "select id, text, forwarded_from from public.messages where id = $1",
      [rows[0]!.id],
    );
    assert.deepEqual(after, [{ id: rows[0]!.id, text: "Во сколько?", forwarded_from: null }]);
  }));
