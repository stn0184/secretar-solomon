/**
 * Вопрос о деле без срока (§19): `undated_to_ask` выбирает дело, `record_ask`
 * записывает ушедший вопрос.
 *
 * Обе функции зовёт только бот ключом service-role; вызовы здесь идут от
 * владельца базы, права — отдельным тестом. Границы, которые считает бот, тест
 * задаёт от настоящего «сейчас»: `record_ask` и правки ставят время базы
 * (`now()`), и триггер `tasks_set_updated_at` двигает `updated_at` при любой
 * правке задачи — его тест и проверяет, а не подменяет.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import type { PGlite } from "@electric-sql/pglite";

import { asRole, withDatabase } from "./database.ts";

const OWNER = 777;
const STRANGER = 999;
const QUESTION = "Когда займётесь?";

const MINUTE = 60 * 1000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

/** Пятница, 4 октября 2030 года: 18:00 у владельца (UTC+5). */
const FRIDAY_DUE = "2030-10-04T13:00:00.000Z";
const FRIDAY_PLAN = [
  { stage: "before", fire_at: "2030-10-04T04:00:00.000Z" },
  { stage: "due", fire_at: FRIDAY_DUE },
];

interface Bounds {
  dayStart: Date;
  askedBefore: Date;
  questionSince: Date;
  quietSince: Date;
}

interface AskRow {
  task_id: string;
  title: string;
  created_at: Date;
  asked_at: Date | null;
}

interface TaskRow {
  id: string | null;
  title: string | null;
  status: string | null;
  open_question: string | null;
  question_asked_at: Date | null;
  needs_review: boolean | null;
  updated_at: Date | null;
}

interface ReminderRow {
  stage: string;
  fire_at: Date;
  sent_at: Date | null;
  telegram_message_id: string | number | null;
}

function ago(ms: number, from = Date.now()): Date {
  return new Date(from - ms);
}

/**
 * Границы, как их считает бот (`services/asks.py`): полночь «сегодня» — за
 * `sinceMidnight` до `now`, неделя — шесть полночей назад, сутки и 15 минут.
 */
function boundsAt(now = new Date(), sinceMidnight = HOUR): Bounds {
  const dayStart = ago(sinceMidnight, now.getTime());
  return {
    dayStart,
    askedBefore: ago(6 * DAY, dayStart.getTime()),
    questionSince: ago(DAY, now.getTime()),
    quietSince: ago(15 * MINUTE, now.getTime()),
  };
}

function only<T>(rows: T[]): T {
  assert.equal(rows.length, 1, `ждали одну строку, пришло ${rows.length}`);
  return rows[0]!;
}

interface Seed {
  owner?: number;
  title?: string;
  kind?: "task" | "idea" | "wish";
  due?: string | null;
  status?: "active" | "done" | "cancelled";
  created?: Date;
  updated?: Date;
  needsReview?: boolean;
  question?: string | null;
  askedAt?: Date | null;
}

/** Задача прямой вставкой: вставка триггер не будит, `updated_at` — свой. */
async function seedTask(db: PGlite, seed: Seed = {}): Promise<string> {
  const created = seed.created ?? ago(3 * DAY);
  const { rows } = await db.query<{ id: string }>(
    `insert into public.tasks (
       owner_telegram_id, title, kind, due_at, due_precision, status,
       created_at, updated_at, needs_review, open_question, question_asked_at
     )
     values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11) returning id`,
    [
      seed.owner ?? OWNER,
      seed.title ?? "купить фильтр для воды",
      seed.kind ?? "task",
      seed.due ?? null,
      seed.due ? "day" : null,
      seed.status ?? "active",
      created,
      seed.updated ?? created,
      seed.needsReview ?? false,
      seed.question ?? null,
      seed.askedAt ?? null,
    ],
  );
  return only(rows).id;
}

/** Строка `ask`, будто вопрос о задаче ушёл в `sentAt`. */
async function seedAsk(db: PGlite, taskId: string, sentAt: Date, owner = OWNER): Promise<void> {
  await db.query(
    `insert into public.reminders (owner_telegram_id, task_id, stage, fire_at, sent_at, telegram_message_id)
     values ($1, $2, 'ask', $3, $3, 1)`,
    [owner, taskId, sentAt],
  );
}

async function seedMessage(db: PGlite, receivedAt: Date, owner = OWNER): Promise<void> {
  await db.query(
    `insert into public.messages (owner_telegram_id, chat_id, telegram_message_id, kind, text, received_at)
     values ($1, $1, (select coalesce(max(telegram_message_id), 0) + 1 from public.messages), 'text', 'привет', $2)`,
    [owner, receivedAt],
  );
}

async function toAsk(db: PGlite, bounds = boundsAt(), owner = OWNER): Promise<AskRow[]> {
  const { rows } = await db.query<AskRow>("select * from public.undated_to_ask($1, $2, $3, $4, $5)", [
    owner,
    bounds.dayStart,
    bounds.askedBefore,
    bounds.questionSince,
    bounds.quietSince,
  ]);
  return rows;
}

async function chosen(db: PGlite, bounds = boundsAt(), owner = OWNER): Promise<string | null> {
  const rows = await toAsk(db, bounds, owner);
  assert.ok(rows.length <= 1, `ждали не больше одной строки, пришло ${rows.length}`);
  return rows[0]?.task_id ?? null;
}

async function recordAsk(db: PGlite, taskId: string, messageId = 4242, owner = OWNER): Promise<TaskRow | null> {
  const { rows } = await db.query<TaskRow>("select * from public.record_ask($1, $2, $3, $4)", [
    owner,
    taskId,
    QUESTION,
    messageId,
  ]);
  const row = only(rows);
  return row.id === null ? null : row;
}

async function taskRow(db: PGlite, id: string): Promise<TaskRow> {
  const { rows } = await db.query<TaskRow>(
    "select id, title, status, open_question, question_asked_at, needs_review, updated_at from public.tasks where id = $1",
    [id],
  );
  return only(rows);
}

async function remindersOf(db: PGlite, taskId: string): Promise<ReminderRow[]> {
  const { rows } = await db.query<ReminderRow>(
    "select stage, fire_at, sent_at, telegram_message_id from public.reminders where task_id = $1 order by stage",
    [taskId],
  );
  return rows;
}

async function close(db: PGlite, id: string, status: "done" | "cancelled"): Promise<void> {
  await db.query("update public.tasks set status = $2 where id = $1", [id, status]);
}

// --- Схема и права -----------------------------------------------------------

test("у каждой функции одна перегрузка, и зовёт её только service_role", async () => {
  await withDatabase(async (db) => {
    const signatures = [
      "public.undated_to_ask(bigint, timestamptz, timestamptz, timestamptz, timestamptz)",
      "public.record_ask(bigint, uuid, text, bigint)",
    ];
    for (const signature of signatures) {
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
      assert.deepEqual(
        Object.fromEntries(rights.rows.map((row) => [row.role, row.allowed])),
        { anon: false, authenticated: false, service_role: true },
        signature,
      );
    }
  });
});

test("ступень ask пускается в reminders, незнакомая — нет", async () => {
  await withDatabase(async (db) => {
    const id = await seedTask(db);
    await seedAsk(db, id, ago(HOUR));
    await assert.rejects(
      db.query(
        `insert into public.reminders (owner_telegram_id, task_id, stage, fire_at) values ($1, $2, 'later', now())`,
        [OWNER, id],
      ),
      /reminders_stage_check/,
    );
  });
});

// --- Отбор -------------------------------------------------------------------

test("спрашивается только активная задача владельца без срока, не тронутая сегодня", async () => {
  await withDatabase(async (db) => {
    const bounds = boundsAt();
    await seedTask(db, { kind: "idea", title: "идея" });
    await seedTask(db, { kind: "wish", title: "желание" });
    await seedTask(db, { due: FRIDAY_DUE, title: "со сроком" });
    await seedTask(db, { status: "done", title: "закрыта" });
    await seedTask(db, { status: "cancelled", title: "убрана" });
    await seedTask(db, { owner: STRANGER, title: "чужая" });
    await seedTask(db, { title: "записана сегодня", created: ago(40 * MINUTE) });
    await seedTask(db, { title: "поправлена сегодня", updated: ago(30 * MINUTE) });
    const recent = await seedTask(db, { title: "спрашивал пять дней назад" });
    await seedAsk(db, recent, ago(5 * DAY, bounds.dayStart.getTime()));

    assert.deepEqual(await toAsk(db, bounds), []);

    const right = await seedTask(db, { title: "купить фильтр для воды", created: ago(2 * DAY) });
    const row = only(await toAsk(db, bounds));
    assert.equal(row.task_id, right);
    assert.equal(row.title, "купить фильтр для воды");
    assert.equal(row.asked_at, null);
    assert.ok(row.created_at instanceof Date);
  });
});

test("правка задачи сегодня откладывает вопрос до завтра: триггер двигает updated_at", async () => {
  await withDatabase(async (db) => {
    const id = await seedTask(db);
    assert.equal(await chosen(db), id);

    await db.query("update public.tasks set title = 'купить фильтр' where id = $1", [id]);
    assert.equal(await chosen(db), null, "сегодня тронута");

    // Завтра: полночь — минуту назад от «завтрашнего» момента.
    const tomorrow = boundsAt(new Date(Date.now() + 2 * MINUTE), MINUTE);
    assert.equal(await chosen(db, tomorrow), id);
  });
});

test("о спрошенном деле — снова через семь дней по календарю, не раньше", async () => {
  await withDatabase(async (db) => {
    const bounds = boundsAt();
    const id = await seedTask(db, { created: ago(30 * DAY) });

    // Спрашивал ровно шесть полночей назад, на границе: ещё рано.
    await seedAsk(db, id, bounds.askedBefore);
    assert.equal(await chosen(db, bounds), null);

    const earlier = ago(MINUTE, bounds.askedBefore.getTime());
    await db.query("update public.reminders set sent_at = $2 where task_id = $1", [id, earlier]);
    const row = only(await toAsk(db, bounds));
    assert.equal(row.task_id, id);
    assert.equal(row.asked_at?.getTime(), earlier.getTime());
  });
});

test("порядок: не спрошенные от новых к старым, потом спрошенные давнее всех", async () => {
  await withDatabase(async (db) => {
    const bounds = boundsAt();
    const older = await seedTask(db, { title: "старое", created: ago(10 * DAY) });
    const newer = await seedTask(db, { title: "вчерашнее", created: ago(DAY + 2 * HOUR) });
    const longAsked = await seedTask(db, { title: "спрашивал давно", created: ago(40 * DAY) });
    const lateAsked = await seedTask(db, { title: "спрашивал позже", created: ago(50 * DAY) });
    await seedAsk(db, longAsked, ago(20 * DAY));
    await seedAsk(db, lateAsked, ago(9 * DAY));

    const order: string[] = [];
    for (;;) {
      const id = await chosen(db, bounds);
      if (id === null) break;
      order.push(id);
      await close(db, id, "done");
    }
    assert.deepEqual(order, [newer, older, longAsked, lateAsked]);
  });
});

// --- Когда вопроса нет -------------------------------------------------------

test("сегодня уже спрашивал — пусто, даже о другом деле", async () => {
  await withDatabase(async (db) => {
    await seedTask(db);
    const asked = await seedTask(db, { title: "спрошенное" });
    await seedAsk(db, asked, ago(30 * MINUTE));
    assert.equal(await chosen(db), null);
  });
});

test("живой открытый вопрос младше суток — пусто; старше суток или у закрытой — нет", async () => {
  await withDatabase(async (db) => {
    const id = await seedTask(db);
    const asking = await seedTask(db, {
      title: "встреча",
      due: FRIDAY_DUE,
      question: "Во сколько?",
      askedAt: ago(2 * HOUR),
    });
    assert.equal(await chosen(db), null);

    await db.query("update public.tasks set question_asked_at = $2 where id = $1", [asking, ago(DAY + HOUR)]);
    assert.equal(await chosen(db), id);

    await db.query("update public.tasks set question_asked_at = $2, status = 'done' where id = $1", [
      asking,
      ago(2 * HOUR),
    ]);
    assert.equal(await chosen(db), id);
  });
});

test("владелец писал меньше 15 минут назад — пусто", async () => {
  await withDatabase(async (db) => {
    const id = await seedTask(db);
    await seedMessage(db, ago(20 * MINUTE));
    assert.equal(await chosen(db), id, "20 минут — тишина");

    await seedMessage(db, ago(5 * MINUTE));
    assert.equal(await chosen(db), null);
  });
});

test("напоминание ушло меньше 15 минут назад — пусто", async () => {
  await withDatabase(async (db) => {
    const id = await seedTask(db);
    const dated = await seedTask(db, { title: "со сроком", due: FRIDAY_DUE });
    await db.query(
      `insert into public.reminders (owner_telegram_id, task_id, stage, fire_at, sent_at)
       values ($1, $2, 'before', $3, $3), ($1, $2, 'due', $4, null)`,
      [OWNER, dated, ago(20 * MINUTE), FRIDAY_DUE],
    );
    assert.equal(await chosen(db), id, "20 минут и неотправленное — тишина");

    await db.query("update public.reminders set sent_at = $2 where task_id = $1 and stage = 'before'", [
      dated,
      ago(5 * MINUTE),
    ]);
    assert.equal(await chosen(db), null);
  });
});

test("чужие вопросы, сообщения и напоминания владельцу не мешают", async () => {
  await withDatabase(async (db) => {
    const id = await seedTask(db);
    const foreign = await seedTask(db, {
      owner: STRANGER,
      title: "чужая",
      question: "Во сколько?",
      askedAt: ago(HOUR),
    });
    await seedAsk(db, foreign, ago(30 * MINUTE), STRANGER);
    await seedMessage(db, ago(MINUTE), STRANGER);
    await db.query(
      `insert into public.reminders (owner_telegram_id, task_id, stage, fire_at, sent_at)
       values ($1, $2, 'due', $3, $3)`,
      [STRANGER, foreign, ago(MINUTE)],
    );

    assert.equal(await chosen(db), id);
    assert.equal(await chosen(db, boundsAt(), STRANGER), null);
  });
});

// --- Запись вопроса ----------------------------------------------------------

test("вопрос записан: открыт у задачи, у остальных снят, строка ask с сообщением", async () => {
  await withDatabase(async (db) => {
    const quietSince = ago(5 * DAY);
    const id = await seedTask(db, { needsReview: true });
    const other = await seedTask(db, { title: "встреча", question: "Во сколько?", askedAt: ago(2 * DAY) });
    const quiet = await seedTask(db, { title: "без вопроса", updated: quietSince });
    const foreign = await seedTask(db, { owner: STRANGER, question: "Во сколько?", askedAt: ago(HOUR) });

    const saved = await recordAsk(db, id, 4242);
    assert.ok(saved);
    assert.equal(saved.id, id);
    assert.equal(saved.open_question, QUESTION);
    assert.ok(saved.question_asked_at instanceof Date);
    assert.equal(saved.needs_review, true, "пометка не тронута");

    const row = await taskRow(db, id);
    assert.equal(row.open_question, QUESTION);
    assert.equal(row.needs_review, true);

    const cleared = await taskRow(db, other);
    assert.equal(cleared.open_question, null);
    assert.equal(cleared.question_asked_at, null);
    assert.equal(cleared.needs_review, false);

    // Задача без вопроса не тронута: `updated_at` прежний.
    assert.equal((await taskRow(db, quiet)).updated_at?.getTime(), quietSince.getTime());
    assert.equal((await taskRow(db, foreign)).open_question, "Во сколько?", "чужой вопрос на месте");

    const ask = only(await remindersOf(db, id));
    assert.equal(ask.stage, "ask");
    assert.equal(Number(ask.telegram_message_id), 4242);
    assert.equal(ask.sent_at?.getTime(), saved.question_asked_at.getTime());
    assert.equal(ask.fire_at.getTime(), saved.question_asked_at.getTime());
  });
});

test("без пометки «Перепроверьте» её и не появляется", async () => {
  await withDatabase(async (db) => {
    const id = await seedTask(db, { needsReview: false });
    const saved = await recordAsk(db, id);
    assert.equal(saved?.needs_review, false);
    assert.equal((await taskRow(db, id)).needs_review, false);
  });
});

test("повторный вопрос обновляет ту же строку ask", async () => {
  await withDatabase(async (db) => {
    const id = await seedTask(db);
    await seedAsk(db, id, ago(8 * DAY));

    await recordAsk(db, id, 5151);

    const ask = only(await remindersOf(db, id));
    assert.equal(Number(ask.telegram_message_id), 5151);
    assert.ok(ask.sent_at && ask.sent_at.getTime() > ago(MINUTE).getTime());
  });
});

test("null и ничего не записано: чужая, закрытая, убранная, со сроком, идея, нет такой", async () => {
  await withDatabase(async (db) => {
    const asking = await seedTask(db, { title: "встреча", question: "Во сколько?", askedAt: ago(HOUR) });
    const refused = [
      await seedTask(db, { owner: STRANGER }),
      await seedTask(db, { status: "done" }),
      await seedTask(db, { status: "cancelled" }),
      await seedTask(db, { due: FRIDAY_DUE }),
      await seedTask(db, { kind: "idea" }),
      await seedTask(db, { kind: "wish" }),
      "00000000-0000-4000-8000-000000000000",
    ];
    for (const id of refused) {
      assert.equal(await recordAsk(db, id), null, id);
    }

    const { rows } = await db.query<{ n: number }>(
      "select count(*)::int as n from public.reminders where stage = 'ask'",
    );
    assert.equal(rows[0]?.n, 0);
    assert.equal((await taskRow(db, asking)).open_question, "Во сколько?", "вопросы не сняты");
  });
});

test("пустой вопрос — отказ базы, а не запись", async () => {
  await withDatabase(async (db) => {
    const id = await seedTask(db);
    await assert.rejects(
      db.query("select * from public.record_ask($1, $2, $3, $4)", [OWNER, id, "  ", 1]),
      /question must not be empty/,
    );
    assert.deepEqual(await remindersOf(db, id), []);
  });
});

test("после вопроса: сегодня пусто, завтра — о другом деле, через неделю — снова о нём", async () => {
  await withDatabase(async (db) => {
    const first = await seedTask(db, { title: "первое", created: ago(2 * DAY) });
    const second = await seedTask(db, { title: "второе", created: ago(9 * DAY) });
    assert.equal(await chosen(db), first);

    await recordAsk(db, first);
    assert.equal(await chosen(db), null, "сегодня уже спрашивал");

    // Завтра в это же время: вопрос старше суток, полночь — час назад.
    const nowish = Date.now();
    const tomorrow = boundsAt(new Date(nowish + DAY + 2 * MINUTE), HOUR);
    assert.equal(await chosen(db, tomorrow), second);

    // Через неделю второе получило срок, а первое всё ещё без ответа.
    await db.query("update public.tasks set due_at = $2, due_precision = 'day' where id = $1", [
      second,
      FRIDAY_DUE,
    ]);
    const nextWeek = boundsAt(new Date(nowish + 7 * DAY), 9 * HOUR);
    const row = only(await toAsk(db, nextWeek));
    assert.equal(row.task_id, first);
    assert.ok(row.asked_at);
  });
});

// --- Строку ask не видят напоминания -----------------------------------------

test("строка ask не уходит напоминанием и не попадает в «Напомню» строки «Перенёс»", async () => {
  await withDatabase(async (db) => {
    await db.query("select public.save_owner_timezone($1, 'Asia/Yekaterinburg')", [OWNER]);
    const id = await seedTask(db);
    await recordAsk(db, id);

    const due = await db.query("select * from public.due_reminders($1, $2)", [OWNER, new Date(Date.now() + DAY)]);
    assert.deepEqual(due.rows, []);

    // Срок поставлен из приложения: строка «Перенёс» берёт ближайшее
    // неотправленное, а не время вопроса.
    await asRole(db, "authenticated", OWNER, async () => {
      await db.query("select * from public.edit_task($1::uuid, $2::jsonb)", [
        id,
        JSON.stringify({ due_date: "2030-10-04" }),
      ]);
    });
    const { rows } = await db.query<{ id: string; next_fire_at: Date | null }>(
      "select id, next_fire_at from public.moved_tasks($1)",
      [OWNER],
    );
    const row = only(rows);
    assert.equal(row.id, id);
    assert.equal(row.next_fire_at?.toISOString(), "2030-10-04T04:00:00.000Z");
  });
});

test("правка задачи строку ask не стирает", async () => {
  await withDatabase(async (db) => {
    await db.query("select public.save_owner_timezone($1, 'Asia/Yekaterinburg')", [OWNER]);
    const id = await seedTask(db);
    await recordAsk(db, id, 4242);

    await asRole(db, "authenticated", OWNER, async () => {
      await db.query("select * from public.edit_task($1::uuid, $2::jsonb)", [
        id,
        JSON.stringify({ title: "купить фильтр" }),
      ]);
    });
    await db.query("select * from public.edit_from_chat($1, $2::jsonb)", [
      OWNER,
      JSON.stringify({ task_id: id, action: "change", changes: { due_at: FRIDAY_DUE }, schedule: FRIDAY_PLAN }),
    ]);
    await db.query("select * from public.edit_from_chat($1, $2::jsonb)", [
      OWNER,
      JSON.stringify({ task_id: id, action: "change", changes: { due_at: null }, schedule: [] }),
    ]);

    const stages = (await remindersOf(db, id)).map((row) => [row.stage, Number(row.telegram_message_id)]);
    assert.deepEqual(stages, [["ask", 4242]]);
  });
});

test("свайп и «сделал» находят задачу по строке ask, «Сделано» под вопросом её закрывает", async () => {
  await withDatabase(async (db) => {
    const id = await seedTask(db);
    const since = ago(HOUR);
    await recordAsk(db, id, 4242);

    // Те же выборки, что `reminder_task_id` и `last_reminder_task` бота (§12.2):
    // по стадии они не фильтруют, и ушедшая строка ask им видна.
    const swiped = await db.query<{ task_id: string }>(
      `select task_id from public.reminders
        where owner_telegram_id = $1 and telegram_message_id = $2 limit 1`,
      [OWNER, 4242],
    );
    assert.equal(only(swiped.rows).task_id, id);
    const last = await db.query<{ task_id: string }>(
      `select task_id from public.reminders
        where owner_telegram_id = $1 and sent_at >= $2 order by sent_at desc limit 1`,
      [OWNER, since],
    );
    assert.equal(only(last.rows).task_id, id);

    const done = await db.query<{ status: string }>(
      "select status from public.mark_task_done($1, $2::uuid, null)",
      [OWNER, id],
    );
    assert.equal(only(done.rows).status, "done");
    assert.equal((await taskRow(db, id)).status, "done");
    assert.deepEqual((await remindersOf(db, id)).map((row) => row.stage), ["ask"]);
  });
});
