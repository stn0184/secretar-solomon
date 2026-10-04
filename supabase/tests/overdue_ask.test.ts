/**
 * Вопрос о прошедшем деле (§22): `overdue_to_ask` выбирает дело,
 * `record_overdue_ask` записывает ушедший вопрос.
 *
 * Обе функции зовёт только бот ключом service-role; вызовы здесь идут от
 * владельца базы, права — отдельным тестом. Границы, которые считает бот
 * (`services/overdue.py`), тест задаёт от настоящего «сейчас»:
 * `record_overdue_ask` ставит время базы (`now()`) и сверяет срок с ним же.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import type { PGlite } from "@electric-sql/pglite";

import { asRole, withDatabase } from "./database.ts";

const OWNER = 777;
const STRANGER = 999;
const QUESTION = "Получилось?";

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
  quietSince: Date | null;
}

interface AskRow {
  task_id: string;
  title: string;
  due_at: Date;
  due_precision: string | null;
  asked_at: Date | null;
}

interface TaskRow {
  id: string | null;
  title: string | null;
  status: string | null;
  open_question: string | null;
  question_asked_at: Date | null;
  needs_review: boolean | null;
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
 * Границы отдельного вопроса, как их считает бот: полночь «сегодня» — за
 * `sinceMidnight` до `now`, неделя — шесть полночей назад, сутки и 15 минут.
 */
function stepBounds(now = new Date(), sinceMidnight = HOUR): Bounds {
  const dayStart = ago(sinceMidnight, now.getTime());
  return {
    dayStart,
    askedBefore: ago(6 * DAY, dayStart.getTime()),
    questionSince: ago(DAY, now.getTime()),
    quietSince: ago(15 * MINUTE, now.getTime()),
  };
}

/** Границы плана: живой вопрос — заданный сегодня, тишина не проверяется. */
function planBounds(now = new Date(), sinceMidnight = HOUR): Bounds {
  const step = stepBounds(now, sinceMidnight);
  return { ...step, questionSince: step.dayStart, quietSince: null };
}

/** Вчерашний срок при полночи час назад: вчера, около 18:00. */
function yesterday(): Date {
  return ago(7 * HOUR);
}

function only<T>(rows: T[]): T {
  assert.equal(rows.length, 1, `ждали одну строку, пришло ${rows.length}`);
  return rows[0]!;
}

interface Seed {
  owner?: number;
  title?: string;
  kind?: "task" | "idea" | "wish";
  due?: Date | string | null;
  status?: "active" | "done" | "cancelled";
  created?: Date;
  needsReview?: boolean;
  question?: string | null;
  askedAt?: Date | null;
  repeat?: Record<string, unknown> | null;
}

/** Задача прямой вставкой; срок по умолчанию — вчерашний. */
async function seedTask(db: PGlite, seed: Seed = {}): Promise<string> {
  const due = seed.due === undefined ? yesterday() : seed.due;
  const repeat = seed.repeat ?? null;
  const created = seed.created ?? ago(10 * DAY);
  const { rows } = await db.query<{ id: string }>(
    `insert into public.tasks (
       owner_telegram_id, title, kind, due_at, due_precision, status, created_at, updated_at,
       needs_review, open_question, question_asked_at, repeat, occurrence_at
     )
     values ($1, $2, $3, $4, $5, $6, $7, $7, $8, $9, $10, $11, $12) returning id`,
    [
      seed.owner ?? OWNER,
      seed.title ?? "позвонить в сервис",
      seed.kind ?? "task",
      due,
      due === null ? null : "day",
      seed.status ?? "active",
      created,
      seed.needsReview ?? false,
      seed.question ?? null,
      seed.askedAt ?? null,
      repeat === null ? null : JSON.stringify(repeat),
      repeat === null ? null : due,
    ],
  );
  return only(rows).id;
}

/** Строка `overdue`, будто вопрос о задаче ушёл в `sentAt`. */
async function seedOverdue(db: PGlite, taskId: string, sentAt: Date, owner = OWNER): Promise<void> {
  await db.query(
    `insert into public.reminders (owner_telegram_id, task_id, stage, fire_at, sent_at, telegram_message_id)
     values ($1, $2, 'overdue', $3, $3, 1)`,
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

async function toAsk(db: PGlite, bounds = stepBounds(), owner = OWNER): Promise<AskRow[]> {
  const { rows } = await db.query<AskRow>("select * from public.overdue_to_ask($1, $2, $3, $4, $5)", [
    owner,
    bounds.dayStart,
    bounds.askedBefore,
    bounds.questionSince,
    bounds.quietSince,
  ]);
  return rows;
}

async function chosen(db: PGlite, bounds = stepBounds(), owner = OWNER): Promise<string | null> {
  const rows = await toAsk(db, bounds, owner);
  assert.ok(rows.length <= 1, `ждали не больше одной строки, пришло ${rows.length}`);
  return rows[0]?.task_id ?? null;
}

async function recordOverdue(
  db: PGlite,
  taskId: string,
  messageId: number | null = 4242,
  owner = OWNER,
): Promise<TaskRow | null> {
  const { rows } = await db.query<TaskRow>("select * from public.record_overdue_ask($1, $2, $3, $4)", [
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
    "select id, title, status, open_question, question_asked_at, needs_review from public.tasks where id = $1",
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

async function close(db: PGlite, id: string): Promise<void> {
  await db.query("update public.tasks set status = 'done' where id = $1", [id]);
}

/** Владелец ответил: любая запись разбора снимает вопрос (§10.3). */
async function answered(db: PGlite): Promise<void> {
  await db.query(
    `update public.tasks set open_question = null, question_asked_at = null
      where owner_telegram_id = $1 and (open_question is not null or question_asked_at is not null)`,
    [OWNER],
  );
}

// --- Схема и права -----------------------------------------------------------

test("у каждой функции одна перегрузка, и зовёт её только service_role", async () => {
  await withDatabase(async (db) => {
    const signatures = [
      "public.overdue_to_ask(bigint, timestamptz, timestamptz, timestamptz, timestamptz)",
      "public.record_overdue_ask(bigint, uuid, text, bigint)",
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

test("ступень overdue пускается в reminders, незнакомая — нет", async () => {
  await withDatabase(async (db) => {
    const id = await seedTask(db);
    await seedOverdue(db, id, ago(HOUR));
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

test("спрашивается только активная разовая задача владельца со сроком раньше сегодняшнего дня", async () => {
  await withDatabase(async (db) => {
    const bounds = stepBounds();
    await seedTask(db, { kind: "idea", title: "идея" });
    await seedTask(db, { kind: "wish", title: "желание" });
    await seedTask(db, { status: "done", title: "закрыта" });
    await seedTask(db, { status: "cancelled", title: "убрана" });
    await seedTask(db, { owner: STRANGER, title: "чужая" });
    await seedTask(db, { title: "повторяющаяся", repeat: { every: "day", interval: 1 } });
    await seedTask(db, { title: "сегодня, час прошёл", due: ago(30 * MINUTE) });
    await seedTask(db, { title: "впереди", due: new Date(Date.now() + DAY) });
    await seedTask(db, { title: "без срока", due: null });
    const recent = await seedTask(db, { title: "спрашивал позавчера", due: ago(3 * DAY) });
    await seedOverdue(db, recent, ago(2 * DAY));

    assert.deepEqual(await toAsk(db, bounds), []);

    const right = await seedTask(db, { title: "отправить расчёт" });
    const row = only(await toAsk(db, bounds));
    assert.equal(row.task_id, right);
    assert.equal(row.title, "отправить расчёт");
    assert.equal(row.due_precision, "day");
    assert.ok(row.due_at.getTime() < bounds.dayStart.getTime());
    assert.equal(row.asked_at, null);
  });
});

test("о спрошенном деле — снова через семь дней по календарю, не раньше", async () => {
  await withDatabase(async (db) => {
    const bounds = stepBounds();
    const id = await seedTask(db, { due: ago(30 * DAY) });

    // Спрашивал ровно шесть полночей назад, на границе: ещё рано.
    await seedOverdue(db, id, bounds.askedBefore);
    assert.equal(await chosen(db, bounds), null);

    const earlier = ago(MINUTE, bounds.askedBefore.getTime());
    await db.query("update public.reminders set sent_at = $2 where task_id = $1", [id, earlier]);
    const row = only(await toAsk(db, bounds));
    assert.equal(row.task_id, id);
    assert.equal(row.asked_at?.getTime(), earlier.getTime());
  });
});

test("вопрос о прежнем сроке не в счёт: перенесённое и снова прошедшее — как в первый раз", async () => {
  await withDatabase(async (db) => {
    const id = await seedTask(db, { due: yesterday() });
    // Спрашивал три дня назад — тогда срок был другим, раньше нынешнего.
    await seedOverdue(db, id, ago(3 * DAY));

    const row = only(await toAsk(db));
    assert.equal(row.task_id, id);
    assert.equal(row.asked_at, null);
  });
});

test("порядок: не спрошенные от позднего срока к раннему, потом спрошенные давнее всех", async () => {
  await withDatabase(async (db) => {
    const bounds = stepBounds();
    const due = yesterday();
    const older = await seedTask(db, { title: "давнее", due: ago(5 * DAY) });
    const twinOld = await seedTask(db, { title: "вчерашнее, записано раньше", due, created: ago(20 * DAY) });
    const twinNew = await seedTask(db, { title: "вчерашнее, записано позже", due, created: ago(3 * DAY) });
    const moved = await seedTask(db, { title: "перенесённое после вопроса", due: ago(2 * DAY) });
    await seedOverdue(db, moved, ago(4 * DAY));
    const longAsked = await seedTask(db, { title: "спрашивал давно", due: ago(40 * DAY) });
    const lateAsked = await seedTask(db, { title: "спрашивал позже", due: ago(50 * DAY) });
    await seedOverdue(db, longAsked, ago(20 * DAY));
    await seedOverdue(db, lateAsked, ago(9 * DAY));

    const order: string[] = [];
    for (;;) {
      const id = await chosen(db, bounds);
      if (id === null) break;
      order.push(id);
      await close(db, id);
    }
    assert.deepEqual(order, [twinNew, twinOld, moved, older, longAsked, lateAsked]);
  });
});

// --- Когда вопроса нет -------------------------------------------------------

test("живой открытый вопрос младше суток — шаг ждёт; старше суток или у закрытой — нет", async () => {
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

test("план не перебивает вопрос, заданный сегодня, а вчерашний — да", async () => {
  await withDatabase(async (db) => {
    const id = await seedTask(db);
    const asking = await seedTask(db, {
      title: "отчёт",
      due: null,
      question: "К какому сроку?",
      askedAt: ago(10 * MINUTE),
    });
    assert.equal(await chosen(db, planBounds()), null, "вопрос после полуночи");

    // Задан вчера вечером, меньше суток назад: шаг ждёт, план — нет.
    await db.query("update public.tasks set question_asked_at = $2 where id = $1", [asking, ago(3 * HOUR)]);
    assert.equal(await chosen(db, stepBounds()), null);
    assert.equal(await chosen(db, planBounds()), id);
  });
});

test("владелец писал меньше 15 минут назад — шаг ждёт, план тишины не ждёт", async () => {
  await withDatabase(async (db) => {
    const id = await seedTask(db);
    await seedMessage(db, ago(20 * MINUTE));
    assert.equal(await chosen(db), id, "20 минут — тишина");

    await seedMessage(db, ago(5 * MINUTE));
    assert.equal(await chosen(db), null);
    assert.equal(await chosen(db, planBounds()), id);
  });
});

test("бот присылал напоминание или вопрос меньше 15 минут назад — шаг ждёт", async () => {
  await withDatabase(async (db) => {
    const id = await seedTask(db);
    const dated = await seedTask(db, { title: "со сроком", due: FRIDAY_DUE });
    await db.query(
      `insert into public.reminders (owner_telegram_id, task_id, stage, fire_at, sent_at)
       values ($1, $2, 'before', $3, $3), ($1, $2, 'due', $4, null)`,
      [OWNER, dated, ago(20 * MINUTE), FRIDAY_DUE],
    );
    assert.equal(await chosen(db), id, "20 минут и неотправленное — тишина");

    for (const stage of ["before", "ask", "overdue"]) {
      await db.query("update public.reminders set stage = $2, sent_at = $3 where task_id = $1 and stage <> 'due'", [
        dated,
        stage,
        ago(5 * MINUTE),
      ]);
      assert.equal(await chosen(db), null, stage);
      assert.equal(await chosen(db, planBounds()), id, `план: ${stage}`);
    }
  });
});

test("утренний план ушёл меньше 15 минут назад — шаг ждёт", async () => {
  await withDatabase(async (db) => {
    const id = await seedTask(db);
    const today = new Date().toISOString().slice(0, 10);
    await db.query(
      `insert into public.morning_plans (owner_telegram_id, day, telegram_message_id, created_at)
       values ($1, $2::date, 1, $3)`,
      [OWNER, today, ago(20 * MINUTE)],
    );
    assert.equal(await chosen(db), id, "план 20 минут назад — тишина");

    await db.query("update public.morning_plans set created_at = $2 where owner_telegram_id = $1", [
      OWNER,
      ago(5 * MINUTE),
    ]);
    assert.equal(await chosen(db), null);

    await db.query("update public.morning_plans set owner_telegram_id = $1", [STRANGER]);
    assert.equal(await chosen(db), id, "чужой план не мешает");
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
    await seedOverdue(db, foreign, ago(MINUTE), STRANGER);
    await seedMessage(db, ago(MINUTE), STRANGER);

    assert.equal(await chosen(db), id);
    assert.equal(await chosen(db, stepBounds(), STRANGER), null);
  });
});

test("вопрос о прошедшем деле — тишина и для вопроса о деле без срока", async () => {
  await withDatabase(async (db) => {
    const undated = await seedTask(db, { title: "купить фильтр", due: null, created: ago(3 * DAY) });
    const id = await seedTask(db);
    await recordOverdue(db, id);
    await answered(db);

    const undatedToAsk = async (bounds: Bounds): Promise<string[]> => {
      const { rows } = await db.query<{ task_id: string }>(
        "select task_id from public.undated_to_ask($1, $2, $3, $4, $5)",
        [OWNER, bounds.dayStart, bounds.askedBefore, bounds.questionSince, bounds.quietSince],
      );
      return rows.map((row) => row.task_id);
    };
    assert.deepEqual(await undatedToAsk(stepBounds()), [], "вопрос ушёл только что");

    // Через 20 минут тишины — уходит, как раньше. Полночь та же: записи
    // вопроса снятие трогало только задачу с вопросом.
    const later = stepBounds(new Date(Date.now() + 20 * MINUTE), HOUR + 20 * MINUTE);
    assert.deepEqual(await undatedToAsk(later), [undated]);
  });
});

// --- Запись вопроса ----------------------------------------------------------

test("вопрос записан: открыт у задачи, у остальных снят, строка overdue с сообщением", async () => {
  await withDatabase(async (db) => {
    const id = await seedTask(db, { needsReview: true });
    const other = await seedTask(db, { title: "встреча", due: null, question: "Во сколько?", askedAt: ago(2 * DAY) });
    const foreign = await seedTask(db, { owner: STRANGER, question: "Во сколько?", askedAt: ago(HOUR) });

    const saved = await recordOverdue(db, id, 4242);
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
    assert.equal((await taskRow(db, foreign)).open_question, "Во сколько?", "чужой вопрос на месте");

    const ask = only(await remindersOf(db, id));
    assert.equal(ask.stage, "overdue");
    assert.equal(Number(ask.telegram_message_id), 4242);
    assert.equal(ask.sent_at?.getTime(), saved.question_asked_at.getTime());
    assert.equal(ask.fire_at.getTime(), saved.question_asked_at.getTime());
  });
});

test("вопрос в плане пишется без сообщения, пометки «Перепроверьте» не появляется", async () => {
  await withDatabase(async (db) => {
    const id = await seedTask(db, { needsReview: false });
    const saved = await recordOverdue(db, id, null);
    assert.equal(saved?.needs_review, false);
    assert.equal(saved?.open_question, QUESTION);

    const ask = only(await remindersOf(db, id));
    assert.equal(ask.stage, "overdue");
    assert.equal(ask.telegram_message_id, null);
    assert.ok(ask.sent_at);
  });
});

test("повторный вопрос обновляет ту же строку overdue", async () => {
  await withDatabase(async (db) => {
    const id = await seedTask(db, { due: ago(20 * DAY) });
    await seedOverdue(db, id, ago(8 * DAY));

    await recordOverdue(db, id, 5151);

    const ask = only(await remindersOf(db, id));
    assert.equal(Number(ask.telegram_message_id), 5151);
    assert.ok(ask.sent_at && ask.sent_at.getTime() > ago(MINUTE).getTime());
  });
});

test("null и ничего не записано: чужая, закрытая, убранная, повторяющаяся, впереди, без срока, идея", async () => {
  await withDatabase(async (db) => {
    const asking = await seedTask(db, { title: "встреча", due: null, question: "Во сколько?", askedAt: ago(HOUR) });
    const refused = [
      await seedTask(db, { owner: STRANGER }),
      await seedTask(db, { status: "done" }),
      await seedTask(db, { status: "cancelled" }),
      await seedTask(db, { repeat: { every: "day", interval: 1 } }),
      await seedTask(db, { due: new Date(Date.now() + HOUR) }),
      await seedTask(db, { due: null }),
      await seedTask(db, { kind: "idea" }),
      await seedTask(db, { kind: "wish" }),
      "00000000-0000-4000-8000-000000000000",
    ];
    for (const id of refused) {
      assert.equal(await recordOverdue(db, id), null, id);
    }

    const { rows } = await db.query<{ n: number }>(
      "select count(*)::int as n from public.reminders where stage = 'overdue'",
    );
    assert.equal(rows[0]?.n, 0);
    assert.equal((await taskRow(db, asking)).open_question, "Во сколько?", "вопросы не сняты");
  });
});

test("пустой вопрос — отказ базы, а не запись", async () => {
  await withDatabase(async (db) => {
    const id = await seedTask(db);
    await assert.rejects(
      db.query("select * from public.record_overdue_ask($1, $2, $3, $4)", [OWNER, id, "  ", 1]),
      /question must not be empty/,
    );
    assert.deepEqual(await remindersOf(db, id), []);
  });
});

test("после вопроса: пока он жив — пусто, ответили — следующее, через неделю — снова о нём", async () => {
  await withDatabase(async (db) => {
    const first = await seedTask(db, { title: "первое", due: yesterday() });
    const second = await seedTask(db, { title: "второе", due: ago(3 * DAY) });
    assert.equal(await chosen(db), first);

    await recordOverdue(db, first);
    assert.equal(await chosen(db), null, "вопрос жив");

    // Ответили «нет», вопрос снят: через 15 минут тишины — о следующем.
    await answered(db);
    const later = stepBounds(new Date(Date.now() + 20 * MINUTE), HOUR + 20 * MINUTE);
    assert.equal(await chosen(db, later), second);

    // Через неделю второе закрыто, а первое всё не закрыто.
    await close(db, second);
    const nextWeek = stepBounds(new Date(Date.now() + 7 * DAY), 9 * HOUR);
    const row = only(await toAsk(db, nextWeek));
    assert.equal(row.task_id, first);
    assert.ok(row.asked_at);
  });
});

test("перенесли после вопроса, и новый срок прошёл — спрашивает как в первый раз", async () => {
  await withDatabase(async (db) => {
    const id = await seedTask(db);
    await recordOverdue(db, id);
    await answered(db);

    // Перенесли на «через два часа» — срок ещё впереди.
    await db.query("select * from public.edit_from_chat($1, $2::jsonb)", [
      OWNER,
      JSON.stringify({
        task_id: id,
        action: "change",
        changes: { due_at: new Date(Date.now() + 2 * HOUR).toISOString() },
        schedule: [],
      }),
    ]);
    assert.equal(await chosen(db), null, "срок впереди");

    // Через два дня прошёл и он: вопрос о прежнем сроке не в счёт.
    const later = stepBounds(new Date(Date.now() + 2 * DAY), HOUR);
    const row = only(await toAsk(db, later));
    assert.equal(row.task_id, id);
    assert.equal(row.asked_at, null);
  });
});

// --- Строку overdue не видят напоминания -------------------------------------

test("строка overdue не уходит напоминанием и не попадает в «Напомню» строки «Перенёс»", async () => {
  await withDatabase(async (db) => {
    await db.query("select public.save_owner_timezone($1, 'Asia/Yekaterinburg')", [OWNER]);
    const id = await seedTask(db);
    await recordOverdue(db, id);

    const due = await db.query("select * from public.due_reminders($1, $2)", [OWNER, new Date(Date.now() + DAY)]);
    assert.deepEqual(due.rows, []);

    // Срок перенесён из приложения: строка «Перенёс» берёт ближайшее
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

test("перенос задачи словом строку overdue не стирает", async () => {
  await withDatabase(async (db) => {
    const id = await seedTask(db);
    await recordOverdue(db, id, 4242);

    await db.query("select * from public.edit_from_chat($1, $2::jsonb)", [
      OWNER,
      JSON.stringify({ task_id: id, action: "change", changes: { due_at: FRIDAY_DUE }, schedule: FRIDAY_PLAN }),
    ]);

    const stages = (await remindersOf(db, id)).map((row) => [
      row.stage,
      row.telegram_message_id === null ? null : Number(row.telegram_message_id),
    ]);
    assert.deepEqual(stages, [
      ["before", null],
      ["due", null],
      ["overdue", 4242],
    ]);
  });
});

test("свайп и «сделал» находят задачу по строке overdue, «Сделано» под вопросом её закрывает", async () => {
  await withDatabase(async (db) => {
    const id = await seedTask(db);
    const since = ago(HOUR);
    await recordOverdue(db, id, 4242);

    // Те же выборки, что `reminder_task_id` и `last_reminder_task` бота (§12.2):
    // по ступени они не фильтруют, и ушедшая строка overdue им видна.
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
  });
});
