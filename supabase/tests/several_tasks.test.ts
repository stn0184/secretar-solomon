/**
 * Несколько дел в одном сообщении на настоящем Postgres (этап 023):
 * номер дела `tasks.source_item`, новая подпись `record_understanding` с
 * массивом `tasks`, `record_separately` по номеру и `append_reply`.
 *
 * Правила — `techspec/23-several-tasks.md` §23.6 и `techspec/03-schema.md`
 * §3.3–3.4. Бот зовёт функции ключом service-role — здесь это владелец
 * базы; права проверяются отдельно.
 */
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { test } from "node:test";

import type { PGlite } from "@electric-sql/pglite";

import { messageRow, withDatabase, withDatabaseBefore } from "./database.ts";

const OWNER = 777;
const STRANGER = 999;
const MIGRATION = "20261006100000_several_tasks.sql";

/** Четверг, 8 октября 2026 года, 10:00 у владельца (UTC+5) — 05:00 UTC. */
const CALL_AT = "2026-10-08T05:00:00.000Z";
/** Пятница, 9 октября, 18:00 у владельца — 13:00 UTC. */
const SUIT_AT = "2026-10-09T13:00:00.000Z";
/** Суббота, 10 октября, 12:00 у владельца — 07:00 UTC. */
const FLOWERS_AT = "2026-10-10T07:00:00.000Z";

type Json = null | boolean | number | string | Json[] | { [key: string]: Json };

interface TaskRow {
  id: string;
  title: string;
  kind: string;
  status: string;
  due_at: Date | null;
  open_question: string | null;
  question_asked_at: Date | null;
  needs_review: boolean;
  source_message_id: string | null;
  source_item: number | null;
  updated_at: Date;
}

interface ReplyRow {
  id: string;
  task_id: string | null;
  reply: string | null;
}

const CALL = {
  title: "позвонить Игорю",
  kind: "task",
  due_at: CALL_AT,
  due_precision: "time",
  priority: "normal",
  promise: null,
  people: ["Игорь"],
  needs_review: false,
} satisfies Json;

const SUIT = {
  title: "забрать костюм из химчистки",
  kind: "task",
  due_at: SUIT_AT,
  due_precision: "time",
  priority: "normal",
  promise: null,
  people: [],
  needs_review: false,
} satisfies Json;

const GIFT = {
  title: "подарок к годовщине",
  kind: "idea",
  due_at: null,
  due_precision: null,
  priority: "normal",
  promise: null,
  people: [],
  needs_review: false,
} satisfies Json;

const FLOWERS = {
  title: "купить цветы",
  kind: "task",
  due_at: FLOWERS_AT,
  due_precision: "time",
  priority: "normal",
  promise: null,
  people: [],
  needs_review: false,
} satisfies Json;

function plan(at: string): Json {
  return [{ stage: "due", fire_at: at }];
}

/** Дело сообщения так, как его передаёт бот: номер, поля, напоминания. */
function item(number: number, task: Json, reminders: Json = []): Json {
  return { item: number, task, reminders };
}

const THREE = [item(1, CALL, plan(CALL_AT)), item(2, SUIT, plan(SUIT_AT)), item(3, GIFT)];

let telegramMessageId = 0;

function only<T>(rows: T[]): T {
  assert.equal(rows.length, 1, `ждали одну строку, пришло ${rows.length}`);
  return rows[0]!;
}

function json(value: Json | undefined): string | null {
  return value === undefined ? null : JSON.stringify(value);
}

/** Первый шаг приёма (§3.4): сообщение в базе, разбора ещё нет. */
async function message(db: PGlite, owner = OWNER): Promise<string> {
  telegramMessageId += 1;
  const { rows } = await db.query<{ id: string }>(
    "select id from public.record_message($1, $2, $3, $4)",
    [owner, owner, telegramMessageId, "завтра позвонить Игорю, в пятницу забрать костюм"],
  );
  return only(rows).id;
}

interface Call {
  messageId: string;
  owner?: number;
  analysis?: Json;
  reply?: string | null;
  tasks?: Json;
  facts?: Json;
  amend?: Json;
  edit?: Json;
  sameTask?: string | null;
}

/** Второй шаг — вызов, как его делает бот: задачи сообщения по порядку. */
async function understand(db: PGlite, call: Call): Promise<TaskRow[]> {
  const { rows } = await db.query<TaskRow>(
    `select * from public.record_understanding(
       message_id => $1, owner_telegram_id => $2, analysis => $3::jsonb,
       ai_model => 'claude-opus-5', ai_input_tokens => 300, ai_output_tokens => 120,
       reply => $4, tasks => $5::jsonb, facts => $6::jsonb,
       amend => $7::jsonb, edit => $8::jsonb, same_task => $9::uuid
     )`,
    [
      call.messageId,
      call.owner ?? OWNER,
      json(call.analysis ?? { kind: "task", title: "позвонить Игорю", also: [] }),
      call.reply === undefined ? "Записал:\n1. Позвонить Игорю" : call.reply,
      json(call.tasks),
      json(call.facts ?? []),
      json(call.amend),
      json(call.edit),
      call.sameTask ?? null,
    ],
  );
  return rows;
}

/** Одна задача, записанная обычным путём; её id. */
async function recorded(db: PGlite, task: Json = CALL, owner = OWNER): Promise<string> {
  const rows = await understand(db, {
    messageId: await message(db, owner),
    owner,
    tasks: [item(1, task)],
    reply: "Записал.",
  });
  return only(rows).id;
}

async function taskOf(db: PGlite, id: string): Promise<TaskRow> {
  return only((await db.query<TaskRow>("select * from public.tasks where id = $1", [id])).rows);
}

async function tasksOf(db: PGlite, messageId: string): Promise<TaskRow[]> {
  const { rows } = await db.query<TaskRow>(
    "select * from public.tasks where source_message_id = $1 order by source_item",
    [messageId],
  );
  return rows;
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

async function separately(
  db: PGlite,
  messageId: string,
  number: number,
  task: Json,
  reply: string,
): Promise<ReplyRow> {
  const { rows } = await db.query<ReplyRow>(
    `select id, task_id, reply
       from public.record_separately($1, $2::uuid, $3::jsonb, $4::jsonb, $5, $6::smallint)`,
    [OWNER, messageId, JSON.stringify(task), "[]", reply, number],
  );
  return only(rows);
}

async function append(
  db: PGlite,
  messageId: string,
  paragraph: string,
  owner = OWNER,
): Promise<ReplyRow> {
  const { rows } = await db.query<ReplyRow>(
    "select id, task_id, reply from public.append_reply($1, $2::uuid, $3)",
    [owner, messageId, paragraph],
  );
  return only(rows);
}

// --- Несколько дел -------------------------------------------------------------

test("три дела — три задачи с номерами 1–3 и своими напоминаниями, сообщение ни к одной не привязано", () =>
  withDatabase(async (db) => {
    const messageId = await message(db);

    const rows = await understand(db, { messageId, tasks: THREE });

    assert.deepEqual(
      rows.map((row) => [row.source_item, row.title, row.kind]),
      [
        [1, "позвонить Игорю", "task"],
        [2, "забрать костюм из химчистки", "task"],
        [3, "подарок к годовщине", "idea"],
      ],
    );
    for (const row of rows) {
      assert.equal(row.source_message_id, messageId);
    }
    assert.equal(rows[0]!.due_at?.toISOString(), CALL_AT);
    assert.equal(rows[1]!.due_at?.toISOString(), SUIT_AT);
    assert.equal(await reminderCount(db, rows[0]!.id), 1);
    assert.equal(await reminderCount(db, rows[1]!.id), 1);
    assert.equal(await reminderCount(db, rows[2]!.id), 0);
    const saved = await messageRow(db, messageId);
    assert.equal(saved.task_id, null);
    assert.equal(saved.reply, "Записал:\n1. Позвонить Игорю");
  }));

test("дела пишутся по номерам, в каком бы порядке их ни передали", () =>
  withDatabase(async (db) => {
    const messageId = await message(db);

    const rows = await understand(db, {
      messageId,
      tasks: [item(3, GIFT), item(1, CALL), item(2, SUIT)],
    });

    assert.deepEqual(
      rows.map((row) => row.source_item),
      [1, 2, 3],
    );
  }));

test("одно дело номер 1 — сообщение ведёт на него, как до этапа", () =>
  withDatabase(async (db) => {
    const messageId = await message(db);

    const row = only(await understand(db, { messageId, tasks: [item(1, CALL, plan(CALL_AT))] }));

    assert.equal(row.source_item, 1);
    assert.equal((await messageRow(db, messageId)).task_id, row.id);
  }));

test("одно дело номер 2 при болтовне сверху — сообщение не привязано", () =>
  withDatabase(async (db) => {
    const messageId = await message(db);

    const row = only(await understand(db, { messageId, tasks: [item(2, SUIT)] }));

    assert.equal(row.source_item, 2);
    assert.equal((await messageRow(db, messageId)).task_id, null);
  }));

test("правка и новые дела одним вызовом: правленая первой, сообщение ведёт на неё", () =>
  withDatabase(async (db) => {
    const meeting = await recorded(db, { ...CALL, title: "встреча с Олегом", people: ["Олег"] });
    const messageId = await message(db);

    const rows = await understand(db, {
      messageId,
      edit: { task_id: meeting, action: "change", changes: { due_at: SUIT_AT, due_precision: "time" } },
      tasks: [item(2, FLOWERS, plan(FLOWERS_AT))],
      reply: "Перенёс: встреча с Олегом.\n\nЗаписал: купить цветы.",
    });

    assert.deepEqual(
      rows.map((row) => [row.id === meeting, row.source_item]),
      [
        [true, 1],
        [false, 2],
      ],
    );
    assert.equal((await taskOf(db, meeting)).due_at?.toISOString(), SUIT_AT);
    const fresh = rows[1]!;
    assert.equal(fresh.title, "купить цветы");
    assert.equal(fresh.source_message_id, messageId);
    assert.equal(await reminderCount(db, fresh.id), 1);
    assert.equal((await messageRow(db, messageId)).task_id, meeting);
  }));

test("ответ на вопрос и новое дело: задача с вопросом дополнена, новое записано", () =>
  withDatabase(async (db) => {
    const asked = await recorded(db, {
      ...CALL,
      due_at: null,
      due_precision: null,
      needs_review: true,
      open_question: "Когда позвонить?",
    });
    const messageId = await message(db);

    const rows = await understand(db, {
      messageId,
      amend: {
        task_id: asked,
        fields: { due_at: CALL_AT, due_precision: "time", needs_review: false },
        reminders: plan(CALL_AT),
      },
      tasks: [item(2, FLOWERS)],
      reply: "Дополнил: позвонить Игорю.\n\nЗаписал: купить цветы.",
    });

    assert.deepEqual(
      rows.map((row) => row.id === asked),
      [true, false],
    );
    const amended = await taskOf(db, asked);
    assert.equal(amended.due_at?.toISOString(), CALL_AT);
    assert.equal(amended.open_question, null);
    assert.equal(amended.question_asked_at, null);
    assert.equal(await reminderCount(db, asked), 1);
    assert.equal(rows[1]!.title, "купить цветы");
    assert.equal(rows[1]!.source_item, 2);
    assert.equal((await messageRow(db, messageId)).task_id, asked);
    assert.equal(await taskCount(db), 2);
  }));

test("вопрос у одного дела из нескольких: открыт ровно он, прежний снят", () =>
  withDatabase(async (db) => {
    const old = await recorded(db, { ...SUIT, open_question: "К какому часу?" });
    const messageId = await message(db);

    const rows = await understand(db, {
      messageId,
      tasks: [
        item(1, { ...CALL, people: [], needs_review: true, open_question: "Кому позвонить?" }),
        item(2, { ...FLOWERS, needs_review: true }),
      ],
    });

    assert.equal(rows[0]!.open_question, "Кому позвонить?");
    assert.ok(rows[0]!.question_asked_at);
    assert.equal(rows[1]!.open_question, null);
    assert.equal(rows[1]!.needs_review, true);
    assert.equal((await taskOf(db, old)).open_question, null);
  }));

// --- Повтор ---------------------------------------------------------------------

test("повтор сообщения о нескольких делах отдаёт те же задачи и ничего не пишет", () =>
  withDatabase(async (db) => {
    const messageId = await message(db);
    const first = await understand(db, { messageId, tasks: THREE, facts: [] });

    const again = await understand(db, {
      messageId,
      tasks: [item(1, FLOWERS), item(2, GIFT)],
      reply: "другой ответ",
      analysis: { kind: "chat" },
      facts: [{ category: "family", text: "У Игоря день рождения в мае", status: "fact" }],
    });

    assert.deepEqual(
      again.map((row) => row.id),
      first.map((row) => row.id),
    );
    assert.equal(await taskCount(db), 3);
    assert.equal(await factCount(db), 0);
    const saved = await messageRow(db, messageId);
    assert.equal(saved.reply, "Записал:\n1. Позвонить Игорю");
    assert.deepEqual(saved.analysis, { kind: "task", title: "позвонить Игорю", also: [] });
  }));

test("повтор сообщения из одних дублей и ждущего выбора ничего не пишет", () =>
  withDatabase(async (db) => {
    for (const reply of [
      "Это уже записано: позвонить Игорю.\n\nЭто уже записано: забрать костюм из химчистки.",
      "Какую задачу перенести?",
    ]) {
      const messageId = await message(db);
      assert.deepEqual(await understand(db, { messageId, reply }), []);

      const again = await understand(db, { messageId, tasks: THREE, reply: "Записал." });

      assert.deepEqual(again, []);
      assert.equal(await taskCount(db), 0);
      const saved = await messageRow(db, messageId);
      assert.equal(saved.reply, reply);
      assert.equal(saved.task_id, null);
    }
  }));

test("повтор сообщения с правкой и делами отдаёт правленую первой", () =>
  withDatabase(async (db) => {
    const meeting = await recorded(db, { ...CALL, title: "встреча с Олегом" });
    const messageId = await message(db);
    const edit = { task_id: meeting, action: "change", changes: { due_at: SUIT_AT, due_precision: "time" } };
    await understand(db, { messageId, edit, tasks: [item(2, FLOWERS), item(3, GIFT)] });

    const again = await understand(db, { messageId, edit, tasks: [item(2, FLOWERS), item(3, GIFT)] });

    assert.deepEqual(
      again.map((row) => [row.id === meeting, row.source_item]),
      [
        [true, 1],
        [false, 2],
        [false, 3],
      ],
    );
    assert.equal(await taskCount(db), 3);
  }));

// --- Отказы и откат -------------------------------------------------------------

test("два открытых вопроса из одного сообщения — отказ, ничего не записано", () =>
  withDatabase(async (db) => {
    const meeting = await recorded(db, { ...CALL, title: "встреча с Олегом" });
    const asking = { ...SUIT, open_question: "К какому часу?" };
    const mixes: Omit<Call, "messageId">[] = [
      { tasks: [item(1, { ...CALL, open_question: "Кому позвонить?" }), item(2, asking)] },
      {
        amend: { task_id: meeting, fields: {}, reminders: [], question: "Перенести на завтра?" },
        tasks: [item(2, asking)],
      },
      {
        edit: { task_id: meeting, action: "change", question: "Какой срок поставить?" },
        tasks: [item(2, asking)],
      },
    ];

    for (const mix of mixes) {
      const messageId = await message(db);
      await assert.rejects(understand(db, { ...mix, messageId }), /more than one open question/);
      const saved = await messageRow(db, messageId);
      assert.equal(saved.reply, null);
      assert.equal(saved.analysis, null);
    }
    assert.equal(await taskCount(db), 1);
  }));

test("same_task вместе с делами — отказ до всякой записи", () =>
  withDatabase(async (db) => {
    const found = await recorded(db);
    const messageId = await message(db);

    await assert.rejects(
      understand(db, { messageId, sameTask: found, tasks: [item(2, SUIT)] }),
      /same_task goes without tasks/,
    );

    assert.equal((await messageRow(db, messageId)).reply, null);
    assert.equal(await taskCount(db), 1);
  }));

test("tasks не массив — отказ", () =>
  withDatabase(async (db) => {
    const messageId = await message(db);

    await assert.rejects(understand(db, { messageId, tasks: CALL }), /tasks must be an array/);

    assert.equal(await taskCount(db), 0);
  }));

test("сбой посреди записи откатывает всё сообщение: ни задач, ни ответа, ни памяти", () =>
  withDatabase(async (db) => {
    const meeting = await recorded(db, { ...CALL, title: "встреча с Олегом" });
    const broken: Json[][] = [
      [item(1, CALL), item(1, SUIT)],
      [item(1, CALL), item(11, SUIT)],
      [item(1, CALL), item(0, SUIT)],
      // Повтор без срока — отказ вставки второго дела после первого.
      [item(1, CALL), item(2, { ...GIFT, kind: "task", repeat: { every: "day", interval: 1 } })],
    ];

    for (const tasks of broken) {
      const messageId = await message(db);
      await assert.rejects(
        understand(db, {
          messageId,
          tasks,
          edit: { task_id: meeting, action: "change", changes: { due_at: SUIT_AT, due_precision: "time" } },
          facts: [{ category: "family", text: "Игорь — брат", status: "fact" }],
        }),
      );
      const saved = await messageRow(db, messageId);
      assert.equal(saved.reply, null);
      assert.equal(saved.analysis, null);
      assert.equal(saved.task_id, null);
      assert.deepEqual(await tasksOf(db, messageId), []);
    }
    assert.equal(await taskCount(db), 1);
    assert.equal(await factCount(db), 0);
    assert.equal((await taskOf(db, meeting)).due_at?.toISOString(), CALL_AT);
  }));

test("чужое сообщение — отказ", () =>
  withDatabase(async (db) => {
    const messageId = await message(db, STRANGER);

    await assert.rejects(understand(db, { messageId, tasks: THREE }), /message .* is not owned by 777/);

    assert.equal(await taskCount(db), 0);
    assert.equal(await taskCount(db, STRANGER), 0);
  }));

// --- record_separately по номеру ------------------------------------------------

test("«Записать отдельно» дела номер 2 заводит его, сообщение не перепривязывает; второе нажатие не пишет", () =>
  withDatabase(async (db) => {
    const found = await recorded(db, SUIT);
    const messageId = await message(db);
    await understand(db, {
      messageId,
      tasks: [item(1, CALL), item(3, GIFT)],
      reply: "Записал:\n1. Позвонить Игорю\n2. Идея: подарок к годовщине\n\nЭто уже записано: забрать костюм.",
    });
    const reply =
      "Записал:\n1. Позвонить Игорю\n2. Идея: подарок к годовщине\n\nЭто уже записано: забрать костюм." +
      "\n\nЗаписал: забрать костюм из химчистки.";

    const first = await separately(db, messageId, 2, SUIT, reply);

    assert.equal(first.task_id, null);
    assert.equal(first.reply, reply);
    const rows = await tasksOf(db, messageId);
    assert.deepEqual(
      rows.map((row) => [row.source_item, row.title]),
      [
        [1, "позвонить Игорю"],
        [2, "забрать костюм из химчистки"],
        [3, "подарок к годовщине"],
      ],
    );
    assert.notEqual(rows[1]!.id, found);

    const second = await separately(db, messageId, 2, { ...SUIT, title: "другое" }, "другой ответ");

    assert.equal(second.reply, reply);
    assert.equal((await tasksOf(db, messageId)).length, 3);
  }));

test("«Записать отдельно» без номера в базе нет: номер вне 1–10 — отказ", () =>
  withDatabase(async (db) => {
    const messageId = await message(db);
    await understand(db, { messageId, reply: "Это уже записано: позвонить Игорю." });

    await assert.rejects(separately(db, messageId, 11, CALL, "x"), /item must be between 1 and 10/);

    assert.equal(await taskCount(db), 0);
  }));

// --- append_reply ---------------------------------------------------------------

test("append_reply дописывает абзац через пустую строку и не удваивает его", () =>
  withDatabase(async (db) => {
    const messageId = await message(db);
    await understand(db, { messageId, tasks: THREE, reply: "Записал:\n1. Позвонить Игорю" });

    const first = await append(db, messageId, "Вернул: позвонить Игорю.");
    const second = await append(db, messageId, "Вернул: позвонить Игорю.");
    const blank = await append(db, messageId, "  ");

    const expected = "Записал:\n1. Позвонить Игорю\n\nВернул: позвонить Игорю.";
    assert.equal(first.reply, expected);
    assert.equal(second.reply, expected);
    assert.equal(blank.reply, expected);
    assert.equal((await messageRow(db, messageId)).reply, expected);
  }));

test("append_reply к сообщению без ответа пишет сам абзац", () =>
  withDatabase(async (db) => {
    const messageId = await message(db);

    const saved = await append(db, messageId, "Вернул: позвонить Игорю.");

    assert.equal(saved.reply, "Вернул: позвонить Игорю.");
  }));

test("append_reply к чужому сообщению или несуществующему — отказ", () =>
  withDatabase(async (db) => {
    const strangers = await message(db, STRANGER);

    for (const messageId of [strangers, randomUUID()]) {
      await assert.rejects(append(db, messageId, "Вернул."), /append_reply: message .* is not owned by 777/);
    }
    assert.equal((await messageRow(db, strangers)).reply, null);
  }));

// --- Схема ------------------------------------------------------------------------

test("номер дела есть ровно у задач из сообщения, от 1 до 10, и у сообщения не повторяется", () =>
  withDatabase(async (db) => {
    const messageId = await message(db);
    const insert = (source: string | null, number: number | null) =>
      db.query(
        `insert into public.tasks (owner_telegram_id, title, source_message_id, source_item)
         values ($1, 'позвонить Игорю', $2::uuid, $3::smallint)`,
        [OWNER, source, number],
      );

    await insert(null, null);
    await insert(messageId, 10);
    await assert.rejects(insert(messageId, null), /tasks_source_item_check/);
    await assert.rejects(insert(null, 1), /tasks_source_item_check/);
    await assert.rejects(insert(messageId, 11), /tasks_source_item_check/);
    await assert.rejects(insert(messageId, 0), /tasks_source_item_check/);
    await assert.rejects(insert(messageId, 10), /tasks_source_item_key/);
    assert.equal(await taskCount(db), 2);
  }));

test("миграция ставит номер 1 задачам из сообщений, задачам без сообщения — ничего, updated_at прежний", () =>
  withDatabaseBefore(MIGRATION, async (db, migrate) => {
    const { rows: messages } = await db.query<{ id: string }>(
      `insert into public.messages (owner_telegram_id, chat_id, telegram_message_id, text)
       values ($1, $1, 1, 'позвонить Игорю') returning id`,
      [OWNER],
    );
    const { rows: before } = await db.query<{ id: string; updated_at: Date }>(
      `insert into public.tasks (owner_telegram_id, title, source_message_id, updated_at)
       values ($1, 'позвонить Игорю', $2, '2026-10-01T10:00:00Z'),
              ($1, 'купить цветы', null, '2026-10-01T10:00:00Z')
       returning id, updated_at`,
      [OWNER, only(messages).id],
    );

    await migrate();

    const fromMessage = await taskOf(db, before[0]!.id);
    const manual = await taskOf(db, before[1]!.id);
    assert.equal(fromMessage.source_item, 1);
    assert.equal(manual.source_item, null);
    assert.equal(fromMessage.updated_at.toISOString(), "2026-10-01T10:00:00.000Z");
    assert.equal(manual.updated_at.toISOString(), "2026-10-01T10:00:00.000Z");
  }));

// --- Права ------------------------------------------------------------------------

test("у каждой функции одна перегрузка, и зовёт их только ключ бота", () =>
  withDatabase(async (db) => {
    const bot = { anon: false, authenticated: false, service_role: true };
    const expected: Record<string, RegExp> = {
      record_understanding: /reply text, tasks jsonb, facts jsonb, .*photo_text text, same_task uuid$/,
      insert_message_task: /reminders jsonb, item smallint$/,
      record_separately: /reply text, item smallint$/,
      append_reply: /^owner_telegram_id bigint, message_id uuid, paragraph text$/,
    };

    for (const [name, args] of Object.entries(expected)) {
      const { rows } = await db.query<{ oid: number; args: string }>(
        `select p.oid, pg_get_function_identity_arguments(p.oid) as args
           from pg_proc p
          where p.proname = $1 and p.pronamespace = 'public'::regnamespace`,
        [name],
      );
      const found = only(rows);
      assert.match(found.args, args, `аргументы ${name}`);
      const granted = await db.query<{ role: string; allowed: boolean }>(
        `select role, has_function_privilege(role, $1::oid, 'execute') as allowed
           from unnest(array['anon', 'authenticated', 'service_role']) as role`,
        [found.oid],
      );
      assert.deepEqual(
        Object.fromEntries(granted.rows.map((row) => [row.role, row.allowed])),
        bot,
        `права ${name}`,
      );
    }
  }));

test("под ключом бота сообщение о нескольких делах пишется целиком, вместе с вложенной вставкой", () =>
  withDatabase(async (db) => {
    const messageId = await message(db);
    await db.exec("set role service_role");
    try {
      const rows = await understand(db, { messageId, tasks: THREE });
      assert.equal(rows.length, 3);
      await append(db, messageId, "Вернул: позвонить Игорю.");
      await separately(db, messageId, 4, FLOWERS, "Записал: купить цветы.");
    } finally {
      await db.exec("reset role");
    }
    assert.equal(await taskCount(db), 4);
  }));
