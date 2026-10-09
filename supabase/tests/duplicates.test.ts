/**
 * Дубли на настоящем Postgres (этап 013): `same_task` в
 * `record_understanding`, кнопка «Записать отдельно» (`record_separately`)
 * и общая для них внутренняя вставка задачи `insert_message_task`.
 *
 * Бот зовёт функции ключом service-role — здесь это владелец базы; права
 * проверяются по `has_function_privilege`, вызовами под `anon` и
 * `authenticated` и одним вызовом под `service_role` целиком: вложенная
 * вставка тоже проверяет право на исполнение. Правила —
 * `techspec/15-duplicates.md` §15.3, §15.4, §15.6 и
 * `techspec/03-schema.md` §3.4.
 */
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { test } from "node:test";

import type { PGlite } from "@electric-sql/pglite";

import { asRole, messageRow, withDatabase } from "./database.ts";

const OWNER = 777;
const STRANGER = 999;
const ASKED = "К какому сроку?";

/** Среда, 7 октября 2026 года, 18:30 у владельца (UTC+5) — 13:30 UTC. */
const MEETING_AT = "2026-10-07T13:30:00.000Z";
const MEETING_MORNING = "2026-10-07T04:00:00.000Z";

type Json = null | boolean | number | string | Json[] | { [key: string]: Json };

interface TaskRow {
  id: string | null;
  title: string;
  kind: string;
  status: string;
  due_at: Date | null;
  due_precision: string | null;
  priority: string;
  people: string[];
  needs_review: boolean;
  open_question: string | null;
  question_asked_at: Date | null;
  source_message_id: string | null;
  repeat: Json;
  occurrence_at: Date | null;
}

interface SeparateRow {
  id: string;
  task_id: string | null;
  reply: string | null;
}

const MEETING = {
  title: "родительское собрание",
  kind: "task",
  due_at: MEETING_AT,
  due_precision: "time",
  priority: "normal",
  promise: null,
  people: [],
  needs_review: false,
} satisfies Json;

const PLAN = [
  { stage: "before", fire_at: MEETING_MORNING },
  { stage: "due", fire_at: MEETING_AT },
] satisfies Json;

const FACT = [{ category: "family", text: "Сын учится в 5 «Б»", status: "fact" }] satisfies Json;

let telegramMessageId = 0;

function only<T>(rows: T[]): T {
  assert.equal(rows.length, 1, `ждали одну строку, пришло ${rows.length}`);
  return rows[0]!;
}

function json(value: Json | undefined): string | null {
  return value === undefined || value === null ? null : JSON.stringify(value);
}

/** Первый шаг приёма (§3.4): сообщение в базе, разбора ещё нет. */
async function message(db: PGlite, owner = OWNER): Promise<string> {
  telegramMessageId += 1;
  const { rows } = await db.query<{ id: string }>(
    "select id from public.record_message($1, $2, $3, $4)",
    [owner, owner, telegramMessageId, "собрание в школе 7 октября в 18:30"],
  );
  return only(rows).id;
}

interface Call {
  messageId: string;
  owner?: number;
  task?: Json;
  reminders?: Json;
  facts?: Json;
  amend?: Json;
  edit?: Json;
  /** `undefined` — аргумента нет вовсе, как у прежнего вызова бота. */
  sameTask?: string | null;
  reply?: string;
}

/** Второй шаг — вызов, как его делает бот; `same_task` — только если он есть. */
async function understand(db: PGlite, call: Call): Promise<TaskRow | null> {
  const params: unknown[] = [
    call.messageId,
    call.owner ?? OWNER,
    JSON.stringify({ kind: "task", title: "родительское собрание", same_as: 1 }),
    call.reply ?? "Это уже записано: родительское собрание.",
    json(call.task),
    json(call.reminders ?? []),
    json(call.facts ?? []),
    json(call.amend),
    json(call.edit),
  ];
  let same = "";
  if (call.sameTask !== undefined) {
    params.push(call.sameTask);
    same = ", same_task => $10::uuid";
  }
  const { rows } = await db.query<TaskRow>(
    `select * from public.record_understanding(
       message_id => $1, owner_telegram_id => $2, analysis => $3::jsonb,
       ai_model => 'claude-opus-5', ai_input_tokens => 120, ai_output_tokens => 45,
       reply => $4,
       -- Дело сообщения — одно, номер 1 (§23.6).
       tasks => case when $5::jsonb is null then null
                  else jsonb_build_array(jsonb_build_object('item', 1, 'task', $5::jsonb, 'reminders', $6::jsonb)) end,
       facts => $7::jsonb,
       transcript => null, transcript_confidence => null,
       amend => $8::jsonb, edit => $9::jsonb${same}
     )`,
    params,
  );
  return rows.length === 0 ? null : only(rows);
}

/** Нажатие «Записать отдельно» — вызов, как его делает бот: прежняя кнопка, дело номер 1. */
async function separately(
  db: PGlite,
  messageId: string,
  task: Json,
  reply = "Записал: родительское собрание.",
  reminders: Json = PLAN,
  owner = OWNER,
): Promise<SeparateRow> {
  const { rows } = await db.query<SeparateRow>(
    "select id, task_id, reply from public.record_separately($1, $2::uuid, $3::jsonb, $4::jsonb, $5, 1::smallint)",
    [owner, messageId, JSON.stringify(task), JSON.stringify(reminders), reply],
  );
  return only(rows);
}

/** Задача, записанная обычным путём: сообщение, разбор, задача. */
async function recorded(db: PGlite, task: Json = MEETING, owner = OWNER): Promise<string> {
  const row = await understand(db, {
    messageId: await message(db, owner),
    owner,
    task,
    reminders: PLAN,
    reply: "Записал: родительское собрание.",
  });
  assert.ok(row?.id, "задача не заведена");
  return row.id;
}

/** Задача с открытым вопросом «К какому сроку?» (§10.1). */
async function askedTask(db: PGlite): Promise<string> {
  return recorded(db, {
    ...MEETING,
    title: "позвонить в банк",
    due_at: null,
    due_precision: null,
    open_question: ASKED,
  });
}

/** Сообщение-дубль задачи `found`: разбор записан, задачи по нему нет. */
async function duplicate(db: PGlite, found: string): Promise<string> {
  const messageId = await message(db);
  await understand(db, { messageId, sameTask: found });
  return messageId;
}

async function taskOf(db: PGlite, id: string): Promise<TaskRow> {
  return only((await db.query<TaskRow>("select * from public.tasks where id = $1", [id])).rows);
}

async function taskCount(db: PGlite, owner = OWNER): Promise<number> {
  const { rows } = await db.query<{ n: number }>(
    "select count(*)::int as n from public.tasks where owner_telegram_id = $1",
    [owner],
  );
  return only(rows).n;
}

async function reminderCount(db: PGlite, taskId: string): Promise<number> {
  const { rows } = await db.query<{ n: number }>(
    "select count(*)::int as n from public.reminders where task_id = $1",
    [taskId],
  );
  return only(rows).n;
}

async function factCount(db: PGlite): Promise<number> {
  const { rows } = await db.query<{ n: number }>(
    "select count(*)::int as n from public.facts where owner_telegram_id = $1",
    [OWNER],
  );
  return only(rows).n;
}

async function setStatus(db: PGlite, id: string, status: string): Promise<void> {
  await db.query("update public.tasks set status = $2 where id = $1", [id, status]);
}

// --- same_task в record_understanding -----------------------------------------

test("дубль не заводит задачу: сообщение ведёт на найденную, разбор, ответ и память записаны", () =>
  withDatabase(async (db) => {
    const found = await recorded(db);
    const messageId = await message(db);

    const row = await understand(db, { messageId, sameTask: found, facts: FACT });

    assert.equal(row?.id, found);
    assert.equal(await taskCount(db), 1);
    const saved = await messageRow(db, messageId);
    assert.equal(saved.task_id, found);
    assert.equal(saved.reply, "Это уже записано: родительское собрание.");
    assert.deepEqual(saved.analysis, { kind: "task", title: "родительское собрание", same_as: 1 });
    assert.equal(await factCount(db), 1);
    // Напоминания найденной задачи прежние.
    assert.equal(await reminderCount(db, found), 2);
  }));

test("дубль снимает открытый вопрос, как любая запись с разбором", () =>
  withDatabase(async (db) => {
    const found = await recorded(db);
    const asked = await askedTask(db);
    assert.equal((await taskOf(db, asked)).open_question, ASKED);

    await duplicate(db, found);

    const after = await taskOf(db, asked);
    assert.equal(after.open_question, null);
    assert.equal(after.question_asked_at, null);
  }));

test("повтор дубля отдаёт ту же задачу и ничего не заводит", () =>
  withDatabase(async (db) => {
    const found = await recorded(db);
    const messageId = await duplicate(db, found);

    const again = await understand(db, { messageId, sameTask: found });

    assert.equal(again?.id, found);
    assert.equal(await taskCount(db), 1);
  }));

for (const status of ["done", "cancelled"]) {
  test(`дубль задачи со статусом ${status} — отказ, ничего не записано`, () =>
    withDatabase(async (db) => {
      const found = await recorded(db);
      await setStatus(db, found, status);
      const messageId = await message(db);

      await assert.rejects(understand(db, { messageId, sameTask: found, facts: FACT }), /not an active task/);

      const saved = await messageRow(db, messageId);
      assert.equal(saved.reply, null);
      assert.equal(saved.task_id, null);
      assert.equal(await factCount(db), 0);
    }));
}

test("дубль чужой или несуществующей задачи — отказ", () =>
  withDatabase(async (db) => {
    const strangers = await recorded(db, MEETING, STRANGER);

    for (const sameTask of [strangers, randomUUID()]) {
      const messageId = await message(db);
      await assert.rejects(understand(db, { messageId, sameTask }), /not an active task/);
      assert.equal((await messageRow(db, messageId)).task_id, null);
    }
  }));

test("same_task вместе с task, amend или edit — отказ до всякой записи", () =>
  withDatabase(async (db) => {
    const found = await recorded(db);
    const mixes: Omit<Call, "messageId">[] = [
      { task: MEETING },
      { amend: { task_id: found, fields: {}, reminders: [] } },
      { edit: { task_id: found, action: "done" } },
    ];

    for (const mix of mixes) {
      const messageId = await message(db);
      await assert.rejects(understand(db, { ...mix, messageId, sameTask: found }), /same_task/);
      assert.equal((await messageRow(db, messageId)).reply, null);
    }
    assert.equal(await taskCount(db), 1);
    assert.equal((await taskOf(db, found)).status, "active");
  }));

test("вызов без same_task работает как раньше", () =>
  withDatabase(async (db) => {
    const id = await recorded(db);

    const row = await taskOf(db, id);
    assert.equal(row.title, "родительское собрание");
    assert.equal(row.due_at?.toISOString(), MEETING_AT);
    assert.equal(await reminderCount(db, id), 2);
  }));

// --- record_separately --------------------------------------------------------

test("«Записать отдельно» заводит задачу с напоминаниями и переводит на неё сообщение", () =>
  withDatabase(async (db) => {
    const found = await recorded(db);
    const messageId = await duplicate(db, found);

    const saved = await separately(db, messageId, { ...MEETING, people: ["Рената"] });

    assert.equal(saved.id, messageId);
    assert.ok(saved.task_id);
    assert.notEqual(saved.task_id, found);
    assert.equal(saved.reply, "Записал: родительское собрание.");
    const task = await taskOf(db, saved.task_id);
    assert.equal(task.source_message_id, messageId);
    assert.equal(task.due_at?.toISOString(), MEETING_AT);
    assert.deepEqual(task.people, ["Рената"]);
    assert.equal(await reminderCount(db, saved.task_id), 2);
    assert.equal((await messageRow(db, messageId)).task_id, saved.task_id);
    // Найденная задача не тронута.
    assert.equal((await taskOf(db, found)).status, "active");
    assert.equal(await taskCount(db), 2);
  }));

test("второе нажатие ничего не пишет и отдаёт записанный ответ", () =>
  withDatabase(async (db) => {
    const found = await recorded(db);
    const messageId = await duplicate(db, found);

    const first = await separately(db, messageId, MEETING);
    const second = await separately(db, messageId, { ...MEETING, title: "другое" }, "Записал: другое.");

    assert.equal(second.task_id, first.task_id);
    assert.equal(second.reply, first.reply);
    assert.equal(await taskCount(db), 2);
    assert.equal(await reminderCount(db, first.task_id!), 2);
  }));

test("задача с вопросом снимает прежний открытый вопрос: открыт один", () =>
  withDatabase(async (db) => {
    const found = await recorded(db);
    const messageId = await duplicate(db, found);
    const asked = await askedTask(db);

    const saved = await separately(
      db,
      messageId,
      { ...MEETING, due_at: null, due_precision: null, needs_review: true, open_question: "Во сколько?" },
      "Записал: родительское собрание. Во сколько?",
      [],
    );

    const fresh = await taskOf(db, saved.task_id!);
    assert.equal(fresh.open_question, "Во сколько?");
    assert.ok(fresh.question_asked_at);
    assert.equal((await taskOf(db, asked)).open_question, null);
  }));

test("задача без вопроса чужих вопросов не снимает", () =>
  withDatabase(async (db) => {
    const found = await recorded(db);
    const messageId = await duplicate(db, found);
    const asked = await askedTask(db);

    await separately(db, messageId, MEETING);

    assert.equal((await taskOf(db, asked)).open_question, ASKED);
  }));

test("повторяющаяся задача получает правило в поясе владельца", () =>
  withDatabase(async (db) => {
    await db.query("select public.save_owner_timezone($1, $2)", [OWNER, "Asia/Yekaterinburg"]);
    const messageId = await message(db);

    const saved = await separately(db, messageId, {
      ...MEETING,
      repeat: { every: "week", interval: 1, weekdays: [3], month_day: null, month: null },
    });

    const task = await taskOf(db, saved.task_id!);
    assert.equal(task.occurrence_at?.toISOString(), MEETING_AT);
    assert.equal((task.repeat as { time: string }).time, "18:30");
  }));

test("сообщение чужое или его нет — отказ, ничего не записано", () =>
  withDatabase(async (db) => {
    const strangers = await message(db, STRANGER);

    for (const messageId of [strangers, randomUUID()]) {
      await assert.rejects(separately(db, messageId, MEETING), /record_separately: message .* is not owned by 777/);
    }
    assert.equal(await taskCount(db), 0);
    assert.equal(await taskCount(db, STRANGER), 0);
  }));

// --- Права ---------------------------------------------------------------------

test("у каждой функции одна перегрузка, и зовёт их только ключ бота", () =>
  withDatabase(async (db) => {
    const bot = { anon: false, authenticated: false, service_role: true };
    const expected: Record<string, Record<string, boolean>> = {
      "public.record_separately(bigint, uuid, jsonb, jsonb, text, smallint)": bot,
      // Вложенный вызов проверяет право вызывающего: без `service_role` ключ
      // бота не записал бы ни одной задачи.
      "public.insert_message_task(bigint, uuid, jsonb, jsonb, smallint)": bot,
    };
    for (const [signature, rights] of Object.entries(expected)) {
      const name = signature.slice("public.".length, signature.indexOf("("));
      const { rows } = await db.query<{ n: number }>(
        "select count(*)::int as n from pg_proc where proname = $1 and pronamespace = 'public'::regnamespace",
        [name],
      );
      assert.equal(only(rows).n, 1, `перегрузки ${name}`);

      const granted = await db.query<{ role: string; allowed: boolean }>(
        `select role, has_function_privilege(role, $1, 'execute') as allowed
           from unnest(array['anon', 'authenticated', 'service_role']) as role`,
        [signature],
      );
      assert.deepEqual(
        Object.fromEntries(granted.rows.map((row) => [row.role, row.allowed])),
        rights,
        `права ${name}`,
      );
    }

    // Две перегрузки PostgREST различать нечем (§3.4): прежняя удалена.
    const { rows } = await db.query<{ oid: number; args: string }>(
      `select p.oid, pg_get_function_identity_arguments(p.oid) as args
         from pg_proc p join pg_namespace n on n.oid = p.pronamespace
        where n.nspname = 'public' and p.proname = 'record_understanding'`,
    );
    const understanding = only(rows);
    assert.match(understanding.args, /photo_text text, same_task uuid, spheres jsonb$/);
    const granted = await db.query<{ role: string; allowed: boolean }>(
      `select role, has_function_privilege(role, $1::oid, 'execute') as allowed
         from unnest(array['anon', 'authenticated', 'service_role']) as role`,
      [understanding.oid],
    );
    assert.deepEqual(Object.fromEntries(granted.rows.map((row) => [row.role, row.allowed])), bot);
  }));

test("record_separately и внутренняя вставка под anon и authenticated — отказ в праве", () =>
  withDatabase(async (db) => {
    const messageId = await message(db);
    const calls: [string, unknown[]][] = [
      [
        "select * from public.record_separately($1, $2::uuid, $3::jsonb, $4::jsonb, $5, 1::smallint)",
        [OWNER, messageId, JSON.stringify(MEETING), "[]", "x"],
      ],
      [
        "select * from public.insert_message_task($1, $2::uuid, $3::jsonb, $4::jsonb, 1::smallint)",
        [OWNER, messageId, JSON.stringify(MEETING), "[]"],
      ],
    ];

    for (const role of ["anon", "authenticated"] as const) {
      for (const [sql, params] of calls) {
        await asRole(db, role, OWNER, async () => {
          await assert.rejects(db.query(sql, params), /permission denied/, `${role}: ${sql}`);
        });
      }
    }
    assert.equal(await taskCount(db), 0);
  }));

test("под ключом бота обе записи проходят целиком, вместе с вложенной вставкой", () =>
  withDatabase(async (db) => {
    const first = await message(db);
    const second = await message(db);
    await db.exec("set role service_role");
    try {
      const row = await understand(db, { messageId: first, task: MEETING, reminders: PLAN });
      assert.ok(row?.id);
      await understand(db, { messageId: second, sameTask: row.id });
      const saved = await separately(db, second, MEETING);
      assert.ok(saved.task_id);
    } finally {
      await db.exec("reset role");
    }
    assert.equal(await taskCount(db), 2);
  }));
