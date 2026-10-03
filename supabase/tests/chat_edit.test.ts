/**
 * Правка задачи словом в чате на настоящем Postgres (этап 010).
 *
 * Бот зовёт `record_understanding` с `edit`, `pick_task` и `reopen_task`
 * ключом service-role — здесь это владелец базы: права проверяются
 * отдельно, по `has_function_privilege` и вызовами под `anon` и
 * `authenticated`. Готовый план напоминаний бот берёт у `reminder_plan` и
 * передаёт сам, поэтому сроки в тестах — любые, часы базы на план из чата
 * не влияют.
 *
 * Правила — `techspec/12-chat-edit.md` §12.3–12.6 и
 * `techspec/03-schema.md` §3.2–3.6.
 */
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { test } from "node:test";

import type { PGlite } from "@electric-sql/pglite";

import { asRole, withDatabase, withDatabaseBefore } from "./database.ts";

const OWNER = 777;
const STRANGER = 999;
const ASKED = "К какому сроку?";

type Json = null | boolean | number | string | Json[] | { [key: string]: Json };

interface TaskRow {
  id: string | null;
  title: string;
  kind: string;
  status: string;
  due_at: Date | null;
  due_precision: string | null;
  priority: string;
  promise: string | null;
  people: string[];
  needs_review: boolean;
  open_question: string | null;
  question_asked_at: Date | null;
  due_moved_at: Date | null;
  source_message_id: string | null;
  updated_at: Date;
}

interface ReminderRow {
  id: string;
  stage: string;
  fire_at: Date;
  sent_at: Date | null;
  telegram_message_id: string | number | null;
}

interface MessageRow {
  id: string;
  task_id: string | null;
  analysis: Json;
  reply: string | null;
}

/** Пятница, 4 октября 2030 года: 18:00 у владельца (UTC+5) — 13:00 UTC. */
const FRIDAY_DUE = "2030-10-04T13:00:00.000Z";
const FRIDAY_MORNING = "2030-10-04T04:00:00.000Z";
/** Понедельник, 7 октября 2030 года, 17:00 у владельца — 12:00 UTC. */
const MONDAY_FIVE = "2030-10-07T12:00:00.000Z";
const MONDAY_FOUR = "2030-10-07T11:00:00.000Z";
const MONDAY_DUE = "2030-10-07T13:00:00.000Z";
const MONDAY_MORNING = "2030-10-07T04:00:00.000Z";

const FRIDAY_PLAN = [
  { stage: "before", fire_at: FRIDAY_MORNING },
  { stage: "due", fire_at: FRIDAY_DUE },
];
const MONDAY_PLAN = [
  { stage: "before", fire_at: MONDAY_FOUR },
  { stage: "due", fire_at: MONDAY_FIVE },
];

/** Разбор модели, как его пишет бот; содержимое функции не важно. */
const ANALYSIS: Json = { kind: "task", title: "встреча с Ренатой" };

let telegramMessageId = 0;

function only<T>(rows: T[]): T {
  assert.equal(rows.length, 1, `ждали одну строку, пришло ${rows.length}`);
  return rows[0]!;
}

function iso(moment: Date | null | undefined): string | null {
  return moment === null || moment === undefined ? null : moment.toISOString();
}

function json(value: Json | undefined): string | null {
  return value === undefined || value === null ? null : JSON.stringify(value);
}

async function saveZone(db: PGlite, owner = OWNER): Promise<void> {
  await db.query("select public.save_owner_timezone($1, $2)", [owner, "Asia/Yekaterinburg"]);
}

/** Первый шаг приёма (§3.4): сообщение в базе, разбора ещё нет. */
async function message(db: PGlite, owner = OWNER): Promise<string> {
  telegramMessageId += 1;
  const { rows } = await db.query<{ id: string }>(
    "select id from public.record_message($1, $2, $3, $4)",
    [owner, owner, telegramMessageId, "сообщение"],
  );
  return only(rows).id;
}

interface Call {
  messageId: string;
  owner?: number;
  analysis?: Json;
  task?: Json;
  reminders?: Json[];
  facts?: Json[];
  amend?: Json;
  edit?: Json;
  reply?: string;
}

/** Второй шаг приёма — вызов, как его делает бот. Задачи нет — `null`. */
async function understand(db: PGlite, call: Call): Promise<TaskRow | null> {
  const { rows } = await db.query<TaskRow>(
    `select * from public.record_understanding(
       message_id => $1, owner_telegram_id => $2, analysis => $3::jsonb,
       ai_model => 'claude-opus-5', ai_input_tokens => 120, ai_output_tokens => 45,
       reply => $4, task => $5::jsonb, reminders => $6::jsonb, facts => $7::jsonb,
       transcript => null, transcript_confidence => null,
       amend => $8::jsonb, edit => $9::jsonb
     )`,
    [
      call.messageId,
      call.owner ?? OWNER,
      json(call.analysis === undefined ? ANALYSIS : call.analysis),
      call.reply ?? "ответ бота",
      json(call.task),
      json(call.reminders ?? []),
      json(call.facts ?? []),
      json(call.amend),
      json(call.edit),
    ],
  );
  const row = only(rows);
  return row.id === null ? null : row;
}

interface Seed {
  owner?: number;
  title?: string;
  status?: string;
  dueAt?: string | null;
  precision?: string | null;
  priority?: string;
  promise?: string | null;
  people?: string[];
  needsReview?: boolean;
  question?: string | null;
}

/** Задача, какой её оставил бот: по умолчанию — встреча в пятницу. */
async function seedTask(db: PGlite, seed: Seed = {}): Promise<string> {
  const { rows } = await db.query<{ id: string }>(
    `insert into public.tasks (
       owner_telegram_id, title, status, due_at, due_precision, priority,
       promise, people, needs_review, open_question, question_asked_at
     ) values (
       $1, $2, $3, $4::timestamptz, $5, $6, $7,
       array(select jsonb_array_elements_text($8::jsonb)), $9, $10,
       case when $10::text is null then null else now() end
     ) returning id`,
    [
      seed.owner ?? OWNER,
      seed.title ?? "встреча с Ренатой",
      seed.status ?? "active",
      seed.dueAt === undefined ? FRIDAY_DUE : seed.dueAt,
      seed.precision === undefined ? "day" : seed.precision,
      seed.priority ?? "normal",
      seed.promise ?? null,
      JSON.stringify(seed.people ?? []),
      seed.needsReview ?? false,
      seed.question ?? null,
    ],
  );
  return only(rows).id;
}

async function seedReminder(
  db: PGlite,
  taskId: string,
  stage: string,
  fireAt: string,
  sent: { at: string; messageId: number } | null = null,
  owner = OWNER,
): Promise<string> {
  const { rows } = await db.query<{ id: string }>(
    `insert into public.reminders (owner_telegram_id, task_id, stage, fire_at, sent_at, telegram_message_id)
     values ($1, $2, $3, $4, $5, $6) returning id`,
    [owner, taskId, stage, fireAt, sent?.at ?? null, sent?.messageId ?? null],
  );
  return only(rows).id;
}

/** Задача с напоминаниями на пятницу: оба ещё ждут. */
async function seedFriday(db: PGlite, seed: Seed = {}): Promise<string> {
  const id = await seedTask(db, seed);
  await seedReminder(db, id, "before", FRIDAY_MORNING, null, seed.owner ?? OWNER);
  await seedReminder(db, id, "due", FRIDAY_DUE, null, seed.owner ?? OWNER);
  return id;
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

async function remindersOf(db: PGlite, taskId: string): Promise<ReminderRow[]> {
  const { rows } = await db.query<ReminderRow>(
    "select id, stage, fire_at, sent_at, telegram_message_id from public.reminders where task_id = $1 order by fire_at",
    [taskId],
  );
  return rows;
}

function schedule(rows: ReminderRow[]): [string, string | null, boolean][] {
  return rows.map((row) => [row.stage, iso(row.fire_at), row.sent_at !== null]);
}

async function messageOf(db: PGlite, id: string): Promise<MessageRow> {
  const { rows } = await db.query<MessageRow>(
    "select id, task_id, analysis, reply from public.messages where id = $1",
    [id],
  );
  return only(rows);
}

async function factCount(db: PGlite, owner = OWNER): Promise<number> {
  const { rows } = await db.query<{ n: number }>(
    "select count(*)::int as n from public.facts where owner_telegram_id = $1",
    [owner],
  );
  return only(rows).n;
}

/** Правка словом: одно сообщение владельца с `edit`. */
async function editByWord(db: PGlite, edit: Json, owner = OWNER): Promise<{ row: TaskRow | null; messageId: string }> {
  const messageId = await message(db, owner);
  const row = await understand(db, { messageId, owner, edit });
  return { row, messageId };
}

async function pick(db: PGlite, messageId: string, edit: Json, reply: string, owner = OWNER): Promise<MessageRow> {
  const { rows } = await db.query<MessageRow>(
    "select id, task_id, analysis, reply from public.pick_task($1, $2::uuid, $3::jsonb, $4)",
    [owner, messageId, JSON.stringify(edit), reply],
  );
  return only(rows);
}

async function reopen(db: PGlite, taskId: string, plan: Json, owner = OWNER): Promise<TaskRow | null> {
  const { rows } = await db.query<TaskRow>("select * from public.reopen_task($1, $2::uuid, $3::jsonb)", [
    owner,
    taskId,
    json(plan),
  ]);
  const row = only(rows);
  return row.id === null ? null : row;
}

// --- Схема и права -----------------------------------------------------------

test("статус «убрана» разрешён, незнакомый — нет", () =>
  withDatabase(async (db) => {
    await seedTask(db, { status: "cancelled" });
    await assert.rejects(seedTask(db, { status: "deleted" }), /tasks_status_check/);
  }));

test("у каждой новой функции одна перегрузка; ядро — authenticated и service_role, остальное — только бот", () =>
  withDatabase(async (db) => {
    const expected: Record<string, Record<string, boolean>> = {
      "public.change_task(bigint, uuid, jsonb, jsonb)": { anon: false, authenticated: true, service_role: true },
      "public.edit_from_chat(bigint, jsonb)": { anon: false, authenticated: false, service_role: true },
      "public.pick_task(bigint, uuid, jsonb, text)": { anon: false, authenticated: false, service_role: true },
      "public.reopen_task(bigint, uuid, jsonb)": { anon: false, authenticated: false, service_role: true },
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
  }));

test("pick_task, reopen_task и правка из чата под anon и authenticated — отказ в праве", () =>
  withDatabase(async (db) => {
    const id = await seedTask(db, { status: "done" });
    const messageId = await message(db);
    const calls: [string, unknown[]][] = [
      ["select * from public.pick_task($1, $2::uuid, $3::jsonb, $4)", [OWNER, messageId, JSON.stringify({ task_id: id, action: "done" }), "x"]],
      ["select * from public.reopen_task($1, $2::uuid, $3::jsonb)", [OWNER, id, "[]"]],
      ["select * from public.edit_from_chat($1, $2::jsonb)", [OWNER, JSON.stringify({ task_id: id, action: "cancel" })]],
    ];

    for (const role of ["anon", "authenticated"] as const) {
      for (const [sql, params] of calls) {
        await asRole(db, role, OWNER, async () => {
          await assert.rejects(db.query(sql, params), /permission denied/, `${role}: ${sql}`);
        });
      }
    }
    // Ядро у anon права не имеет; под authenticated право есть — его зовёт
    // `edit_task`, — и что оно там может, проверяют тесты ниже.
    await asRole(db, "anon", null, async () => {
      await assert.rejects(
        db.query("select * from public.change_task($1, $2::uuid, $3::jsonb, null)", [OWNER, id, "{}"]),
        /permission denied/,
      );
    });

    assert.equal((await taskOf(db, id)).status, "done");
    assert.equal((await messageOf(db, messageId)).task_id, null);
  }));

test("ядро под токеном: чужой владелец в аргументе — пустой ответ, ничего не записано", () =>
  withDatabase(async (db) => {
    await saveZone(db, STRANGER);
    const strangers = await seedFriday(db, { owner: STRANGER });

    const row = await asRole(db, "authenticated", OWNER, async () => {
      const { rows } = await db.query<TaskRow>(
        "select * from public.change_task($1, $2::uuid, $3::jsonb, $4::jsonb)",
        [STRANGER, strangers, JSON.stringify({ title: "чужая", due_at: null }), "[]"],
      );
      return only(rows);
    });

    assert.equal(row.id, null);
    const untouched = await taskOf(db, strangers);
    assert.equal(untouched.title, "встреча с Ренатой");
    assert.equal(iso(untouched.due_at), FRIDAY_DUE);
    assert.equal((await remindersOf(db, strangers)).length, 2);
  }));

test("ядро под токеном: свой владелец и чужая задача — пустой ответ", () =>
  withDatabase(async (db) => {
    await saveZone(db);
    const strangers = await seedTask(db, { owner: STRANGER });

    const row = await asRole(db, "authenticated", OWNER, async () => {
      const { rows } = await db.query<TaskRow>(
        "select * from public.change_task($1, $2::uuid, $3::jsonb, null)",
        [OWNER, strangers, JSON.stringify({ title: "чужая" })],
      );
      return only(rows);
    });

    assert.equal(row.id, null);
    assert.equal((await taskOf(db, strangers)).title, "встреча с Ренатой");
  }));

test("ядро под токеном: готовый план отброшен — расписание считает база и ставит отметку, как edit_task", () =>
  withDatabase(async (db) => {
    await saveZone(db);
    const id = await seedFriday(db);

    const saved = await asRole(db, "authenticated", OWNER, async () => {
      const { rows } = await db.query<TaskRow>(
        "select * from public.change_task($1, $2::uuid, $3::jsonb, $4::jsonb)",
        [
          OWNER,
          id,
          JSON.stringify({ due_date: "2030-10-07" }),
          JSON.stringify([{ stage: "due", fire_at: "2030-01-01T00:00:00Z" }]),
        ],
      );
      return only(rows);
    });

    assert.equal(saved.id, id);
    assert.equal(iso(saved.due_at), MONDAY_DUE);
    assert.ok(saved.due_moved_at, "путь приложения ставит отметку переноса");
    assert.deepEqual(schedule(await remindersOf(db, id)), [
      ["before", MONDAY_MORNING, false],
      ["due", MONDAY_DUE, false],
    ]);
  }));

// --- Правка словом: поля ----------------------------------------------------

test("перенос: срок, ровно переданный план, пометка и вопрос сняты, новой задачи нет, отметки нет", () =>
  withDatabase(async (db) => {
    const id = await seedFriday(db, { needsReview: true, question: ASKED });

    const { row, messageId } = await editByWord(db, {
      task_id: id,
      action: "change",
      changes: { due_at: "2030-10-07T17:00:00+05:00" },
      schedule: MONDAY_PLAN,
      question: null,
    });

    assert.equal(row?.id, id);
    assert.equal(await taskCount(db), 1);
    const saved = await taskOf(db, id);
    assert.equal(iso(saved.due_at), MONDAY_FIVE);
    assert.equal(saved.due_precision, "time");
    assert.equal(saved.needs_review, false);
    assert.equal(saved.open_question, null);
    assert.equal(saved.question_asked_at, null);
    // Ответ на сообщение уже сказал «Перенёс» — минутный цикл второй строки не пишет.
    assert.equal(saved.due_moved_at, null);
    const { rows: moved } = await db.query("select * from public.moved_tasks($1)", [OWNER]);
    assert.deepEqual(moved, []);
    assert.deepEqual(schedule(await remindersOf(db, id)), [
      ["before", MONDAY_FOUR, false],
      ["due", MONDAY_FIVE, false],
    ]);
    const saidAbout = await messageOf(db, messageId);
    assert.equal(saidAbout.task_id, id);
    assert.equal(saidAbout.reply, "ответ бота");
  }));

test("перенос на день — 18:00 в поясе владельца; ушедшая ступень взводится заново", () =>
  withDatabase(async (db) => {
    await saveZone(db);
    const id = await seedTask(db);
    await seedReminder(db, id, "before", FRIDAY_MORNING, { at: "2030-10-04T04:00:05Z", messageId: 41 });
    await seedReminder(db, id, "due", FRIDAY_DUE);

    await editByWord(db, {
      task_id: id,
      action: "change",
      changes: { due_date: "2030-10-07" },
      schedule: [
        { stage: "before", fire_at: MONDAY_MORNING },
        { stage: "due", fire_at: MONDAY_DUE },
      ],
    });

    const saved = await taskOf(db, id);
    assert.equal(iso(saved.due_at), MONDAY_DUE);
    assert.equal(saved.due_precision, "day");
    const [before, due] = await remindersOf(db, id);
    assert.equal(iso(before?.fire_at), MONDAY_MORNING);
    assert.equal(before?.sent_at, null);
    assert.equal(before?.telegram_message_id, null);
    assert.equal(iso(due?.fire_at), MONDAY_DUE);
  }));

test("перенос в прошлое: план пуст — неотправленные уходят, новых нет", () =>
  withDatabase(async (db) => {
    const id = await seedFriday(db);

    await editByWord(db, {
      task_id: id,
      action: "change",
      changes: { due_at: "2020-10-07T17:00:00+05:00" },
      schedule: [],
    });

    assert.equal(iso((await taskOf(db, id)).due_at), "2020-10-07T12:00:00.000Z");
    assert.deepEqual(await remindersOf(db, id), []);
  }));

test("правка сути, срочности, обещания и людей напоминания не трогает, люди заменяются", () =>
  withDatabase(async (db) => {
    const id = await seedFriday(db, { people: ["Кузнецов", "Рената"] });
    const before = await remindersOf(db, id);

    await editByWord(db, {
      task_id: id,
      action: "change",
      changes: { title: "встреча с Петровым", priority: "high", promise: "mine", people: ["Петров"] },
      schedule: [],
    });

    const saved = await taskOf(db, id);
    assert.equal(saved.title, "встреча с Петровым");
    assert.equal(saved.priority, "high");
    assert.equal(saved.promise, "mine");
    assert.deepEqual(saved.people, ["Петров"]);
    assert.equal(iso(saved.due_at), FRIDAY_DUE);
    assert.deepEqual(await remindersOf(db, id), before);
  }));

test("снятие срока: неотправленные уходят, ушедшее остаётся следом", () =>
  withDatabase(async (db) => {
    const id = await seedTask(db);
    await seedReminder(db, id, "before", FRIDAY_MORNING, { at: "2030-10-04T04:00:05Z", messageId: 41 });
    await seedReminder(db, id, "due", FRIDAY_DUE);

    await editByWord(db, { task_id: id, action: "change", changes: { due_at: null }, schedule: [] });

    const saved = await taskOf(db, id);
    assert.equal(saved.due_at, null);
    assert.equal(saved.due_precision, null);
    assert.deepEqual(schedule(await remindersOf(db, id)), [["before", FRIDAY_MORNING, true]]);
  }));

// --- Правка словом: часть дня (§21.2) ------------------------------------------

/** Понедельник, 7 октября 2030 года, 08:00 у владельца — начало утра, 03:00 UTC. */
const MONDAY_EARLY = "2030-10-07T03:00:00.000Z";

test("перенос на часть дня: начало части, часть и одно напоминание по плану бота", () =>
  withDatabase(async (db) => {
    const id = await seedFriday(db);

    await editByWord(db, {
      task_id: id,
      action: "change",
      changes: { due_at: "2030-10-07T08:00:00+05:00", due_precision: "morning" },
      schedule: [{ stage: "due", fire_at: MONDAY_EARLY }],
    });

    const saved = await taskOf(db, id);
    assert.equal(iso(saved.due_at), MONDAY_EARLY);
    assert.equal(saved.due_precision, "morning");
    assert.deepEqual(schedule(await remindersOf(db, id)), [["due", MONDAY_EARLY, false]]);
  }));

test("та же минута, другая точность — перенос: вечер и 18:00 со временем не одно и то же", () =>
  withDatabase(async (db) => {
    const id = await seedTask(db, { dueAt: "2030-10-07T13:00:00.000Z", precision: "time" });

    await editByWord(db, {
      task_id: id,
      action: "change",
      changes: { due_at: "2030-10-07T18:00:00+05:00", due_precision: "evening" },
      schedule: [{ stage: "due", fire_at: MONDAY_DUE }],
    });

    const saved = await taskOf(db, id);
    assert.equal(iso(saved.due_at), MONDAY_DUE);
    assert.equal(saved.due_precision, "evening");
    assert.deepEqual(schedule(await remindersOf(db, id)), [["due", MONDAY_DUE, false]]);
  }));

test("точность time — то же, что срок без ключа", () =>
  withDatabase(async (db) => {
    const id = await seedFriday(db);

    await editByWord(db, {
      task_id: id,
      action: "change",
      changes: { due_at: "2030-10-07T17:00:00+05:00", due_precision: "time" },
      schedule: MONDAY_PLAN,
    });

    const saved = await taskOf(db, id);
    assert.equal(iso(saved.due_at), MONDAY_FIVE);
    assert.equal(saved.due_precision, "time");
  }));

test("точность без непустого срока, с днём или не из пяти — правка отклонена целиком", () =>
  withDatabase(async (db) => {
    await saveZone(db);
    const id = await seedFriday(db, { title: "встреча с Ренатой" });
    const before = await taskOf(db, id);
    const refused: [Json, RegExp][] = [
      [{ due_precision: "morning", title: "встреча с Петровым" }, /due_precision needs a due_at/],
      [{ due_at: null, due_precision: "evening" }, /due_precision needs a due_at/],
      [{ due_date: "2030-10-07", due_precision: "morning" }, /due_precision goes with due_at/],
      [{ due_at: "2030-10-07T12:00:00+05:00", due_precision: "noon" }, /invalid due_precision/],
      [{ due_at: "2030-10-07T18:00:00+05:00", due_precision: "day" }, /invalid due_precision/],
      [{ due_at: "2030-10-07T18:00:00+05:00", due_precision: null }, /invalid due_precision/],
    ];

    for (const [changes, reason] of refused) {
      const messageId = await message(db);
      await assert.rejects(
        understand(db, { messageId, edit: { task_id: id, action: "change", changes, schedule: [] } }),
        reason,
        JSON.stringify(changes),
      );
      assert.equal((await messageOf(db, messageId)).reply, null);
    }

    assert.deepEqual(await taskOf(db, id), before);
    assert.equal((await remindersOf(db, id)).length, 2);
  }));

test("повтор делу с частью дня — правило без часа, как у дела на день; этот раз остаётся частью", () =>
  withDatabase(async (db) => {
    await saveZone(db);
    const id = await seedTask(db, { dueAt: "2030-10-07T08:00:00+05:00", precision: "morning" });

    await editByWord(db, {
      task_id: id,
      action: "change",
      changes: { repeat: { every: "week", interval: 1, weekdays: [1], month_day: null, month: null } },
      schedule: [],
    });

    const { rows } = await db.query<{ repeat: Json; due_precision: string; occurrence_at: Date }>(
      "select repeat, due_precision, occurrence_at from public.tasks where id = $1",
      [id],
    );
    const saved = only(rows);
    assert.deepEqual(saved.repeat, {
      every: "week",
      interval: 1,
      weekdays: [1],
      month_day: null,
      month: null,
      time: null,
    });
    assert.equal(saved.due_precision, "morning");
    assert.equal(iso(saved.occurrence_at), MONDAY_EARLY);
  }));

// --- Правка словом: закрыть, убрать, вопрос, «менять нечего» ------------------

test("«сделал» закрывает задачу: неотправленные уходят, ушедшее остаётся", () =>
  withDatabase(async (db) => {
    const id = await seedTask(db);
    await seedReminder(db, id, "before", FRIDAY_MORNING, { at: "2030-10-04T04:00:05Z", messageId: 41 });
    await seedReminder(db, id, "due", FRIDAY_DUE);

    const { row, messageId } = await editByWord(db, { task_id: id, action: "done" });

    assert.equal(row?.status, "done");
    assert.equal((await taskOf(db, id)).status, "done");
    assert.deepEqual(schedule(await remindersOf(db, id)), [["before", FRIDAY_MORNING, true]]);
    assert.equal((await messageOf(db, messageId)).task_id, id);
  }));

test("«отменилась» убирает задачу: статус cancelled, строка в базе, напоминаний нет", () =>
  withDatabase(async (db) => {
    const id = await seedFriday(db);

    const { row, messageId } = await editByWord(db, { task_id: id, action: "cancel" });

    assert.equal(row?.status, "cancelled");
    assert.equal((await taskOf(db, id)).status, "cancelled");
    assert.deepEqual(await remindersOf(db, id), []);
    assert.equal((await messageOf(db, messageId)).task_id, id);
    // Список приложения — активные задачи владельца: убранной там нет.
    const { rows: listed } = await asRole(db, "authenticated", OWNER, () =>
      db.query("select id from public.tasks where status = 'active'"),
    );
    assert.deepEqual(listed, []);
  }));

test("вопрос по правке: поля не меняются, пометка и вопрос у задачи, вопрос другой задачи снят", () =>
  withDatabase(async (db) => {
    const other = await seedTask(db, { title: "позвонить маме", question: "Когда?" });
    const id = await seedFriday(db);

    const { row, messageId } = await editByWord(db, {
      task_id: id,
      action: "change",
      changes: {},
      question: `  ${ASKED}  `,
    });

    assert.equal(row?.id, id);
    const saved = await taskOf(db, id);
    assert.equal(saved.needs_review, true);
    assert.equal(saved.open_question, ASKED);
    assert.ok(saved.question_asked_at);
    assert.equal(iso(saved.due_at), FRIDAY_DUE);
    assert.equal((await remindersOf(db, id)).length, 2);
    assert.equal((await taskOf(db, other)).open_question, null);
    assert.equal((await messageOf(db, messageId)).task_id, id);

    // Ответ на вопрос дополняет задачу путём этапа 008 (§10.2).
    const answer = await message(db);
    const amended = await understand(db, {
      messageId: answer,
      amend: {
        task_id: id,
        fields: { due_at: MONDAY_FIVE, due_precision: "time", needs_review: false },
        reminders: MONDAY_PLAN,
      },
    });
    assert.equal(amended?.id, id);
    const answered = await taskOf(db, id);
    assert.equal(iso(answered.due_at), MONDAY_FIVE);
    assert.equal(answered.open_question, null);
    assert.equal((await messageOf(db, answer)).task_id, id);
  }));

test("«менять нечего»: задача не меняется, но сообщение — о ней", () =>
  withDatabase(async (db) => {
    const id = await seedFriday(db, { needsReview: true });
    const before = await taskOf(db, id);

    const { row, messageId } = await editByWord(db, { task_id: id, action: "change", changes: {}, question: null });

    assert.equal(row?.id, id);
    assert.deepEqual(await taskOf(db, id), before);
    assert.equal((await remindersOf(db, id)).length, 2);
    assert.equal((await messageOf(db, messageId)).task_id, id);
  }));

test("незнакомое действие — отказ целиком", () =>
  withDatabase(async (db) => {
    const id = await seedTask(db);
    const messageId = await message(db);

    await assert.rejects(
      understand(db, { messageId, edit: { task_id: id, action: "delete" } }),
      /unknown action delete/,
    );

    assert.equal((await taskOf(db, id)).status, "active");
    assert.equal((await messageOf(db, messageId)).reply, null);
  }));

// --- Отказ: задачу закрыли, убрали или удалили, пока модель думала -----------

/** Правка, которую база обязана отвергнуть целиком: ни разбора, ни памяти, ни снятого вопроса. */
async function assertEditRefused(db: PGlite, edit: Json): Promise<void> {
  const asked = await seedTask(db, { title: "позвонить маме", question: ASKED });
  const messageId = await message(db);

  await assert.rejects(
    understand(db, {
      messageId,
      edit,
      facts: [{ category: "work", text: "Встречается с клиентами", status: "guess" }],
    }),
    /record_understanding: task .* is not an active task of 777/,
  );

  const saved = await messageOf(db, messageId);
  assert.deepEqual([saved.analysis, saved.reply, saved.task_id], [null, null, null]);
  assert.equal(await factCount(db), 0);
  assert.equal((await taskOf(db, asked)).open_question, ASKED);
}

for (const status of ["done", "cancelled"]) {
  test(`правка ${status}-задачи — отказ и откат всей транзакции`, () =>
    withDatabase(async (db) => {
      const id = await seedTask(db, { status });
      await seedReminder(db, id, "due", FRIDAY_DUE, { at: "2030-10-04T13:00:05Z", messageId: 42 });

      for (const edit of [
        { task_id: id, action: "change", changes: { due_at: MONDAY_FIVE }, schedule: MONDAY_PLAN },
        { task_id: id, action: "done" },
        { task_id: id, action: "cancel" },
        { task_id: id, action: "change", changes: {}, question: ASKED },
        { task_id: id, action: "change", changes: {} },
      ] as Json[]) {
        await assertEditRefused(db, edit);
      }

      const untouched = await taskOf(db, id);
      assert.equal(untouched.status, status);
      assert.equal(iso(untouched.due_at), FRIDAY_DUE);
      assert.equal(untouched.open_question, null);
      assert.deepEqual(schedule(await remindersOf(db, id)), [["due", FRIDAY_DUE, true]]);
    }));
}

test("правка удалённой задачи — отказ и откат", () =>
  withDatabase(async (db) => {
    await assertEditRefused(db, { task_id: randomUUID(), action: "done" });
  }));

test("правка чужой задачи — отказ, чужая не тронута", () =>
  withDatabase(async (db) => {
    const strangers = await seedFriday(db, { owner: STRANGER });

    await assertEditRefused(db, { task_id: strangers, action: "cancel" });
    await assertEditRefused(db, {
      task_id: strangers,
      action: "change",
      changes: { title: "чужая" },
      schedule: [],
    });

    const untouched = await taskOf(db, strangers);
    assert.equal(untouched.status, "active");
    assert.equal(untouched.title, "встреча с Ренатой");
    assert.equal((await remindersOf(db, strangers)).length, 2);
  }));

// --- Повтор и ссылка сообщения на задачу -------------------------------------

test("повтор обновления не правит второй раз", () =>
  withDatabase(async (db) => {
    const id = await seedFriday(db);
    const messageId = await message(db);
    const edit = {
      task_id: id,
      action: "change",
      changes: { due_at: "2030-10-07T17:00:00+05:00" },
      schedule: MONDAY_PLAN,
    };
    await understand(db, { messageId, edit });
    const first = await remindersOf(db, id);

    const again = await understand(db, {
      messageId,
      edit: { ...edit, changes: { due_at: null }, schedule: [] },
    });

    assert.equal(again?.id, id);
    assert.equal(iso((await taskOf(db, id)).due_at), MONDAY_FIVE);
    assert.deepEqual(await remindersOf(db, id), first);
  }));

test("повтор закрытия отдаёт задачу, а не отказ", () =>
  withDatabase(async (db) => {
    const id = await seedFriday(db);
    const messageId = await message(db);
    await understand(db, { messageId, edit: { task_id: id, action: "done" } });

    const again = await understand(db, { messageId, edit: { task_id: id, action: "done" } });

    assert.equal(again?.id, id);
    assert.equal(again?.status, "done");
  }));

test("новая задача и ответ на вопрос тоже пишут ссылку сообщения на задачу", () =>
  withDatabase(async (db) => {
    const first = await message(db);
    const created = await understand(db, {
      messageId: first,
      task: { title: "встреча с Ренатой", kind: "task", people: [], open_question: ASKED },
    });
    assert.ok(created?.id);
    assert.equal((await messageOf(db, first)).task_id, created.id);

    const answer = await message(db);
    await understand(db, {
      messageId: answer,
      amend: { task_id: created.id, fields: { needs_review: false }, reminders: [] },
    });
    assert.equal((await messageOf(db, answer)).task_id, created.id);

    const chat = await message(db);
    await understand(db, { messageId: chat, analysis: { kind: "chat" } });
    assert.equal((await messageOf(db, chat)).task_id, null);
  }));

test("у сообщений до миграции ссылка — на задачу, заведённую из них", () =>
  withDatabaseBefore("20260929100000_chat_edit.sql", async (db, migrate) => {
    const { rows } = await db.query<{ id: string }>(
      `insert into public.messages (owner_telegram_id, chat_id, telegram_message_id, text)
       values ($1, $1, 1, 'встреча с Ренатой'), ($1, $1, 2, 'как дела?')
       returning id`,
      [OWNER],
    );
    const [withTask, chat] = rows.map((row) => row.id);
    const { rows: tasks } = await db.query<{ id: string }>(
      `insert into public.tasks (owner_telegram_id, title, source_message_id)
       values ($1, 'встреча с Ренатой', $2) returning id`,
      [OWNER, withTask],
    );

    await migrate();

    assert.equal((await messageOf(db, withTask!)).task_id, only(tasks).id);
    assert.equal((await messageOf(db, chat!)).task_id, null);
  }));

test("удаление задачи из приложения обнуляет ссылку, сообщение остаётся", () =>
  withDatabase(async (db) => {
    const id = await seedFriday(db);
    const { messageId } = await editByWord(db, { task_id: id, action: "change", changes: {} });

    await asRole(db, "authenticated", OWNER, () => db.query("delete from public.tasks where id = $1", [id]));

    assert.equal(await taskCount(db), 0);
    const kept = await messageOf(db, messageId);
    assert.equal(kept.task_id, null);
    assert.equal(kept.reply, "ответ бота");
  }));

// --- Выбор кнопкой ------------------------------------------------------------

/** Сообщение, по которому бот спросил «Какую задачу…?»: разбор записан, задачи нет. */
async function askedWhich(db: PGlite): Promise<string> {
  const messageId = await message(db);
  const row = await understand(db, { messageId, reply: "Какую задачу закрыть?" });
  assert.equal(row, null);
  return messageId;
}

test("выбор кнопкой пишет правку, ссылку и ответ; повтор и другая кнопка второй правки не пишут", () =>
  withDatabase(async (db) => {
    const first = await seedFriday(db);
    const second = await seedFriday(db, { title: "встреча с Ренатой в офисе" });
    const messageId = await askedWhich(db);

    const picked = await pick(db, messageId, { task_id: first, action: "done" }, "Закрыл: встреча с Ренатой.");

    assert.equal(picked.task_id, first);
    assert.equal(picked.reply, "Закрыл: встреча с Ренатой.");
    assert.equal((await taskOf(db, first)).status, "done");
    assert.deepEqual(await remindersOf(db, first), []);

    const twice = await pick(db, messageId, { task_id: first, action: "done" }, "Закрыл: встреча с Ренатой.");
    const other = await pick(db, messageId, { task_id: second, action: "done" }, "Закрыл: встреча в офисе.");

    for (const row of [twice, other]) {
      assert.equal(row.task_id, first);
      assert.equal(row.reply, "Закрыл: встреча с Ренатой.");
    }
    assert.equal((await taskOf(db, second)).status, "active");
    assert.equal((await remindersOf(db, second)).length, 2);
  }));

test("выбор кнопкой переносит выбранную задачу по готовому плану", () =>
  withDatabase(async (db) => {
    const id = await seedFriday(db);
    const messageId = await askedWhich(db);

    const picked = await pick(
      db,
      messageId,
      { task_id: id, action: "change", changes: { due_at: "2030-10-07T17:00:00+05:00" }, schedule: MONDAY_PLAN },
      "Перенёс: встреча с Ренатой.",
    );

    assert.equal(picked.task_id, id);
    assert.equal(iso((await taskOf(db, id)).due_at), MONDAY_FIVE);
    assert.equal((await taskOf(db, id)).due_moved_at, null);
    assert.deepEqual(schedule(await remindersOf(db, id)), [
      ["before", MONDAY_FOUR, false],
      ["due", MONDAY_FIVE, false],
    ]);
  }));

test("выбранную задачу уже закрыли или удалили — ничего не записано, вопрос других не тронут", () =>
  withDatabase(async (db) => {
    const closed = await seedTask(db, { status: "done" });
    const messageId = await askedWhich(db);
    const asked = await seedTask(db, { title: "позвонить маме", question: ASKED });

    for (const edit of [
      { task_id: closed, action: "cancel" },
      { task_id: closed, action: "change", changes: {}, question: "Когда?" },
      { task_id: randomUUID(), action: "done" },
    ] as Json[]) {
      const row = await pick(db, messageId, edit, "Убрал из списка: встреча с Ренатой.");
      assert.equal(row.task_id, null);
      assert.equal(row.reply, "Какую задачу закрыть?");
    }

    assert.equal((await taskOf(db, closed)).status, "done");
    assert.equal((await taskOf(db, asked)).open_question, ASKED);
  }));

test("выбор кнопкой по чужому сообщению — отказ", () =>
  withDatabase(async (db) => {
    const id = await seedTask(db);
    const strangers = await message(db, STRANGER);

    await assert.rejects(
      pick(db, strangers, { task_id: id, action: "done" }, "Закрыл."),
      /pick_task: message .* is not owned by 777/,
    );
    await assert.rejects(pick(db, randomUUID(), { task_id: id, action: "done" }, "Закрыл."), /not owned/);
    assert.equal((await taskOf(db, id)).status, "active");
  }));

// --- «Вернуть» ------------------------------------------------------------------

for (const status of ["done", "cancelled"]) {
  test(`«Вернуть» ${status}-задачу: снова активна, план записан, ушедшая ступень взведена`, () =>
    withDatabase(async (db) => {
      const id = await seedTask(db, { status });
      await seedReminder(db, id, "before", FRIDAY_MORNING, { at: "2030-10-04T04:00:05Z", messageId: 41 });

      const row = await reopen(db, id, FRIDAY_PLAN);

      assert.equal(row?.id, id);
      assert.equal(row?.status, "active");
      assert.equal((await taskOf(db, id)).status, "active");
      const reminders = await remindersOf(db, id);
      assert.deepEqual(schedule(reminders), [
        ["before", FRIDAY_MORNING, false],
        ["due", FRIDAY_DUE, false],
      ]);
      assert.equal(reminders[0]?.telegram_message_id, null);
    }));
}

test("«Вернуть» с пустым планом — активна без напоминаний", () =>
  withDatabase(async (db) => {
    const id = await seedTask(db, { status: "done", dueAt: "2020-10-02T13:00:00Z" });

    const row = await reopen(db, id, []);

    assert.equal(row?.status, "active");
    assert.deepEqual(await remindersOf(db, id), []);
  }));

test("«Вернуть» активную задачу — как есть, без записи", () =>
  withDatabase(async (db) => {
    const id = await seedFriday(db);
    const before = await taskOf(db, id);
    const reminders = await remindersOf(db, id);

    const row = await reopen(db, id, MONDAY_PLAN);

    assert.equal(row?.id, id);
    assert.deepEqual(await taskOf(db, id), before);
    assert.deepEqual(await remindersOf(db, id), reminders);
  }));

test("«Вернуть» удалённую или чужую задачу — пустой ответ", () =>
  withDatabase(async (db) => {
    const strangers = await seedTask(db, { owner: STRANGER, status: "done" });

    assert.equal(await reopen(db, randomUUID(), FRIDAY_PLAN), null);
    assert.equal(await reopen(db, strangers, FRIDAY_PLAN), null);
    assert.equal((await taskOf(db, strangers)).status, "done");
  }));

test("«Вернуть» с планом не списком — отказ", () =>
  withDatabase(async (db) => {
    const id = await seedTask(db, { status: "done" });

    await assert.rejects(reopen(db, id, { stage: "due" }), /schedule must be an array/);
    assert.equal((await taskOf(db, id)).status, "done");
  }));

// --- «Сделано» на убранной задаче ------------------------------------------------

test("«Сделано» под напоминанием убранной задачи — как есть, статус не меняется", () =>
  withDatabase(async (db) => {
    const id = await seedTask(db, { status: "cancelled" });
    await seedReminder(db, id, "due", FRIDAY_DUE, { at: "2030-10-04T13:00:05Z", messageId: 42 });
    const before = await taskOf(db, id);

    const { rows } = await db.query<TaskRow>("select * from public.mark_task_done($1, $2::uuid)", [OWNER, id]);

    assert.equal(only(rows).status, "cancelled");
    assert.deepEqual(await taskOf(db, id), before);
    assert.equal((await remindersOf(db, id)).length, 1);
  }));

test("«Сделано» в приложении на убранной задаче — как есть, статус не меняется", () =>
  withDatabase(async (db) => {
    const id = await seedTask(db, { status: "cancelled" });
    const before = await taskOf(db, id);

    const row = await asRole(db, "authenticated", OWNER, async () => {
      const { rows } = await db.query<TaskRow>("select * from public.complete_task($1::uuid)", [id]);
      return only(rows);
    });

    assert.equal(row.status, "cancelled");
    assert.deepEqual(await taskOf(db, id), before);
  }));
