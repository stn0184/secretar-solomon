/**
 * `record_understanding` на настоящем Postgres: разбор, память, вопрос и ответ.
 *
 * Бот в своих тестах базу подменяет, и ошибка внутри plpgsql до живой базы
 * доезжает незамеченной. Здесь функция исполняется целиком — миграции
 * применены к PGlite (`database.ts`), вызов идёт с теми же аргументами, что
 * шлёт бот (`bot/src/solomon/db/tasks.py`): списки — всегда списками,
 * отсутствующее — SQL `null`.
 *
 * Правила — `techspec/03-schema.md` §3.4 и `techspec/10-dialog.md` §10.1–10.3.
 */
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { test } from "node:test";

import type { PGlite } from "@electric-sql/pglite";

import { withDatabase } from "./database.ts";

const OWNER = 777;
const STRANGER = 999;
const ASKED = "К какому сроку?";

type Json = null | boolean | number | string | Json[] | { [key: string]: Json };

interface TaskRow {
  id: string | null;
  title: string;
  status: string;
  due_at: Date | null;
  due_precision: string | null;
  priority: string;
  promise: string | null;
  people: string[];
  needs_review: boolean;
  open_question: string | null;
  question_asked_at: Date | null;
  source_message_id: string | null;
}

interface ReminderRow {
  stage: string;
  fire_at: Date;
  sent_at: Date | null;
  telegram_message_id: string | number | null;
}

/** Разбор модели, как его пишет бот; содержимое функции не важно. */
const ANALYSIS: Json = { kind: "task", title: "отправить расчёт клиенту" };

/** Поручение, по которому бот спрашивает срок (§10.1). */
const ERRAND = {
  title: "отправить расчёт клиенту",
  kind: "task",
  due_at: null,
  due_precision: null,
  priority: "high",
  promise: "mine",
  people: ["клиент"],
  needs_review: true,
} satisfies Json;

const FRIDAY_DUE = "2026-10-02T13:00:00.000Z";
const FRIDAY_MORNING = "2026-10-02T04:00:00.000Z";
const MONDAY_DUE = "2026-10-05T13:00:00.000Z";
const MONDAY_MORNING = "2026-10-05T04:00:00.000Z";

let telegramMessageId = 0;

function only<T>(rows: T[]): T {
  assert.equal(rows.length, 1, `ждали одну строку, пришло ${rows.length}`);
  return rows[0]!;
}

/** Первый шаг приёма (§3.4): сообщение в базе, разбора ещё нет. */
async function message(db: PGlite, owner = OWNER, kind = "text"): Promise<string> {
  telegramMessageId += 1;
  const { rows } = await db.query<{ id: string }>(
    "select id from public.record_message($1, $2, $3, $4, $5)",
    [owner, owner, telegramMessageId, kind === "text" ? "сообщение" : "", kind],
  );
  return only(rows).id;
}

interface Call {
  messageId: string;
  owner?: number;
  /** Разбор модели; `null` — разбора нет (отказ модели, «не расслышал»). */
  analysis?: Json;
  task?: Json;
  reminders?: Json[];
  facts?: Json[];
  amend?: Json;
}

function json(value: Json | undefined): string | null {
  return value === undefined || value === null ? null : JSON.stringify(value);
}

/** Второй шаг приёма — вызов, как его делает бот. Задачи нет — `null`. */
async function understand(db: PGlite, call: Call): Promise<TaskRow | null> {
  const analysis = call.analysis === undefined ? ANALYSIS : call.analysis;
  const understood = analysis !== null;
  const { rows } = await db.query<TaskRow>(
    `select * from public.record_understanding(
       message_id => $1, owner_telegram_id => $2, analysis => $3::jsonb,
       ai_model => $4, ai_input_tokens => $5, ai_output_tokens => $6,
       reply => $7,
       -- Дело сообщения — одно, номер 1 (§23.6).
       tasks => case when $8::jsonb is null then null
                  else jsonb_build_array(jsonb_build_object('item', 1, 'task', $8::jsonb, 'reminders', $9::jsonb)) end,
       facts => $10::jsonb,
       transcript => null, transcript_confidence => null, amend => $11::jsonb
     )`,
    [
      call.messageId,
      call.owner ?? OWNER,
      json(analysis),
      understood ? "claude-opus-5" : null,
      understood ? 120 : null,
      understood ? 45 : null,
      "ответ бота",
      json(call.task),
      json(call.reminders ?? []),
      json(call.facts ?? []),
      json(call.amend),
    ],
  );
  // Задачи нет — функция не отдаёт ни строки.
  return rows.length === 0 ? null : only(rows);
}

/** Поручение с открытым вопросом — то, что оставляет первый проход (§10.1). */
async function askedTask(db: PGlite, owner = OWNER): Promise<TaskRow> {
  const messageId = await message(db, owner);
  const row = await understand(db, {
    messageId,
    owner,
    task: { ...ERRAND, open_question: ASKED },
  });
  assert.ok(row, "задача с вопросом не заведена");
  return row;
}

async function taskById(db: PGlite, id: string): Promise<TaskRow> {
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
    `select stage, fire_at, sent_at, telegram_message_id
       from public.reminders where task_id = $1 order by stage`,
    [taskId],
  );
  return rows;
}

async function savedMessage(
  db: PGlite,
  id: string,
): Promise<{ analysis: Json; reply: string | null }> {
  const { rows } = await db.query<{ analysis: Json; reply: string | null }>(
    "select analysis, reply from public.messages where id = $1",
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

function assertQuestionOpen(row: TaskRow): void {
  assert.equal(row.open_question, ASKED);
  assert.ok(row.question_asked_at, "у открытого вопроса нет времени");
}

function assertQuestionClosed(row: TaskRow): void {
  assert.equal(row.open_question, null);
  assert.equal(row.question_asked_at, null);
}

// --- Функция как таковая -----------------------------------------------------

test("у функции одна перегрузка, и зовёт её только service_role", () =>
  withDatabase(async (db) => {
    const { rows } = await db.query<{ oid: number }>(
      `select p.oid from pg_proc p join pg_namespace n on n.oid = p.pronamespace
        where n.nspname = 'public' and p.proname = 'record_understanding'`,
    );
    // Две перегрузки PostgREST различать нечем (§3.4).
    const { oid } = only(rows);

    for (const [role, allowed] of [
      ["anon", false],
      ["authenticated", false],
      ["service_role", true],
    ] as const) {
      const { rows: granted } = await db.query<{ ok: boolean }>(
        "select has_function_privilege($1, $2::oid, 'execute') as ok",
        [role, oid],
      );
      assert.equal(only(granted).ok, allowed, `execute у ${role}`);
    }
  }));

test("пустой список памяти не мешает записи — бот шлёт его на каждом разборе", () =>
  withDatabase(async (db) => {
    const messageId = await message(db);

    const row = await understand(db, { messageId, analysis: { kind: "chat" }, facts: [] });

    assert.equal(row, null);
    assert.equal((await savedMessage(db, messageId)).reply, "ответ бота");
  }));

test("память пишется один раз, а предположение, сказанное прямо, становится фактом", () =>
  withDatabase(async (db) => {
    const guess = { category: "car", text: "Машина — Toyota Camry", status: "guess" };
    await understand(db, { messageId: await message(db), facts: [guess] });
    await understand(db, { messageId: await message(db), facts: [guess] });
    await understand(db, {
      messageId: await message(db),
      facts: [{ ...guess, status: "fact" }],
    });

    const { rows } = await db.query<{ status: string }>(
      "select status from public.facts where owner_telegram_id = $1",
      [OWNER],
    );
    assert.deepEqual(rows, [{ status: "fact" }]);
  }));

// --- Вопрос (§10.1) ---------------------------------------------------------

test("вопрос пишется в новую задачу: без пробелов по краям и со временем", () =>
  withDatabase(async (db) => {
    const messageId = await message(db);

    const row = await understand(db, {
      messageId,
      task: { ...ERRAND, open_question: `  ${ASKED}  ` },
    });

    assert.ok(row);
    assertQuestionOpen(row);
    assert.equal(row.needs_review, true);
    assert.equal(row.source_message_id, messageId);
  }));

test("пустой вопрос — вопроса нет", () =>
  withDatabase(async (db) => {
    const row = await understand(db, {
      messageId: await message(db),
      task: { ...ERRAND, open_question: "   " },
    });

    assert.ok(row);
    assertQuestionClosed(row);
  }));

test("новый вопрос снимает прежний, а вопрос другого владельца не трогает", () =>
  withDatabase(async (db) => {
    const first = await askedTask(db);
    const strangers = await askedTask(db, STRANGER);

    const second = await askedTask(db);

    const before = await taskById(db, first.id!);
    assertQuestionClosed(before);
    // Задача осталась с пометкой — пусть посмотрит человек (§10.1).
    assert.equal(before.needs_review, true);
    assertQuestionOpen(await taskById(db, second.id!));
    assertQuestionOpen(await taskById(db, strangers.id!));
  }));

test("повтор по тому же сообщению отдаёт прежнюю задачу и вопросов не трогает", () =>
  withDatabase(async (db) => {
    const messageId = await message(db);
    const task = { ...ERRAND, open_question: ASKED };
    const first = await understand(db, { messageId, task });

    const again = await understand(db, { messageId, task });

    assert.ok(first && again);
    assert.equal(again.id, first.id);
    assert.equal(await taskCount(db), 1);
    assertQuestionOpen(await taskById(db, first.id!));
  }));

// --- Чем снимается вопрос (§10.3) --------------------------------------------

test("разговор без задачи снимает открытый вопрос", () =>
  withDatabase(async (db) => {
    const asked = await askedTask(db);

    const row = await understand(db, {
      messageId: await message(db),
      analysis: { kind: "chat" },
      task: null,
    });

    assert.equal(row, null);
    assertQuestionClosed(await taskById(db, asked.id!));
  }));

test("вопрос старше суток снимается следующей записью", () =>
  withDatabase(async (db) => {
    const asked = await askedTask(db);
    await db.query(
      "update public.tasks set question_asked_at = now() - interval '25 hours' where id = $1",
      [asked.id],
    );

    await understand(db, { messageId: await message(db), analysis: { kind: "chat" } });

    assertQuestionClosed(await taskById(db, asked.id!));
  }));

test("«не расслышал» — ни разбора, ни задачи — открытый вопрос оставляет", () =>
  withDatabase(async (db) => {
    const asked = await askedTask(db);
    const voice = await message(db, OWNER, "voice");

    const row = await understand(db, { messageId: voice, analysis: null, task: null });

    assert.equal(row, null);
    // Бот сам просит повторить — повтор должен застать вопрос открытым.
    assertQuestionOpen(await taskById(db, asked.id!));
    assert.equal((await savedMessage(db, voice)).reply, "ответ бота");
  }));

test("отказ модели заводит задачу как есть и снимает вопрос, как любое поручение", () =>
  withDatabase(async (db) => {
    const asked = await askedTask(db);

    const row = await understand(db, {
      messageId: await message(db),
      analysis: null,
      task: { ...ERRAND, title: "в пятницу", priority: "normal", promise: null, people: [] },
    });

    assert.ok(row);
    assert.notEqual(row.id, asked.id);
    assertQuestionClosed(await taskById(db, asked.id!));
  }));

// --- Ответ на вопрос (§10.2) --------------------------------------------------

test("ответ меняет только поля из fields, новой задачи нет, вопрос снят", () =>
  withDatabase(async (db) => {
    const asked = await askedTask(db);
    const answer = await message(db);

    const row = await understand(db, {
      messageId: answer,
      amend: {
        task_id: asked.id,
        fields: { due_at: FRIDAY_DUE, due_precision: "day", needs_review: false },
        reminders: [
          { stage: "before", fire_at: FRIDAY_MORNING },
          { stage: "due", fire_at: FRIDAY_DUE },
        ],
      },
    });

    assert.ok(row);
    assert.equal(row.id, asked.id);
    assert.equal(await taskCount(db), 1);
    const saved = await taskById(db, asked.id!);
    assert.equal(saved.due_at?.toISOString(), FRIDAY_DUE);
    assert.equal(saved.due_precision, "day");
    assert.equal(saved.needs_review, false);
    // Чего нет в fields — остаётся как было.
    assert.equal(saved.title, ERRAND.title);
    assert.equal(saved.priority, "high");
    assert.equal(saved.promise, "mine");
    assert.deepEqual(saved.people, ["клиент"]);
    // Источник задачи — первое сообщение, а не ответ.
    assert.equal(saved.source_message_id, asked.source_message_id);
    assertQuestionClosed(saved);
    assert.deepEqual(
      (await remindersOf(db, asked.id!)).map((r) => [r.stage, r.fire_at.toISOString()]),
      [
        ["before", FRIDAY_MORNING],
        ["due", FRIDAY_DUE],
      ],
    );
    assert.equal((await savedMessage(db, answer)).reply, "ответ бота");
  }));

test("ответ заменяет неотправленные напоминания и взводит ушедшую ступень заново", () =>
  withDatabase(async (db) => {
    const messageId = await message(db);
    const asked = await understand(db, {
      messageId,
      task: { ...ERRAND, due_at: FRIDAY_DUE, due_precision: "day", open_question: ASKED },
      reminders: [
        { stage: "before", fire_at: FRIDAY_MORNING },
        { stage: "due", fire_at: FRIDAY_DUE },
      ],
    });
    assert.ok(asked);
    await db.query(
      `update public.reminders set sent_at = now(), telegram_message_id = 55
        where task_id = $1 and stage = 'before'`,
      [asked.id],
    );

    await understand(db, {
      messageId: await message(db),
      amend: {
        task_id: asked.id,
        fields: { due_at: MONDAY_DUE, due_precision: "day", needs_review: false },
        reminders: [
          { stage: "before", fire_at: MONDAY_MORNING },
          { stage: "due", fire_at: MONDAY_DUE },
        ],
      },
    });

    const [before, due] = await remindersOf(db, asked.id!);
    assert.equal(before?.fire_at.toISOString(), MONDAY_MORNING);
    assert.equal(before?.sent_at, null);
    assert.equal(before?.telegram_message_id, null);
    assert.equal(due?.fire_at.toISOString(), MONDAY_DUE);
    assert.equal(due?.sent_at, null);
  }));

test("ответ без нового плана снимает неотправленное, ушедшее остаётся", () =>
  withDatabase(async (db) => {
    const asked = await understand(db, {
      messageId: await message(db),
      task: { ...ERRAND, due_at: FRIDAY_DUE, due_precision: "day", open_question: ASKED },
      reminders: [
        { stage: "before", fire_at: FRIDAY_MORNING },
        { stage: "due", fire_at: FRIDAY_DUE },
      ],
    });
    assert.ok(asked);
    await db.query(
      "update public.reminders set sent_at = now() where task_id = $1 and stage = 'before'",
      [asked.id],
    );

    await understand(db, {
      messageId: await message(db),
      amend: { task_id: asked.id, fields: { needs_review: true }, reminders: [] },
    });

    const left = await remindersOf(db, asked.id!);
    assert.deepEqual(
      left.map((r) => [r.stage, r.sent_at !== null]),
      [["before", true]],
    );
  }));

// --- Отказ ответа: ничего не записано ---------------------------------------

/**
 * Ответ, который база обязана отвергнуть целиком: ни разбора с ответом бота,
 * ни памяти, ни снятого вопроса — транзакция откатывается (§3.4).
 */
async function assertAmendRefused(db: PGlite, taskId: string): Promise<void> {
  const asked = await askedTask(db);
  const answer = await message(db);

  await assert.rejects(
    understand(db, {
      messageId: answer,
      facts: [{ category: "work", text: "Работает с клиентами", status: "guess" }],
      amend: { task_id: taskId, fields: { due_at: FRIDAY_DUE, due_precision: "day" }, reminders: [] },
    }),
    /record_understanding: task .* is not an active task of 777/,
  );

  assert.deepEqual(await savedMessage(db, answer), { analysis: null, reply: null });
  assert.equal(await factCount(db), 0);
  assertQuestionOpen(await taskById(db, asked.id!));
}

test("ответ с чужой задачей — отказ, ничего не записано", () =>
  withDatabase(async (db) => {
    const strangers = await askedTask(db, STRANGER);

    await assertAmendRefused(db, strangers.id!);

    const untouched = await taskById(db, strangers.id!);
    assert.equal(untouched.due_at, null);
    assertQuestionOpen(untouched);
  }));

test("ответ с несуществующей задачей — отказ, ничего не записано", () =>
  withDatabase(async (db) => {
    await assertAmendRefused(db, randomUUID());
  }));

test("ответ по уже закрытой задаче — отказ: напоминать по ней некому", () =>
  withDatabase(async (db) => {
    const closed = await askedTask(db);
    await db.query("update public.tasks set status = 'done' where id = $1", [closed.id]);

    await assertAmendRefused(db, closed.id!);

    const untouched = await taskById(db, closed.id!);
    assert.equal(untouched.due_at, null);
    assert.deepEqual(await remindersOf(db, closed.id!), []);
  }));

test("ответ по убранной задаче — отказ: её больше не надо делать", () =>
  withDatabase(async (db) => {
    const cancelled = await askedTask(db);
    await db.query("update public.tasks set status = 'cancelled' where id = $1", [cancelled.id]);

    await assertAmendRefused(db, cancelled.id!);

    const untouched = await taskById(db, cancelled.id!);
    assert.equal(untouched.status, "cancelled");
    assert.equal(untouched.due_at, null);
    assert.deepEqual(await remindersOf(db, cancelled.id!), []);
  }));

// --- Новый вопрос в ответе (§22.5) ----------------------------------------------

const MOVE_QUESTION = "На когда перенести?";

test("amend.question открывает новый вопрос той же задачи со временем, needs_review — из fields", () =>
  withDatabase(async (db) => {
    const asked = await askedTask(db);
    const other = await askedTask(db);
    const answer = await message(db);

    const row = await understand(db, {
      messageId: answer,
      amend: {
        task_id: other.id,
        fields: { needs_review: false },
        reminders: [],
        question: `  ${MOVE_QUESTION}  `,
      },
    });

    assert.ok(row);
    assert.equal(row.open_question, MOVE_QUESTION);
    const saved = await taskById(db, other.id!);
    assert.equal(saved.open_question, MOVE_QUESTION);
    assert.ok(saved.question_asked_at, "у нового вопроса нет времени");
    assert.ok(saved.question_asked_at.getTime() > Date.now() - 60_000);
    assert.equal(saved.needs_review, false);
    // Срок не менялся: в fields его нет.
    assert.equal(saved.due_at, null);
    // Вопрос у владельца один: остальные сняты.
    assertQuestionClosed(await taskById(db, asked.id!));
  }));

test("amend.question без needs_review в fields пометку не трогает", () =>
  withDatabase(async (db) => {
    const asked = await askedTask(db);
    const answer = await message(db);

    await understand(db, {
      messageId: answer,
      amend: { task_id: asked.id, fields: {}, reminders: [], question: MOVE_QUESTION },
    });

    const saved = await taskById(db, asked.id!);
    assert.equal(saved.open_question, MOVE_QUESTION);
    assert.equal(saved.needs_review, true, "как было у поручения");
  }));

test("пустой amend.question — вопроса нет, как раньше", () =>
  withDatabase(async (db) => {
    const asked = await askedTask(db);

    for (const question of ["", "   ", null]) {
      const answer = await message(db);
      await understand(db, {
        messageId: answer,
        amend: { task_id: asked.id, fields: { needs_review: false }, reminders: [], question },
      });
      assertQuestionClosed(await taskById(db, asked.id!));
    }
  }));

// --- Часть дня (§21.2) ----------------------------------------------------------

/** Пятница, 2 октября 2026 года, 08:00 у владельца — начало утра. */
const FRIDAY_EARLY = "2026-10-02T03:00:00.000Z";

test("поручение с частью дня пишется как пришло: начало части, часть и одно напоминание", () =>
  withDatabase(async (db) => {
    for (const [precision, start] of [
      ["morning", FRIDAY_EARLY],
      ["afternoon", "2026-10-02T07:00:00.000Z"],
      ["evening", FRIDAY_DUE],
    ] as const) {
      const messageId = await message(db);

      const row = await understand(db, {
        messageId,
        task: { ...ERRAND, title: "встреча с Ренатой", due_at: start, due_precision: precision, needs_review: false },
        reminders: [{ stage: "due", fire_at: start }],
      });

      assert.ok(row, precision);
      const saved = await taskById(db, row.id!);
      assert.equal(saved.due_at?.toISOString(), start);
      assert.equal(saved.due_precision, precision);
      assert.deepEqual(
        (await remindersOf(db, row.id!)).map((r) => [r.stage, r.fire_at.toISOString()]),
        [["due", start]],
      );
    }
  }));

test("ответ на вопрос частью дня — срок ложится частью", () =>
  withDatabase(async (db) => {
    const asked = await askedTask(db);
    const answer = await message(db);

    await understand(db, {
      messageId: answer,
      amend: {
        task_id: asked.id,
        fields: { due_at: FRIDAY_EARLY, due_precision: "morning", needs_review: false },
        reminders: [{ stage: "due", fire_at: FRIDAY_EARLY }],
      },
    });

    const saved = await taskById(db, asked.id!);
    assert.equal(saved.due_at?.toISOString(), FRIDAY_EARLY);
    assert.equal(saved.due_precision, "morning");
    assertQuestionClosed(saved);
  }));

test("точность не из пяти — отказ, ничего не записано", () =>
  withDatabase(async (db) => {
    const messageId = await message(db);

    await assert.rejects(
      understand(db, {
        messageId,
        task: { ...ERRAND, due_at: FRIDAY_EARLY, due_precision: "noon" },
        reminders: [],
      }),
      /tasks_due_precision_check/,
    );

    assert.equal(await taskCount(db), 0);
    assert.equal((await savedMessage(db, messageId)).reply, null);
  }));
