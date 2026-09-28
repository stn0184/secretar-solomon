/**
 * `edit_task` на настоящем Postgres: правка задачи из Mini App.
 *
 * Вызов идёт так же, как из приложения: роль `authenticated`, владелец —
 * клеймом токена, функция работает под RLS (`security invoker`). Данные
 * заводятся и проверяются от владельца базы — мимо правил доступа.
 *
 * Правила — `techspec/11-edit.md` §11.2–11.4 и `techspec/03-schema.md` §3.6.
 * Функция перепланирует от настоящих часов базы, поэтому сроки в тестах —
 * далеко в будущем (2030) или заведомо в прошлом (2020).
 */
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { test } from "node:test";

import type { PGlite } from "@electric-sql/pglite";

import { asRole, withDatabase } from "./database.ts";

const OWNER = 777;
const STRANGER = 999;

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
}

interface ReminderRow {
  id: string;
  stage: string;
  fire_at: Date;
  sent_at: Date | null;
  telegram_message_id: string | number | null;
}

/** Пятница, 4 октября 2030 года: 18:00 у владельца (UTC+5) — 13:00 UTC. */
const FRIDAY_DUE = "2030-10-04T13:00:00.000Z";
const FRIDAY_MORNING = "2030-10-04T04:00:00.000Z";

function only<T>(rows: T[]): T {
  assert.equal(rows.length, 1, `ждали одну строку, пришло ${rows.length}`);
  return rows[0]!;
}

function iso(moment: Date | null): string | null {
  return moment === null ? null : moment.toISOString();
}

async function saveZone(db: PGlite, owner = OWNER, zone = "Asia/Yekaterinburg"): Promise<void> {
  await db.query("select public.save_owner_timezone($1, $2)", [owner, zone]);
}

interface Seed {
  owner?: number;
  title?: string;
  kind?: string;
  status?: string;
  dueAt?: string | null;
  precision?: string | null;
  priority?: string;
  promise?: string | null;
  people?: string[];
  needsReview?: boolean;
  question?: string | null;
}

/** Задача, какой её оставил бот: по умолчанию — дело со сроком в пятницу. */
async function seedTask(db: PGlite, seed: Seed = {}): Promise<string> {
  const { rows } = await db.query<{ id: string }>(
    `insert into public.tasks (
       owner_telegram_id, title, kind, status, due_at, due_precision, priority,
       promise, people, needs_review, open_question, question_asked_at
     ) values (
       $1, $2, $3, $4, $5::timestamptz, $6, $7, $8,
       array(select jsonb_array_elements_text($9::jsonb)), $10, $11,
       case when $11::text is null then null else now() end
     ) returning id`,
    [
      seed.owner ?? OWNER,
      seed.title ?? "отправить расчёт клиенту",
      seed.kind ?? "task",
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

/** Вызов, как из приложения. Пустой ответ базы — `null`. */
async function edit(db: PGlite, taskId: string, changes: Json, owner: number | null = OWNER): Promise<TaskRow | null> {
  return asRole(db, "authenticated", owner, async () => {
    const { rows } = await db.query<TaskRow>("select * from public.edit_task($1::uuid, $2::jsonb)", [
      taskId,
      JSON.stringify(changes),
    ]);
    const row = only(rows);
    return row.id === null ? null : row;
  });
}

async function taskOf(db: PGlite, id: string): Promise<TaskRow> {
  const { rows } = await db.query<TaskRow>("select * from public.tasks where id = $1", [id]);
  return only(rows);
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

test("у функции одна перегрузка; зовёт её только authenticated", async () => {
  await withDatabase(async (db) => {
    const { rows } = await db.query<{ n: number }>(
      "select count(*)::int as n from pg_proc where proname = 'edit_task' and pronamespace = 'public'::regnamespace",
    );
    assert.equal(rows[0]?.n, 1);

    const rights = await db.query<{ role: string; allowed: boolean }>(
      `select role, has_function_privilege(role, 'public.edit_task(uuid, jsonb)', 'execute') as allowed
         from unnest(array['anon', 'authenticated', 'service_role']) as role`,
    );
    assert.deepEqual(Object.fromEntries(rights.rows.map((row) => [row.role, row.allowed])), {
      anon: false,
      authenticated: true,
      service_role: false,
    });
  });
});

test("anon — отказ в праве, задача не тронута", async () => {
  await withDatabase(async (db) => {
    await saveZone(db);
    const id = await seedTask(db);

    await asRole(db, "anon", null, async () => {
      await assert.rejects(
        db.query("select * from public.edit_task($1::uuid, $2::jsonb)", [id, JSON.stringify({ title: "x" })]),
        /permission denied/,
      );
    });

    assert.equal((await taskOf(db, id)).title, "отправить расчёт клиенту");
  });
});

test("своя задача: суть меняется, возвращается строка из базы", async () => {
  await withDatabase(async (db) => {
    await saveZone(db);
    const id = await seedTask(db);

    const saved = await edit(db, id, { title: "  отправить расчёт Кузнецову  " });

    assert.equal(saved?.id, id);
    assert.equal(saved?.title, "отправить расчёт Кузнецову");
    assert.equal((await taskOf(db, id)).title, "отправить расчёт Кузнецову");
  });
});

test("чужая, несуществующая, закрытая задача и токен без клейма — пустой ответ, ничего не записано", async () => {
  await withDatabase(async (db) => {
    await saveZone(db);
    await saveZone(db, STRANGER);
    const foreign = await seedTask(db, { owner: STRANGER });
    const closed = await seedTask(db, { status: "done" });
    const own = await seedTask(db);

    assert.equal(await edit(db, foreign, { title: "чужое" }), null);
    assert.equal(await edit(db, randomUUID(), { title: "нет такой" }), null);
    assert.equal(await edit(db, closed, { title: "закрытая" }), null);
    assert.equal(await edit(db, own, { title: "без клейма" }, null), null);

    assert.equal((await taskOf(db, foreign)).title, "отправить расчёт клиенту");
    assert.equal((await taskOf(db, closed)).title, "отправить расчёт клиенту");
    assert.equal((await taskOf(db, closed)).status, "done");
    assert.equal((await taskOf(db, own)).title, "отправить расчёт клиенту");
  });
});

test("день без часа — 18:00 в поясе владельца, напоминания в 09:00 и 18:00, отметка переноса", async () => {
  await withDatabase(async (db) => {
    await saveZone(db);
    const id = await seedTask(db, { dueAt: null, precision: null });

    const saved = await edit(db, id, { due_date: "2030-10-04" });

    assert.equal(iso(saved?.due_at ?? null), FRIDAY_DUE);
    assert.equal(saved?.due_precision, "day");
    assert.notEqual(saved?.due_moved_at, null);
    assert.deepEqual(schedule(await remindersOf(db, id)), [
      ["before", FRIDAY_MORNING, false],
      ["due", FRIDAY_DUE, false],
    ]);
  });
});

test("день с часом — момент со смещением, точность time, за час и в срок", async () => {
  await withDatabase(async (db) => {
    await saveZone(db);
    const id = await seedTask(db);

    const saved = await edit(db, id, { due_at: "2030-10-04T15:00:00+05:00" });

    assert.equal(iso(saved?.due_at ?? null), "2030-10-04T10:00:00.000Z");
    assert.equal(saved?.due_precision, "time");
    assert.deepEqual(schedule(await remindersOf(db, id)), [
      ["before", "2030-10-04T09:00:00.000Z", false],
      ["due", "2030-10-04T10:00:00.000Z", false],
    ]);
  });
});

test("момент без смещения, незнакомое поле, оба ключа срока, пустая суть — отказ целиком", async () => {
  await withDatabase(async (db) => {
    await saveZone(db);
    const id = await seedTask(db, { needsReview: true, question: "К какому сроку?" });

    await assert.rejects(edit(db, id, { title: "новое", due_at: "2030-10-04T15:00:00" }), /offset/);
    await assert.rejects(edit(db, id, { status: "done" }), /unknown field status/);
    await assert.rejects(
      edit(db, id, { due_at: "2030-10-04T15:00:00+05:00", due_date: "2030-10-04" }),
      /exclusive/,
    );
    await assert.rejects(edit(db, id, { title: "   " }), /title is empty/);
    await assert.rejects(edit(db, id, { kind: "note" }), /check constraint/);

    const task = await taskOf(db, id);
    assert.equal(task.title, "отправить расчёт клиенту");
    assert.equal(task.status, "active");
    assert.equal(task.needs_review, true);
    assert.equal(task.open_question, "К какому сроку?");
  });
});

test("правка сути, срочности, обещания и людей расписание не трогает и переносом не считается", async () => {
  await withDatabase(async (db) => {
    await saveZone(db);
    const id = await seedTask(db);
    await seedReminder(db, id, "before", FRIDAY_MORNING);
    await seedReminder(db, id, "due", FRIDAY_DUE);
    const before = await remindersOf(db, id);

    const saved = await edit(db, id, {
      title: "позвонить Кузнецову",
      priority: "high",
      promise: "mine",
      people: ["  Кузнецов ", "", "Анна"],
    });

    assert.equal(saved?.priority, "high");
    assert.equal(saved?.promise, "mine");
    assert.deepEqual(saved?.people, ["Кузнецов", "Анна"]);
    assert.equal(saved?.due_moved_at, null);
    assert.deepEqual(await remindersOf(db, id), before);
  });
});

test("тот же срок ещё раз — не перенос: расписание и отметка на месте", async () => {
  await withDatabase(async (db) => {
    await saveZone(db);
    const id = await seedTask(db);
    await seedReminder(db, id, "due", FRIDAY_DUE);
    const before = await remindersOf(db, id);

    const saved = await edit(db, id, { due_date: "2030-10-04" });

    assert.equal(saved?.due_moved_at, null);
    assert.deepEqual(await remindersOf(db, id), before);
  });
});

test("смена точности при том же моменте — перенос и новое расписание", async () => {
  await withDatabase(async (db) => {
    await saveZone(db);
    const id = await seedTask(db, { precision: "time" });
    await seedReminder(db, id, "before", "2030-10-04T12:00:00.000Z");
    await seedReminder(db, id, "due", FRIDAY_DUE);

    const saved = await edit(db, id, { due_date: "2030-10-04" });

    assert.equal(saved?.due_precision, "day");
    assert.notEqual(saved?.due_moved_at, null);
    assert.deepEqual(schedule(await remindersOf(db, id)), [
      ["before", FRIDAY_MORNING, false],
      ["due", FRIDAY_DUE, false],
    ]);
  });
});

test("смена вида перепланирует, но переносом срока не считается", async () => {
  await withDatabase(async (db) => {
    await saveZone(db);
    const id = await seedTask(db);
    await seedReminder(db, id, "before", FRIDAY_MORNING);
    await seedReminder(db, id, "due", FRIDAY_DUE);

    const idea = await edit(db, id, { kind: "idea" });
    assert.equal(idea?.kind, "idea");
    assert.equal(idea?.due_moved_at, null);
    assert.deepEqual(await remindersOf(db, id), []);

    await edit(db, id, { kind: "task" });
    assert.deepEqual(schedule(await remindersOf(db, id)), [
      ["before", FRIDAY_MORNING, false],
      ["due", FRIDAY_DUE, false],
    ]);
    assert.equal((await taskOf(db, id)).due_moved_at, null);
  });
});

test("ушедшая ступень, которая по новому сроку снова в будущем, взводится заново", async () => {
  await withDatabase(async (db) => {
    await saveZone(db);
    const id = await seedTask(db, { dueAt: "2030-10-02T13:00:00.000Z" });
    await seedReminder(db, id, "before", "2030-10-02T04:00:00.000Z", {
      at: "2030-10-02T04:00:30.000Z",
      messageId: 42,
    });
    await seedReminder(db, id, "due", "2030-10-02T13:00:00.000Z");

    await edit(db, id, { due_date: "2030-10-04" });

    const rows = await remindersOf(db, id);
    assert.deepEqual(schedule(rows), [
      ["before", FRIDAY_MORNING, false],
      ["due", FRIDAY_DUE, false],
    ]);
    assert.equal(rows[0]?.telegram_message_id, null);
  });
});

test("срок снят — неотправленные уходят, ушедшие остаются следом, отметка ставится", async () => {
  await withDatabase(async (db) => {
    await saveZone(db);
    const id = await seedTask(db);
    await seedReminder(db, id, "before", FRIDAY_MORNING, { at: "2030-10-04T04:00:30.000Z", messageId: 42 });
    await seedReminder(db, id, "due", FRIDAY_DUE);

    const saved = await edit(db, id, { due_at: null });

    assert.equal(saved?.due_at, null);
    assert.equal(saved?.due_precision, null);
    assert.notEqual(saved?.due_moved_at, null);
    assert.deepEqual(schedule(await remindersOf(db, id)), [["before", FRIDAY_MORNING, true]]);
  });
});

test("срок в прошлом сохраняется, напоминаний по нему нет", async () => {
  await withDatabase(async (db) => {
    await saveZone(db);
    const id = await seedTask(db);
    await seedReminder(db, id, "due", FRIDAY_DUE);

    const saved = await edit(db, id, { due_at: "2020-01-10T15:00:00+05:00" });

    assert.equal(iso(saved?.due_at ?? null), "2020-01-10T10:00:00.000Z");
    assert.notEqual(saved?.due_moved_at, null);
    assert.deepEqual(await remindersOf(db, id), []);
  });
});

test("сохранение без изменений снимает пометку и вопрос только у этой задачи", async () => {
  await withDatabase(async (db) => {
    await saveZone(db);
    const id = await seedTask(db, { needsReview: true, question: "К какому сроку?" });
    const other = await seedTask(db, { needsReview: true, question: "Кому позвонить?" });
    await seedReminder(db, id, "due", FRIDAY_DUE);
    const before = await remindersOf(db, id);

    const saved = await edit(db, id, {});

    assert.equal(saved?.needs_review, false);
    assert.equal(saved?.open_question, null);
    assert.equal(saved?.question_asked_at, null);
    assert.equal(saved?.due_moved_at, null);
    assert.deepEqual(await remindersOf(db, id), before);

    const untouched = await taskOf(db, other);
    assert.equal(untouched.needs_review, true);
    assert.equal(untouched.open_question, "Кому позвонить?");
    assert.notEqual(untouched.question_asked_at, null);
  });
});

test("пояса в базе нет: правка срока и вида — отказ, правка сути проходит", async () => {
  await withDatabase(async (db) => {
    const id = await seedTask(db);
    await seedReminder(db, id, "due", FRIDAY_DUE);

    await assert.rejects(edit(db, id, { due_date: "2030-10-05" }), /timezone/);
    await assert.rejects(edit(db, id, { due_at: "2030-10-05T15:00:00+05:00" }), /timezone/);
    await assert.rejects(edit(db, id, { kind: "wish" }), /timezone/);

    const task = await taskOf(db, id);
    assert.equal(iso(task.due_at), FRIDAY_DUE);
    assert.equal(task.kind, "task");
    assert.equal(task.due_moved_at, null);
    assert.equal((await remindersOf(db, id)).length, 1);

    assert.equal((await edit(db, id, { title: "позвонить" }))?.title, "позвонить");
  });
});
