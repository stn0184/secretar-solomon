/**
 * Снимок на настоящем Postgres (этап 012): вид `photo`, колонка
 * `messages.photo_text` и её запись в `record_understanding`.
 *
 * Бот пишет снимок, как голос: сначала `record_message` с видом, файлом и
 * подписью, потом разбор одной транзакцией. Вызов без `photo_text` — тот,
 * что шлют текст и голос, — работает как раньше. Правила —
 * `techspec/14-photo.md` §14.2, §14.5 и `techspec/03-schema.md` §3.2, §3.4.
 */
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { test } from "node:test";

import type { PGlite } from "@electric-sql/pglite";

import { asRole, messageRow, withDatabase, withDatabaseBefore } from "./database.ts";

const OWNER = 777;
const STRANGER = 999;
const ASKED = "К какому сроку?";
const MIGRATION = "20260930100000_photo.sql";
const READ = "Приглашение: родительское собрание 7 октября в 18:30, кабинет 214.";

type Json = null | boolean | number | string | Json[] | { [key: string]: Json };

const ERRAND = {
  title: "сходить на родительское собрание",
  kind: "task",
  due_at: "2026-10-07T13:30:00.000Z",
  due_precision: "time",
  priority: "normal",
  promise: null,
  people: [],
  needs_review: false,
} satisfies Json;

let telegramMessageId = 0;

/** Первый шаг приёма снимка (§14.2): вид, файл и подпись — до разбора. */
async function photo(db: PGlite, caption = "", owner = OWNER): Promise<string> {
  telegramMessageId += 1;
  const { rows } = await db.query<{ id: string }>(
    "select id from public.record_message($1, $2, $3, $4, 'photo', $5)",
    [owner, owner, telegramMessageId, caption, `photo-${telegramMessageId}`],
  );
  assert.equal(rows.length, 1);
  return rows[0]!.id;
}

/** Текстовое сообщение — для задачи с вопросом и для вызова «как раньше». */
async function text(db: PGlite, body: string): Promise<string> {
  telegramMessageId += 1;
  const { rows } = await db.query<{ id: string }>(
    "select id from public.record_message($1, $2, $3, $4)",
    [OWNER, OWNER, telegramMessageId, body],
  );
  assert.equal(rows.length, 1);
  return rows[0]!.id;
}

interface Call {
  messageId: string;
  owner?: number;
  analysis?: Json;
  task?: Json;
  amend?: Json;
  /** `undefined` — аргумента нет вовсе, как у текста и голоса. */
  photoText?: string | null;
}

function json(value: Json | undefined): string | null {
  return value === undefined || value === null ? null : JSON.stringify(value);
}

/** Второй шаг — вызов, как его делает бот; `photo_text` — только если он есть. */
async function understand(db: PGlite, call: Call): Promise<string | null> {
  const analysis = call.analysis === undefined ? { kind: "task" } : call.analysis;
  const understood = analysis !== null;
  const params: unknown[] = [
    call.messageId,
    call.owner ?? OWNER,
    json(analysis),
    understood ? "claude-opus-5" : null,
    understood ? 1800 : null,
    understood ? 240 : null,
    "ответ бота",
    json(call.task),
    "[]",
    "[]",
    json(call.amend),
  ];
  let photoText = "";
  if (call.photoText !== undefined) {
    params.push(call.photoText);
    photoText = ", photo_text => $12";
  }
  const { rows } = await db.query<{ id: string | null }>(
    `select id from public.record_understanding(
       message_id => $1, owner_telegram_id => $2, analysis => $3::jsonb,
       ai_model => $4, ai_input_tokens => $5, ai_output_tokens => $6,
       reply => $7, task => $8::jsonb, reminders => $9::jsonb, facts => $10::jsonb,
       transcript => null, transcript_confidence => null, amend => $11::jsonb${photoText}
     )`,
    params,
  );
  assert.equal(rows.length, 1);
  return rows[0]!.id;
}

/** Задача с открытым вопросом «К какому сроку?» (§10.1). */
async function askedTask(db: PGlite): Promise<string> {
  const id = await understand(db, {
    messageId: await text(db, "сходить на собрание"),
    task: { ...ERRAND, due_at: null, due_precision: null, open_question: ASKED },
  });
  assert.ok(id, "задача с вопросом не заведена");
  return id;
}

async function openQuestion(db: PGlite, taskId: string): Promise<string | null> {
  const { rows } = await db.query<{ open_question: string | null }>(
    "select open_question from public.tasks where id = $1",
    [taskId],
  );
  return rows[0]!.open_question;
}

// --- Вид сообщения ------------------------------------------------------------

test("снимок пишется с видом photo, файлом и подписью, без длительности", () =>
  withDatabase(async (db) => {
    const id = await photo(db, "купить такие же");

    const row = await messageRow(db, id);
    assert.equal(row.kind, "photo");
    assert.equal(row.text, "купить такие же");
    assert.match(row.telegram_file_id ?? "", /^photo-/);
    assert.equal(row.duration_seconds, null);
    assert.equal(row.photo_text, null);
  }));

test("вид вне списка база отвергает", () =>
  withDatabase(async (db) => {
    await assert.rejects(
      db.query("select id from public.record_message($1, $2, $3, '', 'sticker')", [
        OWNER,
        OWNER,
        1,
      ]),
      /messages_kind_check/,
    );
  }));

// --- photo_text в record_understanding ----------------------------------------

test("разбор снимка кладёт прочитанное в строку сообщения вместе с задачей", () =>
  withDatabase(async (db) => {
    const messageId = await photo(db);

    const taskId = await understand(db, { messageId, task: ERRAND, photoText: READ });

    assert.ok(taskId);
    const row = await messageRow(db, messageId);
    assert.equal(row.photo_text, READ);
    assert.equal(row.task_id, taskId);
    assert.equal(row.reply, "ответ бота");
    // Подпись остаётся подписью: прочитанное не подменяет `text`.
    assert.equal(row.text, "");
  }));

test("вызов без photo_text работает как раньше и прочитанного не стирает", () =>
  withDatabase(async (db) => {
    const typed = await text(db, "купить лампочку");
    assert.ok(await understand(db, { messageId: typed, task: ERRAND }));
    assert.equal((await messageRow(db, typed)).photo_text, null);

    // Повтор разбора снимка без аргумента (например, старый бот) — прочитанное на месте.
    const messageId = await photo(db);
    await understand(db, { messageId, analysis: { kind: "chat" }, photoText: READ });
    await understand(db, { messageId, analysis: { kind: "chat" } });
    assert.equal((await messageRow(db, messageId)).photo_text, READ);
  }));

test("отказ в той же транзакции не оставляет прочитанного", () =>
  withDatabase(async (db) => {
    const messageId = await photo(db);

    await assert.rejects(
      understand(db, {
        messageId,
        amend: { task_id: randomUUID(), fields: { needs_review: false }, reminders: [] },
        photoText: READ,
      }),
    );

    const row = await messageRow(db, messageId);
    assert.equal(row.photo_text, null);
    assert.equal(row.reply, null);
  }));

test("снимок, который не открылся, открытого вопроса не снимает", () =>
  withDatabase(async (db) => {
    const asked = await askedTask(db);
    const messageId = await photo(db);

    // Ни разбора, ни задачи, ни поправки — как «не расслышал» у голоса (§14.2).
    const taskId = await understand(db, { messageId, analysis: null, task: null });

    assert.equal(taskId, null);
    assert.equal(await openQuestion(db, asked), ASKED);
    assert.equal((await messageRow(db, messageId)).reply, "ответ бота");
  }));

test("снимок-ответ на вопрос дополняет ту же задачу и снимает вопрос", () =>
  withDatabase(async (db) => {
    const asked = await askedTask(db);
    const messageId = await photo(db);

    await understand(db, {
      messageId,
      amend: {
        task_id: asked,
        fields: { due_at: ERRAND.due_at, due_precision: "time", needs_review: false },
        reminders: [],
      },
      photoText: READ,
    });

    assert.equal(await openQuestion(db, asked), null);
    const { rows } = await db.query<{ due_at: Date; n: number }>(
      `select due_at, (select count(*)::int from public.tasks) as n
         from public.tasks where id = $1`,
      [asked],
    );
    assert.equal(rows[0]!.due_at.toISOString(), ERRAND.due_at);
    assert.equal(rows[0]!.n, 1);
    assert.equal((await messageRow(db, messageId)).photo_text, READ);
  }));

// --- Функция и доступ ---------------------------------------------------------

test("у функции одна перегрузка — с photo_text последним, — и зовёт её только service_role", () =>
  withDatabase(async (db) => {
    const { rows } = await db.query<{ oid: number; args: string }>(
      `select p.oid, pg_get_function_identity_arguments(p.oid) as args
         from pg_proc p join pg_namespace n on n.oid = p.pronamespace
        where n.nspname = 'public' and p.proname = 'record_understanding'`,
    );
    assert.equal(rows.length, 1);
    assert.match(rows[0]!.args, /edit jsonb, photo_text text$/);

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

test("приложение читает прочитанное только у своих сообщений", () =>
  withDatabase(async (db) => {
    const mine = await photo(db);
    await understand(db, { messageId: mine, analysis: { kind: "chat" }, photoText: READ });
    const strangers = await photo(db, "", STRANGER);
    await understand(db, {
      messageId: strangers,
      owner: STRANGER,
      analysis: { kind: "chat" },
      photoText: "чужое",
    });

    const seen = await asRole(db, "authenticated", OWNER, async () => {
      const { rows } = await db.query<{ photo_text: string | null }>(
        "select photo_text from public.messages where kind = 'photo'",
      );
      return rows.map((row) => row.photo_text);
    });

    assert.deepEqual(seen, [READ]);
  }));

// --- Миграция поверх старых строк ---------------------------------------------

test("миграция не трогает прежние сообщения: вид и текст на месте, прочитанного нет", () =>
  withDatabaseBefore(MIGRATION, async (db, migrate) => {
    const { rows } = await db.query<{ id: string }>(
      `select id from public.record_message($1, $2, 1, '', 'voice', 'voice-1', 32)
       union all
       select id from public.record_message($1, $2, 2, 'купить лампочку')`,
      [OWNER, OWNER],
    );

    await migrate();

    const voice = await messageRow(db, rows[0]!.id);
    const typed = await messageRow(db, rows[1]!.id);
    assert.equal(voice.kind, "voice");
    assert.equal(voice.duration_seconds, 32);
    assert.equal(voice.photo_text, null);
    assert.equal(typed.kind, "text");
    assert.equal(typed.text, "купить лампочку");
    assert.equal(typed.photo_text, null);
  }));
