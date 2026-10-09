/**
 * Личные чаты (§25): четыре таблицы под правилом «owner only» и функции бота —
 * согласие по площадке, приём сообщения, правка и удаление, чаты к разбору,
 * запись разбора с задачами и «ждёт ответа», отчёт владельцу, «Убрать»,
 * напоминание о неотвеченном и срок хранения.
 *
 * Функции зовёт только бот ключом service-role; вызовы здесь идут от
 * владельца базы, права — отдельным тестом. Переписки выдуманные.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import type { PGlite } from "@electric-sql/pglite";

import { asRole, withDatabase } from "./database.ts";

const OWNER = 777;
const STRANGER = 999;
const CONNECTION = "biz-777";
const IGOR = "1001";
const OLEG = "1002";

const SIGNATURES = [
  "public.connect_chat_source(bigint, text, text, boolean)",
  "public.mark_consent_asked(bigint, text)",
  "public.answer_consent(bigint, text, boolean)",
  "public.store_chat_message(bigint, text, text, text, text, text, text, text, timestamptz, text, text, boolean, text)",
  "public.set_chat_transcript(bigint, uuid, text)",
  "public.edit_chat_message(bigint, text, text, text, text, text)",
  "public.erase_chat_messages(bigint, text, text, text, text[])",
  "public.chats_to_analyze(bigint, timestamptz, timestamptz)",
  "public.record_chat_analysis(bigint, uuid, uuid[], jsonb, text, integer, integer, integer, text, jsonb, jsonb, text)",
  "public.chat_failed(bigint, uuid)",
  "public.skip_chat_messages(bigint, uuid, uuid[])",
  "public.chat_report(bigint, uuid)",
  "public.mark_chat_report_sent(bigint, uuid, bigint)",
  "public.drop_chat_task(bigint, uuid, smallint)",
  "public.chats_waiting(bigint, timestamptz)",
  "public.mark_waiting_reminded(bigint, uuid, timestamptz)",
  "public.erase_old_chat_messages(bigint, timestamptz)",
];

interface Source {
  owner_telegram_id: string | number;
  platform: string;
  connection_id: string | null;
  is_enabled: boolean;
  asked_at: Date | null;
  consented_at: Date | null;
  declined_at: Date | null;
}

interface Stored {
  outcome: string;
  message_id: string | null;
}

interface Thread {
  id: string;
  name: string;
  username: string | null;
  tracks_waiting: boolean;
  last_out_at: Date | null;
  waiting_since: Date | null;
  waiting_about: string | null;
  waiting_to: string | null;
  waiting_reminded_at: Date | null;
  failures: number;
}

interface ChatMessage {
  id: string;
  external_id: string;
  direction: string;
  sender: string;
  kind: string;
  text: string;
  analysis_id: string | null;
  erased_at: Date | null;
}

function only<T>(rows: T[]): T {
  assert.equal(rows.length, 1, `ждали одну строку, пришло ${rows.length}`);
  return rows[0]!;
}

/** Момент со сдвигом в минутах от сейчас — ISO для параметров. */
function minutesFromNow(minutes: number): string {
  return new Date(Date.now() + minutes * 60_000).toISOString();
}

async function connect(
  db: PGlite,
  enabled = true,
  connection: string | null = CONNECTION,
  platform = "telegram",
  owner = OWNER,
): Promise<Source> {
  const { rows } = await db.query<Source>("select * from public.connect_chat_source($1, $2, $3, $4)", [
    owner,
    platform,
    connection,
    enabled,
  ]);
  return only(rows);
}

async function asked(db: PGlite, platform = "telegram", owner = OWNER): Promise<boolean> {
  const { rows } = await db.query<{ ok: boolean }>("select public.mark_consent_asked($1, $2) as ok", [
    owner,
    platform,
  ]);
  return only(rows).ok;
}

async function answer(db: PGlite, agreed: boolean, platform = "telegram", owner = OWNER): Promise<Source> {
  const { rows } = await db.query<Source>("select * from public.answer_consent($1, $2, $3)", [
    owner,
    platform,
    agreed,
  ]);
  return only(rows);
}

/** Площадка подключена, и владелец согласился. */
async function consented(db: PGlite, platform = "telegram", owner = OWNER): Promise<void> {
  await connect(db, true, platform === "telegram" ? CONNECTION : null, platform, owner);
  await asked(db, platform, owner);
  await answer(db, true, platform, owner);
}

interface Incoming {
  chat?: string;
  name?: string;
  id: string;
  direction?: "in" | "out";
  sender?: string;
  sentAt?: string;
  kind?: string;
  text?: string;
  connection?: string | null;
  platform?: string;
  owner?: number;
  tracksWaiting?: boolean;
  /** Имя пользователя собеседника; не задано — `null`: площадка его не знает. */
  username?: string | null;
}

async function store(db: PGlite, message: Incoming): Promise<Stored> {
  const { rows } = await db.query<Stored>(
    `select * from public.store_chat_message($1, $2, $3, $4, $5, $6, $7, $8, $9::timestamptz, $10, $11, $12, $13)`,
    [
      message.owner ?? OWNER,
      message.platform ?? "telegram",
      message.connection === undefined ? CONNECTION : message.connection,
      message.chat ?? IGOR,
      message.name ?? "Игорь Петров",
      message.id,
      message.direction ?? "in",
      message.sender ?? (message.direction === "out" ? "Тим" : "Игорь Петров"),
      message.sentAt ?? minutesFromNow(0),
      message.kind ?? "text",
      message.text ?? "Пришлёшь расчёт?",
      message.tracksWaiting ?? true,
      message.username ?? null,
    ],
  );
  return only(rows);
}

async function thread(db: PGlite, chat = IGOR, owner = OWNER): Promise<Thread> {
  const { rows } = await db.query<Thread>(
    "select * from public.chat_threads where owner_telegram_id = $1 and chat_key = $2",
    [owner, chat],
  );
  return only(rows);
}

async function messages(db: PGlite, threadId: string): Promise<ChatMessage[]> {
  const { rows } = await db.query<ChatMessage>(
    "select * from public.chat_messages where thread_id = $1 order by sent_at, created_at",
    [threadId],
  );
  return rows;
}

async function count(db: PGlite, table: string): Promise<number> {
  const { rows } = await db.query<{ n: number }>(`select count(*)::int as n from public.${table}`);
  return rows[0]!.n;
}

interface AnalysisInput {
  threadId: string;
  messageIds: string[];
  tasks?: unknown[];
  waiting?: unknown;
  chatWith?: string;
  owner?: number;
}

async function record(db: PGlite, input: AnalysisInput): Promise<string | null> {
  const { rows } = await db.query<{ id: string | null }>(
    `select public.record_chat_analysis($1, $2, $3::uuid[], $4::jsonb, 'claude-opus-5', 1800, 240, 9500,
                                        $5, $6::jsonb, $7::jsonb) as id`,
    [
      input.owner ?? OWNER,
      input.threadId,
      input.messageIds,
      JSON.stringify({ deals: input.tasks?.length ?? 0 }),
      input.chatWith ?? "Игорем",
      input.waiting === undefined ? null : JSON.stringify(input.waiting),
      JSON.stringify(input.tasks ?? []),
    ],
  );
  return only(rows).id;
}

function deal(item: number, title: string, reminders: unknown[] = []): unknown {
  return {
    item,
    task: {
      title,
      due_at: "2026-10-09T18:00:00+05:00",
      due_precision: "day",
      promise: item === 1 ? "mine" : "to_me",
      people: ["Игорь"],
    },
    reminders,
  };
}

/** Чат Игоря с согласием и двумя сообщениями: вопрос и ответ владельца. */
async function chatWithTwo(db: PGlite): Promise<{ threadId: string; ids: string[] }> {
  await consented(db);
  const first = await store(db, { id: "1", text: "Пришлёшь расчёт?" });
  const second = await store(db, { id: "2", direction: "out", text: "Да, в пятницу" });
  const { id: threadId } = await thread(db);
  return { threadId, ids: [first.message_id!, second.message_id!] };
}

// --- Схема и права -----------------------------------------------------------

test("у каждой функции чатов одна перегрузка, и зовёт её только service_role", async () => {
  await withDatabase(async (db) => {
    for (const signature of SIGNATURES) {
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

test("функции чатов под anon и authenticated — отказ в праве", async () => {
  await withDatabase(async (db) => {
    for (const role of ["anon", "authenticated"] as const) {
      await asRole(db, role, OWNER, async () => {
        await assert.rejects(
          db.query("select * from public.connect_chat_source($1, 'telegram', 'x', true)", [OWNER]),
          /permission denied/,
        );
        await assert.rejects(
          db.query(
            `select * from public.store_chat_message($1, 'telegram', 'x', '1', 'И', '1', 'in', 'И', now(), 'text', 'т', true)`,
            [OWNER],
          ),
          /permission denied/,
        );
        await assert.rejects(
          db.query("select public.erase_old_chat_messages($1, now())", [OWNER]),
          /permission denied/,
        );
      });
    }
    assert.equal(await count(db, "chat_sources"), 0);
  });
});

test("четыре таблицы под правилом «owner only»: владелец видит своё, anon — ничего", async () => {
  await withDatabase(async (db) => {
    await consented(db);
    await store(db, { id: "1" });
    await consented(db, "telegram", STRANGER);
    await store(db, { id: "1", owner: STRANGER });

    for (const table of ["chat_sources", "chat_threads", "chat_messages"]) {
      const seen = await asRole(db, "authenticated", OWNER, async () => {
        const { rows } = await db.query<{ owner_telegram_id: string | number }>(
          `select owner_telegram_id from public.${table}`,
        );
        return rows.map((row) => Number(row.owner_telegram_id));
      });
      assert.deepEqual(seen, [OWNER], table);
      await asRole(db, "anon", null, async () => {
        const { rows } = await db.query(`select * from public.${table}`);
        assert.deepEqual(rows, [], table);
      });
    }
    const { rows: rls } = await db.query<{ relname: string; relrowsecurity: boolean }>(
      `select relname, relrowsecurity from pg_class
        where relname in ('chat_sources', 'chat_threads', 'chat_messages', 'chat_analyses')
        order by relname`,
    );
    assert.deepEqual(
      rls.map((row) => [row.relname, row.relrowsecurity]),
      [
        ["chat_analyses", true],
        ["chat_messages", true],
        ["chat_sources", true],
        ["chat_threads", true],
      ],
    );

    // `with check`: строку на чужого владельца не вставить.
    await asRole(db, "authenticated", OWNER, async () => {
      await assert.rejects(
        db.query("insert into public.chat_sources (owner_telegram_id, platform) values ($1, 'max')", [STRANGER]),
        /row-level security/,
      );
    });
  });
});

test("площадка — только telegram, instagram или max", async () => {
  await withDatabase(async (db) => {
    await connect(db, true, null, "instagram");
    await connect(db, true, null, "max");
    await assert.rejects(connect(db, true, null, "vk"), /chat_sources_platform_check/);
    assert.equal(await count(db, "chat_sources"), 2);
  });
});

// --- Согласие ----------------------------------------------------------------

test("подключение заводит площадку без согласия; повтор обновляет id и включённость", async () => {
  await withDatabase(async (db) => {
    const first = await connect(db);
    assert.equal(first.platform, "telegram");
    assert.equal(first.connection_id, CONNECTION);
    assert.equal(first.is_enabled, true);
    assert.equal(first.asked_at, null);
    assert.equal(first.consented_at, null);

    const off = await connect(db, false, "biz-new");
    assert.equal(off.connection_id, "biz-new");
    assert.equal(off.is_enabled, false);
    assert.equal(await count(db, "chat_sources"), 1);
  });
});

test("вопрос о согласии помечается один раз; ответ «Согласен» и «Не надо» меняют решение", async () => {
  await withDatabase(async (db) => {
    await connect(db);
    assert.equal(await asked(db), true);
    assert.equal(await asked(db), false, "второй раз не помечается");

    const yes = await answer(db, true);
    assert.ok(yes.consented_at !== null);
    assert.equal(yes.declined_at, null);

    const no = await answer(db, false);
    assert.equal(no.consented_at, null);
    assert.ok(no.declined_at !== null);

    const again = await answer(db, true);
    assert.ok(again.consented_at !== null, "передумал — согласие снова есть");
    assert.equal(again.declined_at, null);
  });
});

test("ответ без площадки — пустая строка; чужой владелец площадку не трогает", async () => {
  await withDatabase(async (db) => {
    await connect(db);
    const none = await answer(db, true, "telegram", STRANGER);
    assert.equal(none.platform, null);
    const { rows } = await db.query<Source>("select * from public.chat_sources");
    assert.equal(only(rows).consented_at, null);
  });
});

test("после «Не надо» отключение и новое подключение спрашивают снова; согласие не переспрашивается", async () => {
  await withDatabase(async (db) => {
    await connect(db);
    await asked(db);
    await answer(db, false);

    const same = await connect(db);
    assert.ok(same.declined_at !== null, "правка подключения без отключения — отказ остаётся");

    await connect(db, false);
    const back = await connect(db, true, "biz-2");
    assert.equal(back.declined_at, null);
    assert.equal(back.asked_at, null, "вопрос уйдёт снова");

    await asked(db);
    await answer(db, true);
    await connect(db, false);
    const kept = await connect(db, true, "biz-3");
    assert.ok(kept.consented_at !== null, "согласие не сбрасывается");
    assert.ok(kept.asked_at !== null);
  });
});

// --- Приём -------------------------------------------------------------------

test("до согласия сообщение не хранится: нет площадки, чужое подключение, выключено, нет согласия", async () => {
  await withDatabase(async (db) => {
    assert.deepEqual(await store(db, { id: "1" }), { outcome: "no_source", message_id: null });

    await connect(db);
    assert.equal((await store(db, { id: "1", connection: "biz-foreign" })).outcome, "unknown_connection");
    assert.equal((await store(db, { id: "1" })).outcome, "no_consent");
    await asked(db);
    await answer(db, false);
    assert.equal((await store(db, { id: "1" })).outcome, "no_consent", "«Не надо» — не хранится и дальше");

    await answer(db, true);
    await connect(db, false);
    assert.equal((await store(db, { id: "1" })).outcome, "disabled");

    assert.equal(await count(db, "chat_threads"), 0);
    assert.equal(await count(db, "chat_messages"), 0);
  });
});

test("сообщение с согласием хранится, чат заводится; повторная доставка дубля не даёт", async () => {
  await withDatabase(async (db) => {
    await consented(db);
    const sentAt = minutesFromNow(-1);
    const stored = await store(db, { id: "42", sentAt, text: "Пришлёшь расчёт в пятницу?" });
    assert.equal(stored.outcome, "stored");
    assert.ok(stored.message_id);

    const chat = await thread(db);
    assert.equal(chat.name, "Игорь Петров");
    assert.equal(chat.tracks_waiting, true);
    const [message] = await messages(db, chat.id);
    assert.equal(message?.external_id, "42");
    assert.equal(message?.direction, "in");
    assert.equal(message?.sender, "Игорь Петров");
    assert.equal(message?.text, "Пришлёшь расчёт в пятницу?");
    assert.equal(message?.analysis_id, null);

    const again = await store(db, { id: "42", text: "другой текст" });
    assert.deepEqual(again, { outcome: "repeat", message_id: stored.message_id });
    assert.equal((await messages(db, chat.id))[0]?.text, "Пришлёшь расчёт в пятницу?");
  });
});

test("площадка без id подключения (MAX) хранит без него; признак «не вести ждёт ответа» ставится при заведении", async () => {
  await withDatabase(async (db) => {
    await consented(db, "max");
    const stored = await store(db, { id: "m1", platform: "max", connection: null, chat: "notes", tracksWaiting: false });
    assert.equal(stored.outcome, "stored");
    const chat = await thread(db, "notes");
    assert.equal(chat.tracks_waiting, false);
  });
});

test("виды и направление — по списку", async () => {
  await withDatabase(async (db) => {
    await consented(db);
    for (const kind of ["voice", "video_note", "photo", "other"]) {
      assert.equal((await store(db, { id: kind, kind, text: "" })).outcome, "stored");
    }
    await assert.rejects(store(db, { id: "x", kind: "sticker" }), /chat_messages_kind_check/);
    await assert.rejects(
      store(db, { id: "y", direction: "sideways" as "in" }),
      /chat_messages_direction_check/,
    );
  });
});

test("расшифровка голосового пишется до разбора и не пишется после", async () => {
  await withDatabase(async (db) => {
    await consented(db);
    const voice = await store(db, { id: "v", kind: "voice", text: "" });
    const set = (text: string) =>
      db
        .query<{ ok: boolean }>("select public.set_chat_transcript($1, $2, $3) as ok", [OWNER, voice.message_id, text])
        .then(({ rows }) => only(rows).ok);
    assert.equal(await set("Перезвони после обеда"), true);
    const chat = await thread(db);
    assert.equal((await messages(db, chat.id))[0]?.text, "Перезвони после обеда");

    await record(db, { threadId: chat.id, messageIds: [voice.message_id!] });
    assert.equal(await set("поздно"), false);
    const { rows } = await db.query<{ ok: boolean }>("select public.set_chat_transcript($1, $2, 'чужой') as ok", [
      STRANGER,
      voice.message_id,
    ]);
    assert.equal(only(rows).ok, false);
  });
});

test("правка до разбора меняет текст, после — ничего; чужое подключение не правит", async () => {
  await withDatabase(async (db) => {
    const { threadId, ids } = await chatWithTwo(db);
    const edit = (text: string, connection = CONNECTION) =>
      db
        .query<{ ok: boolean }>("select public.edit_chat_message($1, 'telegram', $2, $3, '1', $4) as ok", [
          OWNER,
          connection,
          IGOR,
          text,
        ])
        .then(({ rows }) => only(rows).ok);

    assert.equal(await edit("Пришлёшь расчёт в четверг?", "biz-foreign"), false);
    assert.equal(await edit("Пришлёшь расчёт в четверг?"), true);
    assert.equal((await messages(db, threadId))[0]?.text, "Пришлёшь расчёт в четверг?");

    await record(db, { threadId, messageIds: ids });
    assert.equal(await edit("после разбора"), false);
    assert.equal((await messages(db, threadId))[0]?.text, "Пришлёшь расчёт в четверг?");
  });
});

test("удаление стирает текст — и до разбора, и после; чужое подключение не стирает", async () => {
  await withDatabase(async (db) => {
    const { threadId, ids } = await chatWithTwo(db);
    const erase = (externalIds: string[], connection = CONNECTION) =>
      db
        .query<{ n: number }>("select public.erase_chat_messages($1, 'telegram', $2, $3, $4::text[]) as n", [
          OWNER,
          connection,
          IGOR,
          externalIds,
        ])
        .then(({ rows }) => only(rows).n);

    assert.equal(await erase(["1"], "biz-foreign"), 0);
    assert.equal(await erase(["1", "404"]), 1);
    const [first] = await messages(db, threadId);
    assert.equal(first?.text, "");
    assert.ok(first?.erased_at !== null);
    assert.equal(await erase(["1"]), 0, "уже стёрто");

    await record(db, { threadId, messageIds: ids });
    assert.equal(await erase(["2"]), 1);
    assert.equal((await messages(db, threadId))[1]?.text, "");
  });
});

// --- Чаты к разбору ------------------------------------------------------------

test("к разбору — чаты с неразобранными, где тишина 20 минут или первое старше двух часов", async () => {
  await withDatabase(async (db) => {
    await consented(db);
    await store(db, { id: "1", chat: IGOR });
    await store(db, { id: "1", chat: OLEG, name: "Олег" });
    const due = (quietBefore: string, staleBefore: string) =>
      db
        .query<{ thread_id: string; chat_key: string; name: string; platform: string }>(
          "select * from public.chats_to_analyze($1, $2::timestamptz, $3::timestamptz)",
          [OWNER, quietBefore, staleBefore],
        )
        .then(({ rows }) => rows.map((row) => row.chat_key));

    assert.deepEqual(await due(minutesFromNow(-20), minutesFromNow(-120)), [], "разговор идёт");
    // Олег затих давно: последнее сообщение пришло 30 минут назад.
    await db.query(
      "update public.chat_threads set last_message_at = now() - interval '30 minutes' where chat_key = $1",
      [OLEG],
    );
    assert.deepEqual(await due(minutesFromNow(-20), minutesFromNow(-120)), [OLEG]);
    // У Игоря разговор не затихает, но первое неразобранное пришло 3 часа назад.
    await db.query(
      `update public.chat_messages set created_at = now() - interval '3 hours'
        where thread_id = (select id from public.chat_threads where chat_key = $1)`,
      [IGOR],
    );
    assert.deepEqual(await due(minutesFromNow(-20), minutesFromNow(-120)), [IGOR, OLEG], "старшее первым");
  });
});

test("разобранный чат, чужой и чат после отзыва согласия к разбору не идут", async () => {
  await withDatabase(async (db) => {
    const { threadId, ids } = await chatWithTwo(db);
    await consented(db, "telegram", STRANGER);
    await store(db, { id: "1", owner: STRANGER, chat: OLEG });
    await db.query("update public.chat_threads set last_message_at = now() - interval '1 hour'");
    const due = () =>
      db
        .query<{ thread_id: string }>("select * from public.chats_to_analyze($1, now() - interval '20 minutes', now() - interval '2 hours')", [OWNER])
        .then(({ rows }) => rows.map((row) => row.thread_id));

    assert.deepEqual(await due(), [threadId]);
    await answer(db, false);
    assert.deepEqual(await due(), [], "«Не надо» — хранимое больше не разбирается");
    await answer(db, true);
    await record(db, { threadId, messageIds: ids });
    assert.deepEqual(await due(), []);
  });
});

// --- Запись разбора ----------------------------------------------------------

test("разбор одной транзакцией: строка разбора, пометка, задачи с напоминаниями и номерами", async () => {
  await withDatabase(async (db) => {
    const { threadId, ids } = await chatWithTwo(db);
    const analysisId = await record(db, {
      threadId,
      messageIds: ids,
      tasks: [
        deal(1, "прислать Игорю расчёт", [
          { stage: "before", fire_at: "2026-10-09T09:00:00+05:00" },
          { stage: "due", fire_at: "2026-10-09T18:00:00+05:00" },
        ]),
        deal(2, "Игорь пришлёт договор"),
      ],
    });
    assert.ok(analysisId);

    const { rows: analyses } = await db.query<{
      status: string;
      messages_count: number;
      items: number;
      chat_with: string;
      ai_model: string;
      input_tokens: number;
      output_tokens: number;
      duration_ms: number;
      reported_at: Date | null;
    }>("select * from public.chat_analyses where id = $1", [analysisId]);
    const analysis = only(analyses);
    assert.equal(analysis.status, "done");
    assert.equal(analysis.messages_count, 2);
    assert.equal(analysis.items, 2);
    assert.equal(analysis.chat_with, "Игорем");
    assert.equal(analysis.ai_model, "claude-opus-5");
    assert.deepEqual([analysis.input_tokens, analysis.output_tokens, analysis.duration_ms], [1800, 240, 9500]);
    assert.equal(analysis.reported_at, null);

    assert.ok((await messages(db, threadId)).every((message) => message.analysis_id === analysisId));

    const { rows: tasks } = await db.query<{
      id: string;
      title: string;
      kind: string;
      status: string;
      promise: string;
      people: string[];
      chat_item: number;
      source_message_id: string | null;
    }>("select * from public.tasks where chat_analysis_id = $1 order by chat_item", [analysisId]);
    assert.deepEqual(
      tasks.map((task) => [task.chat_item, task.title, task.kind, task.status, task.promise]),
      [
        [1, "прислать Игорю расчёт", "task", "active", "mine"],
        [2, "Игорь пришлёт договор", "task", "active", "to_me"],
      ],
    );
    assert.deepEqual(tasks[0]?.people, ["Игорь"]);
    assert.equal(tasks[0]?.source_message_id, null);
    const { rows: reminders } = await db.query<{ stage: string }>(
      "select stage from public.reminders where task_id = $1 order by fire_at",
      [tasks[0]?.id],
    );
    assert.deepEqual(reminders.map((row) => row.stage), ["before", "due"]);
  });
});

test("повтор разбора тех же сообщений ничего не пишет; чужой чат — отказ", async () => {
  await withDatabase(async (db) => {
    const { threadId, ids } = await chatWithTwo(db);
    assert.ok(await record(db, { threadId, messageIds: ids, tasks: [deal(1, "прислать расчёт")] }));
    assert.equal(await record(db, { threadId, messageIds: ids, tasks: [deal(1, "прислать расчёт")] }), null);
    assert.equal(await count(db, "chat_analyses"), 1);
    assert.equal(await count(db, "tasks"), 1);

    await assert.rejects(record(db, { threadId, messageIds: ids, owner: STRANGER }), /not owned/);
  });
});

test("больше пяти дел и номер вне 1–5 — отказ целиком", async () => {
  await withDatabase(async (db) => {
    const { threadId, ids } = await chatWithTwo(db);
    const six = [1, 2, 3, 4, 5, 6].map((item) => deal(item, `дело ${item}`));
    await assert.rejects(record(db, { threadId, messageIds: ids, tasks: six }), /at most five/);
    await assert.rejects(record(db, { threadId, messageIds: ids, tasks: [deal(7, "дело")] }), /tasks_chat_item_check/);
    assert.equal(await count(db, "chat_analyses"), 0);
    assert.ok((await messages(db, threadId)).every((message) => message.analysis_id === null));
  });
});

test("у задачи не бывает и сообщения, и разбора чата", async () => {
  await withDatabase(async (db) => {
    const { threadId, ids } = await chatWithTwo(db);
    const analysisId = await record(db, { threadId, messageIds: ids, tasks: [deal(1, "прислать расчёт")] });
    const { rows } = await db.query<{ id: string }>(
      `insert into public.messages (owner_telegram_id, chat_id, telegram_message_id, text)
       values ($1, $1, 1, 'т') returning id`,
      [OWNER],
    );
    await assert.rejects(
      db.query("update public.tasks set source_message_id = $1, source_item = 1 where chat_analysis_id = $2", [
        only(rows).id,
        analysisId,
      ]),
      /tasks_chat_source_check/,
    );
  });
});

const IGOR_ASKED = { about: "он спрашивал, во сколько созвон", to: "Игорю" };

test("«ждёт ответа» пишется с временем вопроса; владелец писал после — не пишется", async () => {
  await withDatabase(async (db) => {
    await consented(db);
    const question = await store(db, { id: "1", sentAt: "2026-10-07T10:00:00Z", text: "Во сколько созвон?" });
    const chat = await thread(db);
    await record(db, {
      threadId: chat.id,
      messageIds: [question.message_id!],
      waiting: { ...IGOR_ASKED, since: "2026-10-07T10:00:00Z" },
    });
    let state = await thread(db);
    assert.equal(state.waiting_since?.toISOString(), "2026-10-07T10:00:00.000Z");
    assert.equal(state.waiting_about, IGOR_ASKED.about);
    assert.equal(state.waiting_to, "Игорю");

    // Владелец ответил — «ждёт ответа» снимается сообщением.
    await store(db, { id: "2", direction: "out", sentAt: "2026-10-07T10:05:00Z", text: "В 15" });
    state = await thread(db);
    assert.equal(state.waiting_since, null);
    assert.equal(state.waiting_about, null);
    assert.equal(state.last_out_at?.toISOString(), "2026-10-07T10:05:00.000Z");

    // Разбор, который пришёл позже ответа о более раннем вопросе, «ждёт» не ставит.
    const older = await store(db, { id: "3", sentAt: "2026-10-07T10:03:00Z", text: "Алло?" });
    await record(db, {
      threadId: chat.id,
      messageIds: [older.message_id!],
      waiting: { ...IGOR_ASKED, since: "2026-10-07T10:03:00Z" },
    });
    assert.equal((await thread(db)).waiting_since, null);
  });
});

test("ждущий и не напомненный чат держит прежнее время; напомненный получает новое", async () => {
  await withDatabase(async (db) => {
    await consented(db);
    const first = await store(db, { id: "1", sentAt: "2026-10-07T10:00:00Z" });
    const chat = await thread(db);
    await record(db, { threadId: chat.id, messageIds: [first.message_id!], waiting: { ...IGOR_ASKED, since: "2026-10-07T10:00:00Z" } });

    const second = await store(db, { id: "2", sentAt: "2026-10-07T10:30:00Z" });
    await record(db, {
      threadId: chat.id,
      messageIds: [second.message_id!],
      waiting: { about: "он просил прислать фото", to: "Игорю", since: "2026-10-07T10:30:00Z" },
    });
    let state = await thread(db);
    assert.equal(state.waiting_since?.toISOString(), "2026-10-07T10:00:00.000Z");
    assert.equal(state.waiting_about, "он просил прислать фото");

    const reminded = await db.query<{ ok: boolean }>("select public.mark_waiting_reminded($1, $2, $3::timestamptz) as ok", [
      OWNER,
      chat.id,
      "2026-10-07T10:00:00Z",
    ]);
    assert.equal(only(reminded.rows).ok, true);

    const third = await store(db, { id: "3", sentAt: "2026-10-07T13:30:00Z" });
    await record(db, { threadId: chat.id, messageIds: [third.message_id!], waiting: { ...IGOR_ASKED, since: "2026-10-07T13:30:00Z" } });
    state = await thread(db);
    assert.equal(state.waiting_since?.toISOString(), "2026-10-07T13:30:00.000Z");
    assert.equal(state.waiting_reminded_at, null);

    // Разбор без «ждёт ответа» прежнее не снимает.
    const fourth = await store(db, { id: "4", sentAt: "2026-10-07T13:40:00Z" });
    await record(db, { threadId: chat.id, messageIds: [fourth.message_id!] });
    assert.equal((await thread(db)).waiting_about, IGOR_ASKED.about);
  });
});

test("чат с признаком «не вести ждёт ответа» его не получает", async () => {
  await withDatabase(async (db) => {
    await consented(db, "max");
    const stored = await store(db, { id: "1", platform: "max", connection: null, chat: "notes", tracksWaiting: false });
    const chat = await thread(db, "notes");
    await record(db, { threadId: chat.id, messageIds: [stored.message_id!], waiting: { ...IGOR_ASKED, since: minutesFromNow(-200) } });
    assert.equal((await thread(db, "notes")).waiting_since, null);
  });
});

// --- Неудачи -----------------------------------------------------------------

test("неудачи считаются подряд; пропуск помечает разобранным без дел и обнуляет счёт", async () => {
  await withDatabase(async (db) => {
    const { threadId, ids } = await chatWithTwo(db);
    const fail = () =>
      db
        .query<{ n: number | null }>("select public.chat_failed($1, $2) as n", [OWNER, threadId])
        .then(({ rows }) => only(rows).n);
    assert.equal(await fail(), 1);
    assert.equal(await fail(), 2);
    assert.equal(await fail(), 3);

    const { rows } = await db.query<{ id: string | null }>("select public.skip_chat_messages($1, $2, $3::uuid[]) as id", [
      OWNER,
      threadId,
      ids,
    ]);
    const skipped = only(rows).id;
    assert.ok(skipped);
    const { rows: analyses } = await db.query<{ status: string; items: number; messages_count: number }>(
      "select status, items, messages_count from public.chat_analyses",
    );
    assert.deepEqual(only(analyses), { status: "skipped", items: 0, messages_count: 2 });
    assert.ok((await messages(db, threadId)).every((message) => message.analysis_id === skipped));
    assert.equal((await thread(db)).failures, 0);

    const { rows: foreign } = await db.query<{ n: number | null }>("select public.chat_failed($1, $2) as n", [
      STRANGER,
      threadId,
    ]);
    assert.equal(only(foreign).n, null);
  });
});

test("удачный разбор обнуляет счёт неудач", async () => {
  await withDatabase(async (db) => {
    const { threadId, ids } = await chatWithTwo(db);
    await db.query("select public.chat_failed($1, $2)", [OWNER, threadId]);
    await record(db, { threadId, messageIds: ids });
    assert.equal((await thread(db)).failures, 0);
  });
});

// --- Отчёт и «Убрать» --------------------------------------------------------

interface ReportRow {
  platform: string;
  chat_key: string;
  chat_name: string;
  username: string | null;
  chat_with: string;
  item: number;
  task_id: string;
  title: string;
  promise: string;
  status: string;
}

async function report(db: PGlite, analysisId: string, owner = OWNER): Promise<ReportRow[]> {
  const { rows } = await db.query<ReportRow>("select * from public.chat_report($1, $2)", [owner, analysisId]);
  return rows;
}

test("отчёт — дела разбора по номерам с площадкой и именем; чужому — ничего", async () => {
  await withDatabase(async (db) => {
    const { threadId, ids } = await chatWithTwo(db);
    const analysisId = (await record(db, {
      threadId,
      messageIds: ids,
      tasks: [deal(1, "прислать Игорю расчёт"), deal(2, "Игорь пришлёт договор")],
    }))!;
    const rows = await report(db, analysisId);
    assert.deepEqual(
      rows.map((row) => [row.platform, row.chat_name, row.chat_with, row.item, row.title, row.promise, row.status]),
      [
        ["telegram", "Игорь Петров", "Игорем", 1, "прислать Игорю расчёт", "mine", "active"],
        ["telegram", "Игорь Петров", "Игорем", 2, "Игорь пришлёт договор", "to_me", "active"],
      ],
    );
    assert.deepEqual(await report(db, analysisId, STRANGER), []);
  });
});

test("отчёт помечается отправленным один раз", async () => {
  await withDatabase(async (db) => {
    const { threadId, ids } = await chatWithTwo(db);
    const analysisId = await record(db, { threadId, messageIds: ids, tasks: [deal(1, "прислать расчёт")] });
    const mark = (messageId: number | null, owner = OWNER) =>
      db
        .query<{ ok: boolean }>("select public.mark_chat_report_sent($1, $2, $3) as ok", [owner, analysisId, messageId])
        .then(({ rows }) => only(rows).ok);
    assert.equal(await mark(5151, STRANGER), false);
    assert.equal(await mark(5151), true);
    assert.equal(await mark(6262), false);
    const { rows } = await db.query<{ report_message_id: string | number; reported_at: Date | null }>(
      "select report_message_id, reported_at from public.chat_analyses where id = $1",
      [analysisId],
    );
    assert.equal(Number(only(rows).report_message_id), 5151);
    assert.ok(only(rows).reported_at !== null);
  });
});

test("«Убрать» убирает задачу с неотправленными напоминаниями; второй раз и чужому — без правки", async () => {
  await withDatabase(async (db) => {
    const { threadId, ids } = await chatWithTwo(db);
    const analysisId = await record(db, {
      threadId,
      messageIds: ids,
      tasks: [
        deal(1, "прислать расчёт", [
          { stage: "before", fire_at: "2026-10-09T09:00:00+05:00" },
          { stage: "due", fire_at: "2026-10-09T18:00:00+05:00" },
        ]),
        deal(2, "Игорь пришлёт договор"),
      ],
    });
    await db.query("update public.reminders set sent_at = now() where stage = 'before'");
    const drop = (item: number, owner = OWNER) =>
      db
        .query<{ id: string | null; status: string | null }>("select * from public.drop_chat_task($1, $2, $3::smallint)", [
          owner,
          analysisId,
          item,
        ])
        .then(({ rows }) => only(rows));

    assert.equal((await drop(1, STRANGER)).id, null);
    const dropped = await drop(1);
    assert.equal(dropped.status, "cancelled");
    const { rows: left } = await db.query<{ stage: string }>("select stage from public.reminders where task_id = $1", [
      dropped.id,
    ]);
    assert.deepEqual(left.map((row) => row.stage), ["before"], "ушедшее остаётся следом, ждущее стёрто");
    assert.equal((await drop(1)).status, "cancelled", "второй раз — та же задача без правки");
    assert.equal((await drop(9)).id, null);
    assert.deepEqual(
      (await report(db, analysisId!)).map((row) => row.status),
      ["cancelled", "active"],
    );
  });
});

// --- Ждёт ответа -------------------------------------------------------------

test("напомнить — чаты, где вопрос старше трёх часов, владелец не писал и не напомнено", async () => {
  await withDatabase(async (db) => {
    await consented(db);
    const igor = await store(db, { id: "1", sentAt: minutesFromNow(-200) });
    const oleg = await store(db, { id: "1", chat: OLEG, name: "Олег", sentAt: minutesFromNow(-60) });
    const igorChat = await thread(db);
    const olegChat = await thread(db, OLEG);
    await record(db, { threadId: igorChat.id, messageIds: [igor.message_id!], waiting: { ...IGOR_ASKED, since: minutesFromNow(-200) } });
    await record(db, {
      threadId: olegChat.id,
      messageIds: [oleg.message_id!],
      waiting: { about: "он просил вернуть книгу", to: "Олегу", since: minutesFromNow(-60) },
    });
    const waiting = () =>
      db
        .query<{ thread_id: string; platform: string; name: string; waiting_about: string; waiting_to: string }>(
          "select * from public.chats_waiting($1, now() - interval '3 hours')",
          [OWNER],
        )
        .then(({ rows }) => rows);

    const due = await waiting();
    assert.deepEqual(
      due.map((row) => [row.thread_id, row.platform, row.name, row.waiting_about, row.waiting_to]),
      [[igorChat.id, "telegram", "Игорь Петров", IGOR_ASKED.about, "Игорю"]],
    );
    const since = (await thread(db)).waiting_since!.toISOString();
    const { rows: marked } = await db.query<{ ok: boolean }>(
      "select public.mark_waiting_reminded($1, $2, $3::timestamptz) as ok",
      [OWNER, igorChat.id, since],
    );
    assert.equal(only(marked).ok, true);
    assert.deepEqual(await waiting(), [], "второй раз не напоминается");
    const { rows: again } = await db.query<{ ok: boolean }>(
      "select public.mark_waiting_reminded($1, $2, $3::timestamptz) as ok",
      [OWNER, igorChat.id, since],
    );
    assert.equal(only(again).ok, false);
  });
});

test("напоминание не берётся, если вопрос сменился", async () => {
  await withDatabase(async (db) => {
    await consented(db);
    const first = await store(db, { id: "1", sentAt: minutesFromNow(-200) });
    const chat = await thread(db);
    await record(db, { threadId: chat.id, messageIds: [first.message_id!], waiting: { ...IGOR_ASKED, since: minutesFromNow(-200) } });
    const { rows } = await db.query<{ ok: boolean }>("select public.mark_waiting_reminded($1, $2, now()) as ok", [
      OWNER,
      chat.id,
    ]);
    assert.equal(only(rows).ok, false);
  });
});

// --- Имя пользователя: кнопка «Открыть чат» (этап 030) ------------------------

test("имя пользователя ложится в чат без «@» и меняется со следующим сообщением", async () => {
  await withDatabase(async (db) => {
    await consented(db);
    await store(db, { id: "1", username: "@igor_p" });
    assert.equal((await thread(db)).username, "igor_p");

    await store(db, { id: "2", direction: "out", username: "igor.petrov" });
    assert.equal((await thread(db)).username, "igor.petrov", "сменил имя — в чате новое");
  });
});

test("площадка без имени его не стирает; пустое имя — имени больше нет", async () => {
  await withDatabase(async (db) => {
    await consented(db);
    await store(db, { id: "1", username: "igor_p" });

    // MAX и Partner Assistant имени не знают: `null` — как было.
    await store(db, { id: "2" });
    assert.equal((await thread(db)).username, "igor_p");
    // Старый вызов без имени (так зовёт `relay_chat_event`) — тот же путь.
    await db.query(
      `select * from public.store_chat_message($1, 'telegram', $2, $3, 'Игорь Петров', '3', 'in', 'Игорь Петров',
                                               now(), 'text', 'Ну что?', true)`,
      [OWNER, CONNECTION, IGOR],
    );
    assert.equal((await thread(db)).username, "igor_p");

    await store(db, { id: "4", username: "" });
    assert.equal((await thread(db)).username, null, "убрал имя в Telegram — ссылки по нему нет");
  });
});

test("имя, негодное для ссылки, не хранится, а сообщение сохраняется", async () => {
  await withDatabase(async (db) => {
    await consented(db);
    const stored = await store(db, { id: "1", username: "igor/../x?y=1" });
    assert.equal(stored.outcome, "stored");
    assert.equal((await thread(db)).username, null);
    await assert.rejects(
      db.query("update public.chat_threads set username = 'igor p'"),
      /chat_threads_username_check/,
    );
  });
});

test("отчёт и «ждёт ответа» отдают ключ чата и имя пользователя — для «Открыть чат»", async () => {
  await withDatabase(async (db) => {
    await consented(db);
    const question = await store(db, { id: "1", sentAt: minutesFromNow(-200), username: "igor_p" });
    const chat = await thread(db);
    const analysisId = (await record(db, {
      threadId: chat.id,
      messageIds: [question.message_id!],
      tasks: [deal(1, "прислать Игорю расчёт")],
      waiting: { ...IGOR_ASKED, since: minutesFromNow(-200) },
    }))!;

    const [line] = await report(db, analysisId);
    assert.deepEqual([line?.chat_key, line?.username], [IGOR, "igor_p"]);

    // Имя сменилось после разбора — отчёт после «Убрать» берёт новое.
    await store(db, { id: "2", username: "igor_new", sentAt: minutesFromNow(-190) });
    const [again] = await report(db, analysisId);
    assert.equal(again?.username, "igor_new");

    const { rows } = await db.query<{ chat_key: string; username: string | null }>(
      "select * from public.chats_waiting($1, now() - interval '3 hours')",
      [OWNER],
    );
    assert.deepEqual(
      rows.map((row) => [row.chat_key, row.username]),
      [[IGOR, "igor_new"]],
    );
  });
});

// --- Срок хранения -----------------------------------------------------------

test("текст сообщений старше семи дней стирается, свежие и стёртые не трогаются", async () => {
  await withDatabase(async (db) => {
    const { threadId } = await chatWithTwo(db);
    await db.query("update public.chat_messages set created_at = now() - interval '8 days' where external_id = '1'");
    const erase = (owner = OWNER) =>
      db
        .query<{ n: number }>("select public.erase_old_chat_messages($1, now() - interval '7 days') as n", [owner])
        .then(({ rows }) => only(rows).n);
    assert.equal(await erase(STRANGER), 0);
    assert.equal(await erase(), 1);
    const [old, fresh] = await messages(db, threadId);
    assert.equal(old?.text, "");
    assert.ok(old?.erased_at !== null);
    assert.equal(fresh?.text, "Да, в пятницу");
    assert.equal(await erase(), 0);
  });
});
