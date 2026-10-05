/**
 * Повторяющиеся задачи на настоящем Postgres (этап 011).
 *
 * Правило и раз — колонки `tasks.repeat` и `tasks.occurrence_at`; следующий
 * раз считает чистая `repeat_next`, переход «Сделано» — одно ядро на
 * кнопку, приложение и слово, пропущенный раз двигает `roll_repeats`.
 * Правила — `techspec/13-repeat.md` §13.2–13.5 и `techspec/03-schema.md`
 * §3.3–3.6.
 *
 * Бот зовёт функции ключом service-role — здесь это владелец базы; права
 * проверяются по `has_function_privilege` и вызовами под `anon` и
 * `authenticated`. Моменты сверяются строками ISO в UTC.
 */
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { test } from "node:test";

import type { PGlite } from "@electric-sql/pglite";

import { asRole, withDatabase } from "./database.ts";

const OWNER = 777;
const STRANGER = 999;

/** Пояс владельца в тестах: UTC+5, без перехода на летнее время. */
const YEKATERINBURG = "Asia/Yekaterinburg";
/** Пояс с переходом: 30 марта 2031 года часы уходят с +01:00 на +02:00. */
const BERLIN = "Europe/Berlin";

type Json = null | boolean | number | string | Json[] | { [key: string]: Json };

interface Rule {
  every: string;
  interval: number;
  weekdays?: number[] | null;
  month_day?: number | null;
  month?: number | null;
  time?: string | null;
}

interface TaskRow {
  id: string | null;
  title: string;
  kind: string;
  status: string;
  due_at: Date | null;
  due_precision: string | null;
  repeat: Rule | null;
  occurrence_at: Date | null;
  needs_review: boolean;
  open_question: string | null;
  due_moved_at: Date | null;
}

interface ReminderRow {
  stage: string;
  fire_at: Date;
  sent_at: Date | null;
  telegram_message_id: string | number | null;
}

const MONDAYS: Rule = { every: "week", interval: 1, weekdays: [1] };
const DAILY: Rule = { every: "day", interval: 1 };

/** Понедельник, 7 октября 2030 года, 10:00 у владельца. */
const MONDAY_TEN = "2030-10-07T10:00:00+05:00";
/** Следующий понедельник, 14 октября, 10:00. */
const NEXT_MONDAY_TEN = "2030-10-14T10:00:00+05:00";
/** Пятница, 4 октября 2030 года, 18:00 — срок днём. */
const FRIDAY_DAY = "2030-10-04T18:00:00+05:00";

let telegramMessageId = 0;

function only<T>(rows: T[]): T {
  assert.equal(rows.length, 1, `ждали одну строку, пришло ${rows.length}`);
  return rows[0]!;
}

function iso(moment: Date | null | undefined): string | null {
  return moment === null || moment === undefined ? null : moment.toISOString();
}

/** Момент строкой ISO в UTC — как его вернёт `Date.toISOString()`. */
function utc(moment: string): string {
  return new Date(moment).toISOString();
}

/** Раз в секундах Unix — как его несёт кнопка. */
function seconds(moment: string): number {
  return Math.floor(new Date(moment).getTime() / 1000);
}

function json(value: Json | Rule | undefined): string | null {
  return value === undefined || value === null ? null : JSON.stringify(value);
}

async function saveZone(db: PGlite, owner = OWNER, zone = YEKATERINBURG): Promise<void> {
  await db.query("select public.save_owner_timezone($1, $2)", [owner, zone]);
}

async function next(db: PGlite, rule: Rule, occurrence: string, after: string, zone = YEKATERINBURG): Promise<string | null> {
  const { rows } = await db.query<{ at: Date | null }>(
    "select public.repeat_next($1::jsonb, $2::timestamptz, $3::timestamptz, $4) as at",
    [JSON.stringify(rule), occurrence, after, zone],
  );
  return iso(only(rows).at);
}

async function valid(db: PGlite, rule: unknown): Promise<boolean> {
  const { rows } = await db.query<{ ok: boolean }>("select public.repeat_valid($1::jsonb) as ok", [
    rule === null ? null : JSON.stringify(rule),
  ]);
  return only(rows).ok;
}

interface Seed {
  owner?: number;
  title?: string;
  status?: string;
  kind?: string;
  dueAt?: string | null;
  precision?: string | null;
  repeat?: Rule | null;
  occurrence?: string | null;
  needsReview?: boolean;
  question?: string | null;
}

/** Задача, какой её оставил бот; по умолчанию — повтор по понедельникам в 10:00. */
async function seedTask(db: PGlite, seed: Seed = {}): Promise<string> {
  const repeat = seed.repeat === undefined ? { ...MONDAYS, time: "10:00" } : seed.repeat;
  const dueAt = seed.dueAt === undefined ? MONDAY_TEN : seed.dueAt;
  const { rows } = await db.query<{ id: string }>(
    `insert into public.tasks (
       owner_telegram_id, title, status, kind, due_at, due_precision, repeat, occurrence_at,
       needs_review, open_question, question_asked_at
     ) values ($1, $2, $3, $4, $5::timestamptz, $6, $7::jsonb, $8::timestamptz, $9, $10,
               case when $10::text is null then null else now() end)
     returning id`,
    [
      seed.owner ?? OWNER,
      seed.title ?? "планёрка",
      seed.status ?? "active",
      seed.kind ?? "task",
      dueAt,
      seed.precision === undefined ? (repeat?.time ? "time" : "day") : seed.precision,
      json(repeat),
      seed.occurrence === undefined ? (repeat === null ? null : dueAt) : seed.occurrence,
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
): Promise<void> {
  await db.query(
    `insert into public.reminders (owner_telegram_id, task_id, stage, fire_at, sent_at, telegram_message_id)
     values ($1, $2, $3, $4, $5, $6)`,
    [owner, taskId, stage, fireAt, sent?.at ?? null, sent?.messageId ?? null],
  );
}

/** Планёрка в понедельник 10:00: «за час» ушла, «к сроку» ждёт. */
async function seedMonday(db: PGlite, seed: Seed = {}): Promise<string> {
  const owner = seed.owner ?? OWNER;
  const id = await seedTask(db, seed);
  await seedReminder(db, id, "before", "2030-10-07T09:00:00+05:00", { at: "2030-10-07T04:00:05Z", messageId: 41 }, owner);
  await seedReminder(db, id, "due", MONDAY_TEN, null, owner);
  return id;
}

async function taskOf(db: PGlite, id: string): Promise<TaskRow> {
  return only((await db.query<TaskRow>("select * from public.tasks where id = $1", [id])).rows);
}

async function remindersOf(db: PGlite, taskId: string): Promise<ReminderRow[]> {
  const { rows } = await db.query<ReminderRow>(
    "select stage, fire_at, sent_at, telegram_message_id from public.reminders where task_id = $1 order by fire_at",
    [taskId],
  );
  return rows;
}

function schedule(rows: ReminderRow[]): [string, string | null, boolean][] {
  return rows.map((row) => [row.stage, iso(row.fire_at), row.sent_at !== null]);
}

/** Раз задачи: срок, раз и точность. */
function occurrenceOf(row: TaskRow): [string | null, string | null, string | null] {
  return [iso(row.due_at), iso(row.occurrence_at), row.due_precision];
}

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
  task?: Json;
  reminders?: Json[];
  amend?: Json;
  edit?: Json;
}

/** Второй шаг приёма — вызов, как его делает бот. Задачи нет — `null`. */
async function understand(db: PGlite, call: Call): Promise<TaskRow | null> {
  const { rows } = await db.query<TaskRow>(
    `select * from public.record_understanding(
       message_id => $1, owner_telegram_id => $2, analysis => $3::jsonb,
       ai_model => 'claude-opus-5', ai_input_tokens => 120, ai_output_tokens => 45,
       reply => 'ответ бота',
       -- Дело сообщения — одно, номер 1 (§23.6).
       tasks => case when $4::jsonb is null then null
                  else jsonb_build_array(jsonb_build_object('item', 1, 'task', $4::jsonb, 'reminders', $5::jsonb)) end,
       facts => '[]'::jsonb,
       amend => $6::jsonb, edit => $7::jsonb
     )`,
    [
      call.messageId,
      OWNER,
      JSON.stringify({ kind: "task", title: "планёрка" }),
      json(call.task),
      json(call.reminders ?? []),
      json(call.amend),
      json(call.edit),
    ],
  );
  return rows.length === 0 ? null : only(rows);
}

async function analysisOf(db: PGlite, messageId: string): Promise<Json> {
  const { rows } = await db.query<{ analysis: Json }>("select analysis from public.messages where id = $1", [messageId]);
  return only(rows).analysis;
}

async function markDone(db: PGlite, id: string, occurrence?: number | null): Promise<TaskRow | null> {
  const { rows } =
    occurrence === undefined
      ? await db.query<TaskRow>("select * from public.mark_task_done($1, $2::uuid)", [OWNER, id])
      : await db.query<TaskRow>("select * from public.mark_task_done($1, $2::uuid, $3)", [OWNER, id, occurrence]);
  const row = only(rows);
  return row.id === null ? null : row;
}

async function roll(db: PGlite, now: string, owner = OWNER): Promise<TaskRow[]> {
  const { rows } = await db.query<TaskRow>("select * from public.roll_repeats($1, $2::timestamptz)", [owner, now]);
  return rows;
}

async function giveBack(db: PGlite, id: string, backTo: string, movedFrom: string, plan: Json[] = []): Promise<TaskRow | null> {
  const { rows } = await db.query<TaskRow>(
    "select * from public.return_occurrence($1, $2::uuid, $3, $4, $5::jsonb)",
    [OWNER, id, seconds(backTo), seconds(movedFrom), JSON.stringify(plan)],
  );
  const row = only(rows);
  return row.id === null ? null : row;
}

async function change(db: PGlite, id: string, changes: Json | Record<string, unknown>, plan: Json[] | null = []): Promise<TaskRow | null> {
  const { rows } = await db.query<TaskRow>(
    "select * from public.change_task($1, $2::uuid, $3::jsonb, $4::jsonb)",
    [OWNER, id, JSON.stringify(changes), plan === null ? null : JSON.stringify(plan)],
  );
  const row = only(rows);
  return row.id === null ? null : row;
}

// --- repeat_next ---------------------------------------------------------------

test("repeat_next: каждое 31-е — последний день короткого месяца и снова 31-е", () =>
  withDatabase(async (db) => {
    const rule: Rule = { every: "month", interval: 1, month_day: 31 };
    const cases: [occurrence: string, expected: string][] = [
      ["2031-03-31T18:00:00+05:00", "2031-04-30T18:00:00+05:00"],
      ["2031-01-31T18:00:00+05:00", "2031-02-28T18:00:00+05:00"],
      ["2032-01-31T18:00:00+05:00", "2032-02-29T18:00:00+05:00"],
      ["2031-02-28T18:00:00+05:00", "2031-03-31T18:00:00+05:00"],
      ["2031-04-30T18:00:00+05:00", "2031-05-31T18:00:00+05:00"],
    ];
    for (const [occurrence, expected] of cases) {
      assert.equal(await next(db, rule, occurrence, occurrence), utc(expected), occurrence);
    }
  }));

test("repeat_next: последний день месяца — последний день каждого месяца", () =>
  withDatabase(async (db) => {
    const rule: Rule = { every: "month", interval: 1, month_day: -1 };
    const cases: [occurrence: string, expected: string][] = [
      ["2031-01-31T18:00:00+05:00", "2031-02-28T18:00:00+05:00"],
      ["2031-02-28T18:00:00+05:00", "2031-03-31T18:00:00+05:00"],
      ["2031-03-31T18:00:00+05:00", "2031-04-30T18:00:00+05:00"],
      ["2032-01-31T18:00:00+05:00", "2032-02-29T18:00:00+05:00"],
    ];
    for (const [occurrence, expected] of cases) {
      assert.equal(await next(db, rule, occurrence, occurrence), utc(expected), occurrence);
    }
  }));

test("repeat_next: 29 февраля каждый год — 28-е в невисокосный", () =>
  withDatabase(async (db) => {
    const rule: Rule = { every: "year", interval: 1, month: 2, month_day: 29 };
    const cases: [occurrence: string, expected: string][] = [
      ["2032-02-29T18:00:00+05:00", "2033-02-28T18:00:00+05:00"],
      ["2033-02-28T18:00:00+05:00", "2034-02-28T18:00:00+05:00"],
      ["2035-02-28T18:00:00+05:00", "2036-02-29T18:00:00+05:00"],
    ];
    for (const [occurrence, expected] of cases) {
      assert.equal(await next(db, rule, occurrence, occurrence), utc(expected), occurrence);
    }
  }));

test("repeat_next: недели, дни, месяцы и годы с шагом", () =>
  withDatabase(async (db) => {
    const weekdays: Rule = { every: "week", interval: 1, weekdays: [1, 2, 3, 4, 5] };
    const fortnightFriday: Rule = { every: "week", interval: 2, weekdays: [5] };
    const cases: [label: string, rule: Rule, occurrence: string, expected: string][] = [
      ["по будням после пятницы", weekdays, FRIDAY_DAY, "2030-10-07T18:00:00+05:00"],
      ["по будням в среду", weekdays, "2030-10-02T18:00:00+05:00", "2030-10-03T18:00:00+05:00"],
      ["по выходным в субботу", { every: "week", interval: 1, weekdays: [6, 7] }, "2030-10-05T18:00:00+05:00", "2030-10-06T18:00:00+05:00"],
      ["по пн и пт в понедельник", { every: "week", interval: 1, weekdays: [1, 5] }, "2030-10-07T18:00:00+05:00", "2030-10-11T18:00:00+05:00"],
      ["раз в две недели по пятницам", fortnightFriday, FRIDAY_DAY, "2030-10-18T18:00:00+05:00"],
      ["через границу года", fortnightFriday, "2030-12-27T18:00:00+05:00", "2031-01-10T18:00:00+05:00"],
      ["каждые 2 недели по вт и чт в четверг", { every: "week", interval: 2, weekdays: [2, 4] }, "2030-10-03T18:00:00+05:00", "2030-10-15T18:00:00+05:00"],
      ["каждые 3 дня", { every: "day", interval: 3 }, FRIDAY_DAY, "2030-10-07T18:00:00+05:00"],
      ["каждый день через месяц", DAILY, "2030-10-31T18:00:00+05:00", "2030-11-01T18:00:00+05:00"],
      ["каждые 3 месяца 5-го", { every: "month", interval: 3, month_day: 5 }, "2030-11-05T18:00:00+05:00", "2031-02-05T18:00:00+05:00"],
      ["каждый месяц 10-го", { every: "month", interval: 1, month_day: 10 }, "2030-12-10T18:00:00+05:00", "2031-01-10T18:00:00+05:00"],
      ["каждый год 5 марта", { every: "year", interval: 1, month: 3, month_day: 5 }, "2031-03-05T18:00:00+05:00", "2032-03-05T18:00:00+05:00"],
    ];
    for (const [label, rule, occurrence, expected] of cases) {
      assert.equal(await next(db, rule, occurrence, occurrence), utc(expected), label);
    }
  }));

test("repeat_next: час — из правила, без часа — 18:00", () =>
  withDatabase(async (db) => {
    const atTen: Rule = { ...MONDAYS, time: "10:00" };
    assert.equal(await next(db, atTen, MONDAY_TEN, MONDAY_TEN), utc(NEXT_MONDAY_TEN));
    // Раз сдвинули на 12:00, а серия — в 10:00: следующий в час серии.
    const noon = "2030-10-07T12:00:00+05:00";
    assert.equal(await next(db, atTen, noon, noon), utc(NEXT_MONDAY_TEN));
    assert.equal(await next(db, MONDAYS, MONDAY_TEN, MONDAY_TEN), utc("2030-10-14T18:00:00+05:00"));
  }));

test("repeat_next: в поясе с переходом на летнее время час остаётся", () =>
  withDatabase(async (db) => {
    const saturday = "2031-03-29T09:00:00+01:00";
    assert.equal(await next(db, { ...DAILY, time: "09:00" }, saturday, saturday, BERLIN), utc("2031-03-30T09:00:00+02:00"));
    assert.equal(
      await next(db, { every: "week", interval: 1, weekdays: [6], time: "09:00" }, saturday, saturday, BERLIN),
      utc("2031-04-05T09:00:00+02:00"),
    );
  }));

test("repeat_next: первый раз вне правила — ближайший раз по правилу", () =>
  withDatabase(async (db) => {
    // Среда при «по понедельникам».
    const wednesday = "2030-10-02T18:00:00+05:00";
    assert.equal(await next(db, MONDAYS, wednesday, wednesday), utc("2030-10-07T18:00:00+05:00"));
    // 29-е при «каждый месяц 10-го».
    const twentyNinth = "2030-09-29T18:00:00+05:00";
    assert.equal(
      await next(db, { every: "month", interval: 1, month_day: 10 }, twentyNinth, twentyNinth),
      utc("2030-10-10T18:00:00+05:00"),
    );
  }));

test("repeat_next: всегда строго позже after, и after далеко от раза", () =>
  withDatabase(async (db) => {
    assert.equal(await next(db, DAILY, FRIDAY_DAY, "2030-10-07T18:00:00+05:00"), utc("2030-10-08T18:00:00+05:00"));
    assert.equal(await next(db, DAILY, FRIDAY_DAY, "2030-10-07T17:59:00+05:00"), utc("2030-10-07T18:00:00+05:00"));
    assert.equal(await next(db, DAILY, "2030-01-01T18:00:00+05:00", "2030-10-04T12:00:00+05:00"), utc(FRIDAY_DAY));
    assert.equal(
      await next(db, { every: "week", interval: 2, weekdays: [5] }, FRIDAY_DAY, "2031-06-01T00:00:00+05:00"),
      utc("2031-06-13T18:00:00+05:00"),
    );
    assert.equal(
      await next(db, { every: "year", interval: 1, month: 3, month_day: 5 }, "2031-03-05T18:00:00+05:00", "2040-01-01T00:00:00+05:00"),
      utc("2040-03-05T18:00:00+05:00"),
    );
  }));

// --- repeat_valid и проверки таблицы -----------------------------------------------

test("repeat_valid: каждое правило формы проходит", () =>
  withDatabase(async (db) => {
    const rules: unknown[] = [
      null,
      DAILY,
      { every: "day", interval: 2, weekdays: null, month_day: null, month: null, time: null },
      { every: "day", interval: 99, time: "00:30" },
      { every: "week", interval: 1, weekdays: [1, 2, 3, 4, 5] },
      { every: "week", interval: 2, weekdays: [6, 7], time: "09:00" },
      { every: "month", interval: 1, month_day: 10 },
      { every: "month", interval: 3, month_day: -1, time: "23:59" },
      { every: "year", interval: 1, month: 3, month_day: 5 },
      { every: "year", interval: 1, month: 2, month_day: 29 },
    ];
    for (const rule of rules) {
      assert.equal(await valid(db, rule), true, JSON.stringify(rule));
    }
  }));

test("repeat_valid: не по форме — отказ", () =>
  withDatabase(async (db) => {
    const rules: unknown[] = [
      [],
      "day",
      {},
      { every: "hour", interval: 1 },
      { every: "day" },
      { every: "day", interval: 0 },
      { every: "day", interval: 100 },
      { every: "day", interval: 1.5 },
      { every: "day", interval: "2" },
      { every: "day", interval: 1, until: "2030-12-01" },
      { every: "day", interval: 1, weekdays: [1] },
      { every: "day", interval: 1, month_day: 5 },
      { every: "week", interval: 1 },
      { every: "week", interval: 1, weekdays: [] },
      { every: "week", interval: 1, weekdays: [0] },
      { every: "week", interval: 1, weekdays: [8] },
      { every: "week", interval: 1, weekdays: [1, 1] },
      { every: "week", interval: 1, weekdays: ["1"] },
      { every: "week", interval: 1, weekdays: [1], month_day: 3 },
      { every: "month", interval: 1 },
      { every: "month", interval: 1, month_day: 0 },
      { every: "month", interval: 1, month_day: 32 },
      { every: "month", interval: 1, month_day: -2 },
      { every: "month", interval: 1, month_day: 10, month: 3 },
      { every: "month", interval: 1, month_day: 10, weekdays: [1] },
      { every: "year", interval: 1, month_day: 5 },
      { every: "year", interval: 1, month: 3 },
      { every: "year", interval: 1, month: 13, month_day: 5 },
      { every: "year", interval: 1, month: 3, month_day: -1 },
      { every: "year", interval: 1, month: 2, month_day: 30 },
      { every: "year", interval: 1, month: 4, month_day: 31 },
      { every: "day", interval: 1, time: "9:00" },
      { every: "day", interval: 1, time: "24:00" },
      { every: "day", interval: 1, time: 900 },
    ];
    for (const rule of rules) {
      assert.equal(await valid(db, rule), false, JSON.stringify(rule));
    }
  }));

test("таблица: правило без раза, без срока, у идеи и не по форме — отказ", () =>
  withDatabase(async (db) => {
    await seedTask(db);
    await seedTask(db, { repeat: null });
    await assert.rejects(seedTask(db, { occurrence: null }), /tasks_repeat_occurrence/);
    await assert.rejects(seedTask(db, { repeat: null, occurrence: MONDAY_TEN }), /tasks_repeat_occurrence/);
    await assert.rejects(seedTask(db, { dueAt: null, precision: null, occurrence: MONDAY_TEN }), /tasks_repeat_needs_due/);
    await assert.rejects(seedTask(db, { kind: "idea" }), /tasks_repeat_needs_due/);
    await assert.rejects(seedTask(db, { repeat: { every: "week", interval: 1, weekdays: [] } }), /tasks_repeat_valid/);
  }));

test("чтения бота: созревшие напоминания и «Перенёс» отдают правило и раз", () =>
  withDatabase(async (db) => {
    const id = await seedMonday(db);
    await db.query("update public.tasks set due_moved_at = now() where id = $1", [id]);

    const { rows: due } = await db.query<{ task_id: string; repeat: Rule; occurrence_at: Date }>(
      "select task_id, repeat, occurrence_at from public.due_reminders($1, $2::timestamptz)",
      [OWNER, MONDAY_TEN],
    );
    assert.equal(only(due).task_id, id);
    assert.deepEqual(only(due).repeat, { ...MONDAYS, time: "10:00" });
    assert.equal(iso(only(due).occurrence_at), utc(MONDAY_TEN));

    const { rows: moved } = await db.query<{ id: string; repeat: Rule; occurrence_at: Date }>(
      "select id, repeat, occurrence_at from public.moved_tasks($1)",
      [OWNER],
    );
    assert.equal(only(moved).id, id);
    assert.deepEqual(only(moved).repeat, { ...MONDAYS, time: "10:00" });
    assert.equal(iso(only(moved).occurrence_at), utc(MONDAY_TEN));
  }));

// --- «Сделано»: кнопка ------------------------------------------------------------

const NEXT_MONDAY_SCHEDULE: [string, string, boolean][] = [
  ["before", utc("2030-10-14T09:00:00+05:00"), false],
  ["due", utc(NEXT_MONDAY_TEN), false],
];

test("кнопка со своим разом: задача на следующем разе, ступени взведены заново, пометка и вопрос на месте", () =>
  withDatabase(async (db) => {
    await saveZone(db);
    const id = await seedMonday(db, { needsReview: true, question: "Где планёрка?" });

    const row = await markDone(db, id, seconds(MONDAY_TEN));

    assert.equal(row?.id, id);
    const saved = await taskOf(db, id);
    assert.equal(saved.status, "active");
    assert.deepEqual(occurrenceOf(saved), [utc(NEXT_MONDAY_TEN), utc(NEXT_MONDAY_TEN), "time"]);
    assert.deepEqual(saved.repeat, { ...MONDAYS, time: "10:00" });
    assert.equal(saved.needs_review, true);
    assert.equal(saved.open_question, "Где планёрка?");
    assert.equal(saved.due_moved_at, null);
    const reminders = await remindersOf(db, id);
    assert.deepEqual(schedule(reminders), NEXT_MONDAY_SCHEDULE);
    assert.ok(reminders.every((reminder) => reminder.telegram_message_id === null));
  }));

test("второе нажатие и кнопка прошлого раза — задача не перескакивает", () =>
  withDatabase(async (db) => {
    await saveZone(db);
    const id = await seedMonday(db);

    await markDone(db, id, seconds(MONDAY_TEN));
    const after = await taskOf(db, id);
    const reminders = await remindersOf(db, id);

    const again = await markDone(db, id, seconds(MONDAY_TEN));

    assert.deepEqual(occurrenceOf(again!), [utc(NEXT_MONDAY_TEN), utc(NEXT_MONDAY_TEN), "time"]);
    assert.deepEqual(await taskOf(db, id), after);
    assert.deepEqual(await remindersOf(db, id), reminders);
  }));

test("кнопка без раза (напоминание до этапа) переводит текущий раз", () =>
  withDatabase(async (db) => {
    await saveZone(db);
    const id = await seedMonday(db);

    const row = await markDone(db, id);

    assert.equal(row?.status, "active");
    assert.deepEqual(occurrenceOf(await taskOf(db, id)), [utc(NEXT_MONDAY_TEN), utc(NEXT_MONDAY_TEN), "time"]);
  }));

test("кнопка у разовой — закрывает, как раньше, и с разом тоже", () =>
  withDatabase(async (db) => {
    await saveZone(db);
    const plain = await seedTask(db, { repeat: null });
    await seedReminder(db, plain, "due", MONDAY_TEN);
    const becameSingle = await seedTask(db, { repeat: null });

    assert.equal((await markDone(db, plain))?.status, "done");
    assert.deepEqual(await remindersOf(db, plain), []);
    assert.equal((await markDone(db, becameSingle, seconds(MONDAY_TEN)))?.status, "done");
    assert.equal(await markDone(db, randomUUID()), null);
  }));

test("кнопка у серии, убранной целиком, — как есть", () =>
  withDatabase(async (db) => {
    await saveZone(db);
    const id = await seedTask(db, { status: "cancelled" });
    const before = await taskOf(db, id);

    assert.equal((await markDone(db, id, seconds(MONDAY_TEN)))?.status, "cancelled");
    assert.deepEqual(await taskOf(db, id), before);
  }));

test("перенесённый раз: следующий считается от раза, а не от срока", () =>
  withDatabase(async (db) => {
    await saveZone(db);
    // Понедельничный раз перенесли на пятницу 11 октября.
    const id = await seedTask(db, { dueAt: "2030-10-11T15:00:00+05:00", occurrence: MONDAY_TEN });

    await markDone(db, id, seconds(MONDAY_TEN));

    assert.deepEqual(occurrenceOf(await taskOf(db, id)), [utc(NEXT_MONDAY_TEN), utc(NEXT_MONDAY_TEN), "time"]);
  }));

test("повтор днём: следующий раз — 18:00 с точностью «день»", () =>
  withDatabase(async (db) => {
    await saveZone(db);
    const id = await seedTask(db, { repeat: MONDAYS, dueAt: "2030-10-07T18:00:00+05:00" });

    await markDone(db, id);

    const evening = utc("2030-10-14T18:00:00+05:00");
    assert.deepEqual(occurrenceOf(await taskOf(db, id)), [evening, evening, "day"]);
    assert.deepEqual(schedule(await remindersOf(db, id)), [
      ["before", utc("2030-10-14T09:00:00+05:00"), false],
      ["due", evening, false],
    ]);
  }));

// --- «Сделано»: приложение --------------------------------------------------------

test("приложение: complete_task с разом и без раза переводит, разовую закрывает", () =>
  withDatabase(async (db) => {
    await saveZone(db);
    const withOccurrence = await seedMonday(db);
    const withoutOccurrence = await seedMonday(db);
    const single = await seedTask(db, { repeat: null });

    const rows = await asRole(db, "authenticated", OWNER, async () => {
      const call = async (sql: string, params: unknown[]) => only((await db.query<TaskRow>(sql, params)).rows);
      return [
        await call("select * from public.complete_task($1::uuid, $2)", [withOccurrence, seconds(MONDAY_TEN)]),
        await call("select * from public.complete_task($1::uuid, $2)", [withOccurrence, seconds(MONDAY_TEN)]),
        await call("select * from public.complete_task($1::uuid)", [withoutOccurrence]),
        await call("select * from public.complete_task($1::uuid)", [single]),
      ];
    });

    const moved = [utc(NEXT_MONDAY_TEN), utc(NEXT_MONDAY_TEN), "time"];
    assert.deepEqual(occurrenceOf(rows[0]!), moved);
    assert.equal(rows[0]!.status, "active");
    assert.deepEqual(occurrenceOf(rows[1]!), moved);
    assert.deepEqual(occurrenceOf(rows[2]!), moved);
    assert.equal(rows[3]!.status, "done");
    assert.deepEqual(schedule(await remindersOf(db, withOccurrence)), NEXT_MONDAY_SCHEDULE);
  }));

test("приложение: чужая задача под токеном — пустой ответ, ничего не тронуто", () =>
  withDatabase(async (db) => {
    await saveZone(db, STRANGER);
    const strangers = await seedMonday(db, { owner: STRANGER });
    const before = await taskOf(db, strangers);

    const [app, core] = await asRole(db, "authenticated", OWNER, async () => {
      const app = await db.query<TaskRow>("select * from public.complete_task($1::uuid, $2)", [strangers, seconds(MONDAY_TEN)]);
      const core = await db.query<TaskRow>(
        "select * from public.advance_task($1, $2::uuid, $3, $4::timestamptz, $5::jsonb)",
        [STRANGER, strangers, seconds(MONDAY_TEN), "2031-01-01T00:00:00Z", "[]"],
      );
      return [only(app.rows), only(core.rows)];
    });

    assert.equal(app.id, null);
    assert.equal(core.id, null);
    assert.deepEqual(await taskOf(db, strangers), before);
    assert.equal((await remindersOf(db, strangers)).length, 2);
  }));

test("ядро под токеном: готовый следующий раз и план отброшены — их считает база", () =>
  withDatabase(async (db) => {
    await saveZone(db);
    const id = await seedMonday(db);

    const row = await asRole(db, "authenticated", OWNER, async () => {
      const { rows } = await db.query<TaskRow>(
        "select * from public.advance_task($1, $2::uuid, $3, $4::timestamptz, $5::jsonb)",
        [OWNER, id, seconds(MONDAY_TEN), "2031-01-01T00:00:00Z", JSON.stringify([{ stage: "due", fire_at: "2031-01-01T00:00:00Z" }])],
      );
      return only(rows);
    });

    assert.deepEqual(occurrenceOf(row), [utc(NEXT_MONDAY_TEN), utc(NEXT_MONDAY_TEN), "time"]);
    assert.deepEqual(schedule(await remindersOf(db, id)), NEXT_MONDAY_SCHEDULE);
  }));

// --- «Сделано» и пропуск словом -------------------------------------------------------

const NEXT_PLAN: Json[] = [
  { stage: "before", fire_at: utc("2030-10-14T09:00:00+05:00") },
  { stage: "due", fire_at: utc(NEXT_MONDAY_TEN) },
];

test("словом: done и skip по повторяющейся — переход с готовым разом и планом бота", () =>
  withDatabase(async (db) => {
    for (const action of ["done", "skip"]) {
      const id = await seedMonday(db, { needsReview: true });
      const messageId = await message(db);

      const row = await understand(db, {
        messageId,
        edit: { task_id: id, action, occurrence: seconds(MONDAY_TEN), next_at: utc(NEXT_MONDAY_TEN), schedule: NEXT_PLAN },
      });

      assert.equal(row?.id, id, action);
      const saved = await taskOf(db, id);
      assert.equal(saved.status, "active", action);
      assert.equal(saved.needs_review, true, action);
      assert.equal(saved.due_moved_at, null, action);
      assert.deepEqual(occurrenceOf(saved), [utc(NEXT_MONDAY_TEN), utc(NEXT_MONDAY_TEN), "time"], action);
      assert.deepEqual(schedule(await remindersOf(db, id)), NEXT_MONDAY_SCHEDULE, action);
    }
  }));

test("словом: задача ушла на другой раз — отказ целиком, разбор не записан", () =>
  withDatabase(async (db) => {
    const id = await seedMonday(db);
    const before = await taskOf(db, id);

    for (const occurrence of [seconds("2030-09-30T10:00:00+05:00"), null]) {
      const messageId = await message(db);
      await assert.rejects(
        understand(db, {
          messageId,
          edit: { task_id: id, action: "done", occurrence, next_at: utc(NEXT_MONDAY_TEN), schedule: NEXT_PLAN },
        }),
        /not an active task/,
      );
      assert.equal(await analysisOf(db, messageId), null);
    }
    assert.deepEqual(await taskOf(db, id), before);
  }));

test("словом: закрытие повторяющейся без следующего раза — отказ, задача активна", () =>
  withDatabase(async (db) => {
    const id = await seedMonday(db);
    const messageId = await message(db);

    await assert.rejects(
      understand(db, { messageId, edit: { task_id: id, action: "done", occurrence: seconds(MONDAY_TEN) } }),
      /next_at/,
    );
    const saved = await taskOf(db, id);
    assert.equal(saved.status, "active");
    assert.deepEqual(occurrenceOf(saved), [utc(MONDAY_TEN), utc(MONDAY_TEN), "time"]);
  }));

test("словом: skip по разовой — убрана, как cancel; done — закрыта", () =>
  withDatabase(async (db) => {
    const skipped = await seedTask(db, { repeat: null });
    await seedReminder(db, skipped, "due", MONDAY_TEN);
    const done = await seedTask(db, { repeat: null });

    await understand(db, { messageId: await message(db), edit: { task_id: skipped, action: "skip" } });
    await understand(db, { messageId: await message(db), edit: { task_id: done, action: "done" } });

    assert.equal((await taskOf(db, skipped)).status, "cancelled");
    assert.deepEqual(await remindersOf(db, skipped), []);
    assert.equal((await taskOf(db, done)).status, "done");
  }));

test("словом: cancel по повторяющейся убирает серию, правило остаётся; «Вернуть» её возвращает", () =>
  withDatabase(async (db) => {
    const id = await seedMonday(db);

    await understand(db, { messageId: await message(db), edit: { task_id: id, action: "cancel" } });

    const cancelled = await taskOf(db, id);
    assert.equal(cancelled.status, "cancelled");
    assert.deepEqual(cancelled.repeat, { ...MONDAYS, time: "10:00" });
    assert.deepEqual(occurrenceOf(cancelled), [utc(MONDAY_TEN), utc(MONDAY_TEN), "time"]);
    assert.deepEqual(schedule(await remindersOf(db, id)), [["before", utc("2030-10-07T09:00:00+05:00"), true]]);

    const { rows } = await db.query<TaskRow>("select * from public.reopen_task($1, $2::uuid, $3::jsonb)", [
      OWNER,
      id,
      JSON.stringify([{ stage: "due", fire_at: utc(MONDAY_TEN) }]),
    ]);
    assert.equal(only(rows).status, "active");
    assert.deepEqual(only(rows).repeat, { ...MONDAYS, time: "10:00" });
  }));

// --- «Вернуть» под «Отметил» и «Пропускаю» -------------------------------------------

test("«Вернуть»: задача на своём разе — назад на прежний раз в час серии, с планом бота", () =>
  withDatabase(async (db) => {
    await saveZone(db);
    const id = await seedMonday(db);
    await markDone(db, id, seconds(MONDAY_TEN));

    const row = await giveBack(db, id, MONDAY_TEN, NEXT_MONDAY_TEN, [{ stage: "due", fire_at: utc(MONDAY_TEN) }]);

    assert.equal(row?.id, id);
    assert.deepEqual(occurrenceOf(await taskOf(db, id)), [utc(MONDAY_TEN), utc(MONDAY_TEN), "time"]);
    assert.deepEqual(schedule(await remindersOf(db, id)), [["due", utc(MONDAY_TEN), false]]);
  }));

test("«Вернуть» после разового переноса — раз в час и точность серии, перенос не восстанавливается", () =>
  withDatabase(async (db) => {
    await saveZone(db);
    const monday = "2030-10-07T18:00:00+05:00";
    const id = await seedTask(db, { repeat: MONDAYS, dueAt: "2030-10-08T15:00:00+05:00", precision: "time", occurrence: monday });
    await markDone(db, id);

    await giveBack(db, id, monday, "2030-10-14T18:00:00+05:00");

    assert.deepEqual(occurrenceOf(await taskOf(db, id)), [utc(monday), utc(monday), "day"]);
  }));

test("«Вернуть»: задача ушла дальше — как есть; удалённая — пусто; разовая — как есть", () =>
  withDatabase(async (db) => {
    await saveZone(db);
    const id = await seedMonday(db);
    await markDone(db, id, seconds(MONDAY_TEN));
    await markDone(db, id, seconds(NEXT_MONDAY_TEN));
    const before = await taskOf(db, id);
    const reminders = await remindersOf(db, id);

    const row = await giveBack(db, id, MONDAY_TEN, NEXT_MONDAY_TEN);

    assert.equal(iso(row?.occurrence_at), utc("2030-10-21T10:00:00+05:00"));
    assert.deepEqual(await taskOf(db, id), before);
    assert.deepEqual(await remindersOf(db, id), reminders);

    assert.equal(await giveBack(db, randomUUID(), MONDAY_TEN, NEXT_MONDAY_TEN), null);

    const single = await seedTask(db, { repeat: null, dueAt: NEXT_MONDAY_TEN });
    const untouched = await giveBack(db, single, MONDAY_TEN, NEXT_MONDAY_TEN);
    assert.equal(iso(untouched?.due_at), utc(NEXT_MONDAY_TEN));
    assert.equal(untouched?.occurrence_at, null);
  }));

// --- Перекатывание ------------------------------------------------------------------

test("перекатывание: раз ждёт до полуночи дня следующего и переходит молча, с обеими ступенями", () =>
  withDatabase(async (db) => {
    await saveZone(db);
    const id = await seedTask(db, { repeat: DAILY, dueAt: FRIDAY_DAY });
    await seedReminder(db, id, "before", "2030-10-04T09:00:00+05:00", { at: "2030-10-04T04:00:05Z", messageId: 51 });
    await seedReminder(db, id, "due", FRIDAY_DAY, { at: "2030-10-04T13:00:05Z", messageId: 52 });

    assert.deepEqual(await roll(db, "2030-10-04T23:59:00+05:00"), []);
    assert.deepEqual(occurrenceOf(await taskOf(db, id)), [utc(FRIDAY_DAY), utc(FRIDAY_DAY), "day"]);

    const rolled = await roll(db, "2030-10-05T00:00:00+05:00");

    assert.equal(only(rolled).id, id);
    const saved = await taskOf(db, id);
    const saturday = utc("2030-10-05T18:00:00+05:00");
    assert.deepEqual(occurrenceOf(saved), [saturday, saturday, "day"]);
    assert.equal(saved.due_moved_at, null);
    assert.equal(saved.status, "active");
    assert.deepEqual(schedule(await remindersOf(db, id)), [
      ["before", utc("2030-10-05T09:00:00+05:00"), false],
      ["due", saturday, false],
    ]);
    assert.deepEqual(await roll(db, "2030-10-05T00:01:00+05:00"), []);
  }));

test("перекатывание: срок раза ещё не прошёл — стоит, хотя начало следующего наступило", () =>
  withDatabase(async (db) => {
    await saveZone(db);
    // Раз понедельника перенесли на вторник 15-го: следующий понедельник 14-го
    // начался, а срок ещё впереди.
    const tuesday = "2030-10-15T18:00:00+05:00";
    const id = await seedTask(db, { repeat: MONDAYS, dueAt: tuesday, precision: "day", occurrence: "2030-10-07T18:00:00+05:00" });

    assert.deepEqual(await roll(db, "2030-10-14T12:00:00+05:00"), []);
    assert.equal(iso((await taskOf(db, id)).due_at), utc(tuesday));
  }));

test("перекатывание: раз, перенесённый позже следующего, этот следующий поглощает", () =>
  withDatabase(async (db) => {
    await saveZone(db);
    const id = await seedTask(db, {
      repeat: MONDAYS,
      dueAt: "2030-10-15T18:00:00+05:00",
      precision: "day",
      occurrence: "2030-10-07T18:00:00+05:00",
    });

    assert.deepEqual(await roll(db, "2030-10-20T23:00:00+05:00"), []);
    const rolled = await roll(db, "2030-10-21T00:00:00+05:00");

    assert.equal(only(rolled).id, id);
    const monday = utc("2030-10-21T18:00:00+05:00");
    assert.deepEqual(occurrenceOf(await taskOf(db, id)), [monday, monday, "day"]);
  }));

test("перекатывание после простоя — сразу на последний наступивший раз, одно созревшее напоминание", () =>
  withDatabase(async (db) => {
    await saveZone(db);
    const id = await seedTask(db, { repeat: { ...DAILY, time: "10:00" }, dueAt: "2030-10-01T10:00:00+05:00" });
    await seedReminder(db, id, "due", "2030-10-01T10:00:00+05:00", { at: "2030-10-01T05:00:05Z", messageId: 61 });
    const now = "2030-10-04T20:00:00+05:00";

    const rolled = await roll(db, now);

    assert.equal(only(rolled).id, id);
    const friday = utc("2030-10-04T10:00:00+05:00");
    assert.deepEqual(occurrenceOf(await taskOf(db, id)), [friday, friday, "time"]);
    assert.deepEqual(schedule(await remindersOf(db, id)), [
      ["before", utc("2030-10-04T09:00:00+05:00"), false],
      ["due", friday, false],
    ]);
    const { rows: due } = await db.query<{ task_id: string }>(
      "select task_id from public.due_reminders($1, $2::timestamptz)",
      [OWNER, now],
    );
    assert.deepEqual(new Set(due.map((row) => row.task_id)), new Set([id]));
  }));

test("перекатывание: «каждый день в 00:30» начинается в 23:30 накануне", () =>
  withDatabase(async (db) => {
    await saveZone(db);
    const id = await seedTask(db, { repeat: { ...DAILY, time: "00:30" }, dueAt: "2030-10-04T00:30:00+05:00" });

    assert.deepEqual(await roll(db, "2030-10-04T23:29:00+05:00"), []);
    const rolled = await roll(db, "2030-10-04T23:30:00+05:00");

    assert.equal(only(rolled).id, id);
    const night = utc("2030-10-05T00:30:00+05:00");
    assert.deepEqual(occurrenceOf(await taskOf(db, id)), [night, night, "time"]);
    assert.deepEqual(schedule(await remindersOf(db, id)), [
      ["before", utc("2030-10-04T23:30:00+05:00"), false],
      ["due", night, false],
    ]);
  }));

test("перекатывание: убранная серия, разовая, чужая и владелец без пояса — не двигаются", () =>
  withDatabase(async (db) => {
    await saveZone(db);
    await saveZone(db, STRANGER);
    const cancelled = await seedTask(db, { repeat: DAILY, dueAt: FRIDAY_DAY, status: "cancelled" });
    const single = await seedTask(db, { repeat: null, dueAt: FRIDAY_DAY });
    const strangers = await seedTask(db, { owner: STRANGER, repeat: DAILY, dueAt: FRIDAY_DAY });
    const now = "2030-10-10T12:00:00+05:00";

    assert.deepEqual(await roll(db, now), []);
    for (const id of [cancelled, single, strangers]) {
      assert.equal(iso((await taskOf(db, id)).due_at), utc(FRIDAY_DAY));
    }

    const lonely = 555;
    const noZone = await seedTask(db, { owner: lonely, repeat: DAILY, dueAt: FRIDAY_DAY });
    assert.deepEqual(await roll(db, now, lonely), []);
    assert.equal(iso((await taskOf(db, noZone)).due_at), utc(FRIDAY_DAY));
  }));

// --- Правило в правке -----------------------------------------------------------------

test("change_task: правило разовой задаче — первым разом становится её срок, час — из срока", () =>
  withDatabase(async (db) => {
    await saveZone(db);
    const byDay = await seedTask(db, { repeat: null, dueAt: FRIDAY_DAY, precision: "day" });
    const byHour = await seedTask(db, { repeat: null, dueAt: MONDAY_TEN, precision: "time" });

    const day = await change(db, byDay, { repeat: { every: "week", interval: 1, weekdays: [5] } });
    const hour = await change(db, byHour, { repeat: { every: "week", interval: 1, weekdays: [1], time: "23:00" } });

    assert.deepEqual(day?.repeat, { every: "week", interval: 1, weekdays: [5], month_day: null, month: null, time: null });
    assert.deepEqual(occurrenceOf(day!), [utc(FRIDAY_DAY), utc(FRIDAY_DAY), "day"]);
    assert.deepEqual(hour?.repeat, { every: "week", interval: 1, weekdays: [1], month_day: null, month: null, time: "10:00" });
    assert.deepEqual(occurrenceOf(hour!), [utc(MONDAY_TEN), utc(MONDAY_TEN), "time"]);
  }));

test("change_task: «теперь по вторникам» и «теперь в 11» — правило, срок и раз по названному сроку", () =>
  withDatabase(async (db) => {
    await saveZone(db);
    const id = await seedMonday(db);
    const tuesdays: Rule = { every: "week", interval: 1, weekdays: [2] };
    const tuesday = "2030-10-08T10:00:00+05:00";

    const moved = await change(db, id, { due_at: tuesday, repeat: tuesdays }, [{ stage: "due", fire_at: utc(tuesday) }]);

    assert.deepEqual(moved?.repeat, { ...tuesdays, month_day: null, month: null, time: "10:00" });
    assert.deepEqual(occurrenceOf(moved!), [utc(tuesday), utc(tuesday), "time"]);
    assert.equal(moved?.due_moved_at, null);

    const eleven = "2030-10-08T11:00:00+05:00";
    const later = await change(db, id, { due_at: eleven, repeat: tuesdays });
    assert.equal(later?.repeat?.time, "11:00");
    assert.deepEqual(occurrenceOf(later!), [utc(eleven), utc(eleven), "time"]);
  }));

test("change_task: перенос срока у повторяющейся меняет только этот раз", () =>
  withDatabase(async (db) => {
    await saveZone(db);
    const id = await seedMonday(db);

    const row = await change(db, id, { due_date: "2030-10-11" });

    assert.deepEqual(row?.repeat, { ...MONDAYS, time: "10:00" });
    assert.deepEqual(occurrenceOf(row!), [utc("2030-10-11T18:00:00+05:00"), utc(MONDAY_TEN), "day"]);
  }));

test("change_task: снять правило; снять срок и сменить вид — правило снимается тоже", () =>
  withDatabase(async (db) => {
    await saveZone(db);
    const removed = await seedMonday(db);
    const noDue = await seedMonday(db);
    const idea = await seedMonday(db);

    const plain = await change(db, removed, { repeat: null });
    const undated = await change(db, noDue, { due_at: null });
    const wish = await change(db, idea, { kind: "wish" });

    assert.equal(plain?.repeat, null);
    assert.deepEqual(occurrenceOf(plain!), [utc(MONDAY_TEN), null, "time"]);
    assert.equal(undated?.repeat, null);
    assert.deepEqual(occurrenceOf(undated!), [null, null, null]);
    assert.equal(wish?.repeat, null);
    assert.equal(wish?.occurrence_at, null);
  }));

test("change_task: правило без срока после правки или у идеи — отказ целиком; не по форме — отказ", () =>
  withDatabase(async (db) => {
    await saveZone(db);
    const undated = await seedTask(db, { repeat: null, dueAt: null, precision: null });
    const dated = await seedMonday(db);
    const before = await taskOf(db, dated);

    await assert.rejects(change(db, undated, { title: "новое", repeat: MONDAYS }), /repeat needs a due/);
    await assert.rejects(change(db, dated, { due_at: null, repeat: MONDAYS }), /repeat needs a due/);
    await assert.rejects(change(db, dated, { kind: "idea", repeat: MONDAYS }), /repeat needs a due/);
    await assert.rejects(change(db, dated, { repeat: { every: "week", interval: 1, weekdays: [] } }), /invalid repeat/);

    assert.equal((await taskOf(db, undated)).title, "планёрка");
    assert.deepEqual(await taskOf(db, dated), before);
  }));

test("edit_task с repeat из приложения: правило по сроку формы; чужая задача — ничего", () =>
  withDatabase(async (db) => {
    await saveZone(db);
    await saveZone(db, STRANGER);
    const own = await seedTask(db, { repeat: null, dueAt: FRIDAY_DAY, precision: "day" });
    const strangers = await seedMonday(db, { owner: STRANGER });
    const before = await taskOf(db, strangers);

    const [mine, foreign] = await asRole(db, "authenticated", OWNER, async () => {
      const mine = await db.query<TaskRow>("select * from public.edit_task($1::uuid, $2::jsonb)", [
        own,
        JSON.stringify({ due_date: "2030-10-10", repeat: { every: "month", interval: 1, month_day: 10 } }),
      ]);
      const foreign = await db.query<TaskRow>("select * from public.edit_task($1::uuid, $2::jsonb)", [
        strangers,
        JSON.stringify({ repeat: null }),
      ]);
      return [only(mine.rows), only(foreign.rows)];
    });

    const tenth = utc("2030-10-10T18:00:00+05:00");
    assert.deepEqual(mine.repeat, { every: "month", interval: 1, weekdays: null, month_day: 10, month: null, time: null });
    assert.deepEqual(occurrenceOf(mine), [tenth, tenth, "day"]);
    assert.equal(foreign.id, null);
    assert.deepEqual(await taskOf(db, strangers), before);
  }));

// --- Правило при записи и в ответе на вопрос --------------------------------------------

test("запись с правилом: одна задача, раз — срок, час правила — из срока", () =>
  withDatabase(async (db) => {
    await saveZone(db);
    const evening = utc("2030-10-07T18:00:00+05:00");
    const morning = utc("2030-10-07T09:00:00+05:00");
    const weekly = await understand(db, {
      messageId: await message(db),
      task: { title: "отправить отчёт", kind: "task", due_at: evening, due_precision: "day", repeat: MONDAYS as unknown as Json },
    });
    const weekdays = await understand(db, {
      messageId: await message(db),
      task: {
        title: "планёрка",
        kind: "task",
        due_at: morning,
        due_precision: "time",
        repeat: { every: "week", interval: 1, weekdays: [5, 1, 3, 2, 4] },
      },
    });

    assert.deepEqual(weekly?.repeat, { every: "week", interval: 1, weekdays: [1], month_day: null, month: null, time: null });
    assert.deepEqual(occurrenceOf(weekly!), [evening, evening, "day"]);
    assert.deepEqual(weekdays?.repeat, {
      every: "week",
      interval: 1,
      weekdays: [1, 2, 3, 4, 5],
      month_day: null,
      month: null,
      time: "09:00",
    });
    assert.equal(iso(weekdays?.occurrence_at), morning);
  }));

test("ответ на вопрос: срок и правило одной поправкой", () =>
  withDatabase(async (db) => {
    await saveZone(db);
    const id = await seedTask(db, { repeat: null, dueAt: null, precision: null, question: "Какого числа каждый месяц?" });
    const due = utc("2030-10-10T18:00:00+05:00");

    const row = await understand(db, {
      messageId: await message(db),
      amend: {
        task_id: id,
        fields: { due_at: due, due_precision: "day", needs_review: false, repeat: { every: "month", interval: 1, month_day: 10 } },
        reminders: [{ stage: "due", fire_at: due }],
      },
    });

    assert.deepEqual(row?.repeat, { every: "month", interval: 1, weekdays: null, month_day: 10, month: null, time: null });
    assert.deepEqual(occurrenceOf(row!), [due, due, "day"]);
    assert.equal(row?.open_question, null);
  }));

test("правило в правке словом: changes.repeat идёт в ядро", () =>
  withDatabase(async (db) => {
    await saveZone(db);
    const id = await seedMonday(db);

    const row = await understand(db, {
      messageId: await message(db),
      edit: { task_id: id, action: "change", changes: { repeat: null }, schedule: [] },
    });

    assert.equal(row?.repeat, null);
    assert.equal(row?.occurrence_at, null);
    assert.equal(iso(row?.due_at), utc(MONDAY_TEN));
  }));

// --- Права --------------------------------------------------------------------------

test("у каждой функции одна перегрузка и свои права", () =>
  withDatabase(async (db) => {
    const both = { anon: false, authenticated: true, service_role: true };
    const bot = { anon: false, authenticated: false, service_role: true };
    const app = { anon: false, authenticated: true, service_role: false };
    const expected: Record<string, Record<string, boolean>> = {
      "public.repeat_valid(jsonb)": both,
      "public.repeat_next(jsonb, timestamptz, timestamptz, text)": both,
      "public.repeat_rule(jsonb, timestamptz, text, text)": both,
      "public.advance_task(bigint, uuid, bigint, timestamptz, jsonb)": both,
      "public.mark_task_done(bigint, uuid, bigint)": bot,
      "public.complete_task(uuid, bigint)": app,
      "public.return_occurrence(bigint, uuid, bigint, bigint, jsonb)": bot,
      "public.roll_repeats(bigint, timestamptz)": bot,
      "public.due_reminders(bigint, timestamptz)": bot,
      "public.moved_tasks(bigint)": bot,
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

test("mark_task_done, «Вернуть» и перекатывание под anon и authenticated — отказ в праве", () =>
  withDatabase(async (db) => {
    await saveZone(db);
    const id = await seedMonday(db);
    const before = await taskOf(db, id);
    const calls: [string, unknown[]][] = [
      ["select * from public.mark_task_done($1, $2::uuid, $3)", [OWNER, id, seconds(MONDAY_TEN)]],
      [
        "select * from public.return_occurrence($1, $2::uuid, $3, $4, '[]'::jsonb)",
        [OWNER, id, seconds(MONDAY_TEN), seconds(NEXT_MONDAY_TEN)],
      ],
      ["select * from public.roll_repeats($1, $2::timestamptz)", [OWNER, "2031-01-01T00:00:00Z"]],
    ];

    for (const role of ["anon", "authenticated"] as const) {
      for (const [sql, params] of calls) {
        await asRole(db, role, OWNER, async () => {
          await assert.rejects(db.query(sql, params), /permission denied/, `${role}: ${sql}`);
        });
      }
    }
    await asRole(db, "anon", null, async () => {
      await assert.rejects(
        db.query("select public.repeat_next($1::jsonb, now(), now(), $2)", [JSON.stringify(DAILY), YEKATERINBURG]),
        /permission denied/,
      );
    });
    assert.deepEqual(await taskOf(db, id), before);
  }));

test("repeat_next и правка задачи с правилом под authenticated работают", () =>
  withDatabase(async (db) => {
    await saveZone(db);
    const id = await seedMonday(db);

    const at = await asRole(db, "authenticated", OWNER, async () => {
      const { rows } = await db.query<{ at: Date }>(
        "select public.repeat_next($1::jsonb, $2::timestamptz, $2::timestamptz, $3) as at",
        [JSON.stringify(MONDAYS), MONDAY_TEN, YEKATERINBURG],
      );
      // Прямая правка под RLS проходит проверки таблицы с правилом.
      await db.query("update public.tasks set title = 'планёрка отдела' where id = $1", [id]);
      return only(rows).at;
    });

    assert.equal(iso(at), utc("2030-10-14T18:00:00+05:00"));
    assert.equal((await taskOf(db, id)).title, "планёрка отдела");
  }));
