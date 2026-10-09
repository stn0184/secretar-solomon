/**
 * Сферы жизни на настоящем Postgres (этап 032): таблица `spheres` под правилом
 * «owner only», колонка `sphere_id` у задач, памяти и чатов с составным ключом,
 * поиск и заведение по названию, сферы в записи разбора, правке словом,
 * разборе чатов и отчёте.
 *
 * Правила — `techspec/30-spheres.md` и `techspec/03-schema.md` §3.17. Бот зовёт
 * функции ключом service-role — здесь это владелец базы; права проверяются
 * отдельно. Сферы и люди выдуманные: VoiceFin, РЕЙВА, Игорь.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import type { PGlite } from "@electric-sql/pglite";

import { asRole, withDatabase } from "./database.ts";

const OWNER = 777;
const STRANGER = 999;
const CONNECTION = "biz-777";
const IGOR = "1001";

type Json = null | boolean | number | string | Json[] | { [key: string]: Json };

interface SphereRow {
  id: string;
  name: string;
  removed_at: Date | null;
}

interface TaskRow {
  id: string;
  title: string;
  sphere_id: string | null;
}

function only<T>(rows: T[]): T {
  assert.equal(rows.length, 1, `ждали одну строку, пришло ${rows.length}`);
  return rows[0]!;
}

function json(value: Json | undefined): string | null {
  return value === undefined ? null : JSON.stringify(value);
}

let telegramMessageId = 0;

/** Первый шаг приёма (§3.4): сообщение в базе, разбора ещё нет. */
async function message(db: PGlite, owner = OWNER): Promise<string> {
  telegramMessageId += 1;
  const { rows } = await db.query<{ id: string }>(
    "select id from public.record_message($1, $2, $3, $4)",
    [owner, owner, telegramMessageId, "мои сферы: VoiceFin, РЕЙВА, семья"],
  );
  return only(rows).id;
}

interface Call {
  owner?: number;
  tasks?: Json;
  facts?: Json;
  edit?: Json;
  spheres?: Json;
}

/** Второй шаг — как его делает бот; задачи сообщения. */
async function understand(db: PGlite, call: Call): Promise<TaskRow[]> {
  const owner = call.owner ?? OWNER;
  const { rows } = await db.query<TaskRow>(
    `select * from public.record_understanding(
       message_id => $1, owner_telegram_id => $2, analysis => '{"kind": "sphere"}'::jsonb,
       ai_model => 'claude-opus-5', ai_input_tokens => 300, ai_output_tokens => 120,
       reply => 'ok', tasks => $3::jsonb, facts => $4::jsonb, edit => $5::jsonb,
       spheres => $6::jsonb
     )`,
    [
      await message(db, owner),
      owner,
      json(call.tasks ?? []),
      json(call.facts ?? []),
      json(call.edit),
      json(call.spheres),
    ],
  );
  return rows;
}

async function alive(db: PGlite, owner = OWNER): Promise<string[]> {
  const { rows } = await db.query<{ name: string }>(
    `select name from public.spheres
      where owner_telegram_id = $1 and removed_at is null
      order by created_at, name`,
    [owner],
  );
  return rows.map((row) => row.name);
}

async function sphereId(db: PGlite, name: string, owner = OWNER): Promise<string> {
  const { rows } = await db.query<SphereRow>(
    "select * from public.spheres where owner_telegram_id = $1 and name = $2 and removed_at is null",
    [owner, name],
  );
  return only(rows).id;
}

async function taskOf(db: PGlite, id: string): Promise<TaskRow> {
  return only((await db.query<TaskRow>("select * from public.tasks where id = $1", [id])).rows);
}

function task(title: string, sphere: string | null = null): Json {
  return {
    title,
    kind: "task",
    due_at: null,
    due_precision: null,
    priority: "normal",
    promise: null,
    people: [],
    needs_review: false,
    sphere,
  };
}

/** Одна задача обычным путём; её id. */
async function recorded(db: PGlite, title: string, sphere: string | null = null): Promise<string> {
  const rows = await understand(db, { tasks: [{ item: 1, task: task(title, sphere), reminders: [] }] });
  return only(rows).id;
}

// --- Чаты ---------------------------------------------------------------------

/** Чат Игоря с согласием и одним сообщением; его id и сообщения. */
async function chat(db: PGlite): Promise<{ threadId: string; ids: string[] }> {
  await db.query("select public.connect_chat_source($1, 'telegram', $2, true)", [OWNER, CONNECTION]);
  await db.query("select public.mark_consent_asked($1, 'telegram')", [OWNER]);
  await db.query("select public.answer_consent($1, 'telegram', true)", [OWNER]);
  return more(db, "1");
}

/** Ещё одно сообщение Игоря; чат и id неразобранных. */
async function more(db: PGlite, external: string): Promise<{ threadId: string; ids: string[] }> {
  await db.query(
    `select * from public.store_chat_message($1, 'telegram', $2, $3, 'Игорь Петров', $4, 'in',
                                             'Игорь Петров', now(), 'text', 'Созвон по подписке завтра?', true, null)`,
    [OWNER, CONNECTION, IGOR, external],
  );
  const { rows } = await db.query<{ thread_id: string; id: string }>(
    `select m.thread_id, m.id from public.chat_messages m
      where m.owner_telegram_id = $1 and m.analysis_id is null`,
    [OWNER],
  );
  return { threadId: rows[0]!.thread_id, ids: rows.map((row) => row.id) };
}

async function analyze(
  db: PGlite,
  threadId: string,
  ids: string[],
  sphere: string | null,
  titles: string[] = ["созвон с Игорем по подписке"],
): Promise<string> {
  const tasks = titles.map((title, index) => ({
    item: index + 1,
    task: { title, due_at: null, due_precision: null, promise: "mine", people: ["Игорь"] },
    reminders: [],
  }));
  const { rows } = await db.query<{ id: string }>(
    `select public.record_chat_analysis($1, $2, $3::uuid[], '{}'::jsonb, 'claude-opus-5', 1500, 120, 3000,
                                        'Игорем', null, $4::jsonb, $5) as id`,
    [OWNER, threadId, ids, JSON.stringify(tasks), sphere],
  );
  return only(rows).id;
}

async function threadSphere(db: PGlite, threadId: string): Promise<string | null> {
  const { rows } = await db.query<{ name: string | null }>(
    `select s.name from public.chat_threads t left join public.spheres s on s.id = t.sphere_id
      where t.id = $1`,
    [threadId],
  );
  return only(rows).name;
}

async function chatTaskSpheres(db: PGlite, threadId: string): Promise<(string | null)[]> {
  const { rows } = await db.query<{ name: string | null }>(
    `select s.name from public.tasks k
       join public.chat_analyses a on a.id = k.chat_analysis_id
       left join public.spheres s on s.id = k.sphere_id
      where a.thread_id = $1
      order by a.created_at, k.chat_item`,
    [threadId],
  );
  return rows.map((row) => row.name);
}

// --- Таблица и права ------------------------------------------------------------

test("сферы под правилом «owner only»: владелец видит свои, anon — ничего", () =>
  withDatabase(async (db) => {
    await understand(db, { spheres: { add: ["VoiceFin"] } });
    await understand(db, { owner: STRANGER, spheres: { add: ["Дача"] } });

    const own = await asRole(db, "authenticated", OWNER, async () =>
      (await db.query<SphereRow>("select * from public.spheres")).rows.map((row) => row.name),
    );
    assert.deepEqual(own, ["VoiceFin"]);
    const anon = await asRole(db, "anon", null, async () =>
      (await db.query<SphereRow>("select * from public.spheres")).rows,
    );
    assert.deepEqual(anon, []);
  }));

test("название — 1–40 знаков без пробелов по краям; живые не повторяются без учёта регистра", () =>
  withDatabase(async (db) => {
    const insert = (name: string) =>
      db.query("insert into public.spheres (owner_telegram_id, name) values ($1, $2)", [OWNER, name]);
    await assert.rejects(insert(""), /spheres_name_check/);
    await assert.rejects(insert(" VoiceFin"), /spheres_name_check/);
    await assert.rejects(insert("я".repeat(41)), /spheres_name_check/);
    await insert("РЕЙВА");
    await assert.rejects(insert("рейва"), /spheres_name_key/);
    await db.query("insert into public.spheres (owner_telegram_id, name) values ($1, 'рейва')", [STRANGER]);
  }));

test("задача, запись и чат не ссылаются на сферу другого владельца", () =>
  withDatabase(async (db) => {
    await understand(db, { owner: STRANGER, spheres: { add: ["Дача"] } });
    const foreign = await sphereId(db, "Дача", STRANGER);
    const taskId = await recorded(db, "купить лампочку");
    await assert.rejects(
      db.query("update public.tasks set sphere_id = $1 where id = $2", [foreign, taskId]),
      /tasks_sphere_fkey/,
    );
  }));

test("функции сфер зовёт только service_role", () =>
  withDatabase(async (db) => {
    for (const signature of [
      "public.sphere_id_of(bigint, text, boolean)",
      "public.reported_chat(bigint, bigint)",
      "public.chat_report(bigint, uuid)",
      "public.record_chat_analysis(bigint, uuid, uuid[], jsonb, text, integer, integer, integer, text, jsonb, jsonb, text)",
    ]) {
      const { rows } = await db.query<{ role: string; allowed: boolean }>(
        `select role, has_function_privilege(role, $1, 'execute') as allowed
           from unnest(array['anon', 'authenticated', 'service_role']) as role`,
        [signature],
      );
      assert.deepEqual(
        Object.fromEntries(rows.map((row) => [row.role, row.allowed])),
        { anon: false, authenticated: false, service_role: true },
        signature,
      );
    }
  }));

// --- Заведение и убирание -------------------------------------------------------------

test("«мои сферы: …» заводит сферы; та же ещё раз и в другом регистре — не дублируется", () =>
  withDatabase(async (db) => {
    await understand(db, { spheres: { add: ["VoiceFin", "РЕЙВА", "семья"] } });
    await understand(db, { spheres: { add: ["voicefin", " РЕЙВА "] } });
    assert.deepEqual((await alive(db)).sort(), ["VoiceFin", "РЕЙВА", "семья"].sort());
  }));

test("живых сфер не больше 12: тринадцатая — отказ целиком", () =>
  withDatabase(async (db) => {
    const twelve = Array.from({ length: 12 }, (_, index) => `сфера ${index + 1}`);
    await understand(db, { spheres: { add: twelve } });
    await assert.rejects(understand(db, { spheres: { add: ["тринадцатая"] } }), /12 spheres/);
    assert.equal((await alive(db)).length, 12);
  }));

test("убранная сфера снимается с дел, записей и чатов; та же заводится заново новой строкой", () =>
  withDatabase(async (db) => {
    await understand(db, { spheres: { add: ["семья", "VoiceFin"] } });
    const family = await sphereId(db, "семья");
    const taskId = await recorded(db, "забрать Мишу", "семья");
    await understand(db, { facts: [{ category: "family", text: "Сын Миша", status: "fact", sphere: "семья" }] });
    const { threadId, ids } = await chat(db);
    await analyze(db, threadId, ids, "семья");

    await understand(db, { spheres: { drop: ["Семья"] } });

    assert.deepEqual(await alive(db), ["VoiceFin"]);
    assert.equal((await taskOf(db, taskId)).sphere_id, null);
    const { rows: facts } = await db.query<{ sphere_id: string | null }>("select sphere_id from public.facts");
    assert.deepEqual(facts, [{ sphere_id: null }]);
    assert.equal(await threadSphere(db, threadId), null);
    const { rows: removed } = await db.query<SphereRow>("select * from public.spheres where id = $1", [family]);
    assert.ok(only(removed).removed_at !== null);

    // §30.5: убрать и завести заново — можно.
    await understand(db, { spheres: { add: ["семья"] } });
    assert.deepEqual((await alive(db)).sort(), ["VoiceFin", "семья"].sort());
    assert.notEqual(await sphereId(db, "семья"), family);
  }));

test("убрать сферу, которой нет, — ничего не меняет", () =>
  withDatabase(async (db) => {
    await understand(db, { spheres: { add: ["VoiceFin"] } });
    await understand(db, { spheres: { drop: ["спорт"] } });
    assert.deepEqual(await alive(db), ["VoiceFin"]);
  }));

// --- Дела, знания, правка -----------------------------------------------------------

test("дело получает сферу из списка; сферы нет — без сферы, и новая не заводится", () =>
  withDatabase(async (db) => {
    await understand(db, { spheres: { add: ["VoiceFin"] } });
    const known = await recorded(db, "созвон с бухгалтерами", "voicefin");
    const unknown = await recorded(db, "купить хлеб", "покупки");

    assert.equal((await taskOf(db, known)).sphere_id, await sphereId(db, "VoiceFin"));
    assert.equal((await taskOf(db, unknown)).sphere_id, null);
    assert.deepEqual(await alive(db), ["VoiceFin"]);
  }));

test("знание о сфере — запись памяти со сферой; сферы нет — заводится; повтор ставит сферу", () =>
  withDatabase(async (db) => {
    await understand(db, {
      facts: [{ category: "work", text: "Продаём подписку бухгалтерам", status: "fact", sphere: "VoiceFin" }],
    });
    assert.deepEqual(await alive(db), ["VoiceFin"]);
    const { rows } = await db.query<{ sphere_id: string | null; status: string }>(
      "select sphere_id, status from public.facts",
    );
    assert.deepEqual(rows, [{ sphere_id: await sphereId(db, "VoiceFin"), status: "fact" }]);

    await understand(db, { facts: [{ category: "work", text: "Отвечаю за продажи", status: "fact" }] });
    await understand(db, {
      facts: [{ category: "work", text: "Отвечаю за продажи", status: "fact", sphere: "РЕЙВА" }],
    });
    const { rows: again } = await db.query<{ name: string | null }>(
      `select s.name from public.facts f left join public.spheres s on s.id = f.sphere_id
        where f.text = 'Отвечаю за продажи'`,
    );
    assert.deepEqual(again, [{ name: "РЕЙВА" }]);
  }));

test("правка «это по X» меняет сферу дела, заводит новую; пусто — снимает", () =>
  withDatabase(async (db) => {
    await understand(db, { spheres: { add: ["VoiceFin"] } });
    const taskId = await recorded(db, "созвон с бухгалтерами", "VoiceFin");

    const moved = await understand(db, { edit: { task_id: taskId, action: "sphere", sphere: "РЕЙВА" } });
    assert.equal(only(moved).id, taskId);
    assert.equal((await taskOf(db, taskId)).sphere_id, await sphereId(db, "РЕЙВА"));

    await understand(db, { edit: { task_id: taskId, action: "sphere", sphere: null } });
    assert.equal((await taskOf(db, taskId)).sphere_id, null);
  }));

test("правка сферы закрытой или чужой задачи — отказ целиком", () =>
  withDatabase(async (db) => {
    const taskId = await recorded(db, "созвон");
    await db.query("update public.tasks set status = 'done' where id = $1", [taskId]);
    await assert.rejects(
      understand(db, { edit: { task_id: taskId, action: "sphere", sphere: "РЕЙВА" } }),
      /not an active task/,
    );
    assert.deepEqual(await alive(db), []);
  }));

// --- Чаты ----------------------------------------------------------------------------

test("разбор чата ставит сферу чату и его делам; она липкая — следующий разбор её не меняет", () =>
  withDatabase(async (db) => {
    await understand(db, { spheres: { add: ["VoiceFin", "РЕЙВА"] } });
    const { threadId, ids } = await chat(db);
    const first = await analyze(db, threadId, ids, "voicefin");
    assert.equal(await threadSphere(db, threadId), "VoiceFin");

    const next = await more(db, "2");
    await analyze(db, threadId, next.ids, "РЕЙВА", ["Игорь пришлёт договор"]);
    assert.equal(await threadSphere(db, threadId), "VoiceFin");
    assert.deepEqual(await chatTaskSpheres(db, threadId), ["VoiceFin", "VoiceFin"]);

    const { rows } = await db.query<{ sphere: string | null }>("select * from public.chat_report($1, $2)", [
      OWNER,
      first,
    ]);
    assert.deepEqual(rows.map((row) => row.sphere), ["VoiceFin"]);
  }));

test("сферы, которой нет, разбор чата не заводит: чат и дела без сферы", () =>
  withDatabase(async (db) => {
    const { threadId, ids } = await chat(db);
    await analyze(db, threadId, ids, "VoiceFin");
    assert.equal(await threadSphere(db, threadId), null);
    assert.deepEqual(await chatTaskSpheres(db, threadId), [null]);
    assert.deepEqual(await alive(db), []);
  }));

test("ответ на отчёт находит чат; «это по X» меняет сферу чата и всех его дел", () =>
  withDatabase(async (db) => {
    await understand(db, { spheres: { add: ["VoiceFin"] } });
    const { threadId, ids } = await chat(db);
    const analysisId = await analyze(db, threadId, ids, "VoiceFin");
    await db.query("select public.mark_chat_report_sent($1, $2, 4242)", [OWNER, analysisId]);
    const next = await more(db, "2");
    await analyze(db, threadId, next.ids, null, ["Игорь пришлёт договор"]);

    const { rows } = await db.query<{ thread_id: string; chat_with: string; sphere: string | null }>(
      "select * from public.reported_chat($1, 4242)",
      [OWNER],
    );
    assert.deepEqual(
      rows.map((row) => [row.thread_id, row.chat_with, row.sphere]),
      [[threadId, "Игорем", "VoiceFin"]],
    );
    assert.deepEqual((await db.query("select * from public.reported_chat($1, 4242)", [STRANGER])).rows, []);
    assert.deepEqual((await db.query("select * from public.reported_chat($1, 4343)", [OWNER])).rows, []);

    await understand(db, { spheres: { chat: { thread_id: threadId, sphere: "РЕЙВА" } } });
    assert.equal(await threadSphere(db, threadId), "РЕЙВА");
    assert.deepEqual(await chatTaskSpheres(db, threadId), ["РЕЙВА", "РЕЙВА"]);
  }));

test("сфера чужого чата — отказ целиком", () =>
  withDatabase(async (db) => {
    const { threadId } = await chat(db);
    await assert.rejects(
      understand(db, { owner: STRANGER, spheres: { chat: { thread_id: threadId, sphere: "РЕЙВА" } } }),
      /is not owned by/,
    );
  }));
