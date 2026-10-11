/**
 * Разобрать позже (§33): отложенные сообщения владельца в `messages` и
 * временные неудачи разбора переписки в `chat_threads`.
 *
 * Функции зовёт только бот ключом service-role; вызовы здесь идут от
 * владельца базы, права — отдельным тестом. `retry_started_at` ставит база
 * (`now()`), поэтому «прошло больше десяти минут» тест задаёт границей
 * `stale_before` в будущем, а не ожиданием. Имена и переписки выдуманные.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import type { PGlite } from "@electric-sql/pglite";

import { asRole, withDatabase } from "./database.ts";

const OWNER = 777;
const STRANGER = 999;

const SIGNATURES = [
  "public.defer_message(bigint, uuid, timestamptz, smallint, jsonb, text, numeric)",
  "public.deferred_waiting(bigint, uuid)",
  "public.take_deferred(bigint, timestamptz, timestamptz)",
  "public.release_deferred(bigint, uuid, timestamptz)",
  "public.finish_deferred(bigint, uuid)",
  "public.chat_unavailable(bigint, uuid, timestamptz)",
  "public.chats_to_analyze(bigint, timestamptz, timestamptz)",
];

interface Deferred {
  id: string;
  text: string;
  reply: string | null;
  transcript_confidence: string | number | null;
  received_at: Date;
  retry_at: Date | null;
  retry_attempts: number;
  retry_started_at: Date | null;
  retry_input: Record<string, unknown> | null;
}

let telegramMessageId = 100;

function only<T>(rows: T[]): T {
  assert.equal(rows.length, 1, `ждали одну строку, пришло ${rows.length}`);
  return rows[0]!;
}

/** Сейчас со сдвигом в минутах — ISO для параметров. */
function minutesFromNow(minutes: number): string {
  return new Date(Date.now() + minutes * 60_000).toISOString();
}

interface MessageInput {
  text?: string;
  reply?: string | null;
  /** Сколько минут назад пришло. */
  ago?: number;
  owner?: number;
  kind?: string;
}

/** Сообщение владельца, как его оставил `record_message`. */
async function message(db: PGlite, input: MessageInput = {}): Promise<string> {
  telegramMessageId += 1;
  const owner = input.owner ?? OWNER;
  const { rows } = await db.query<{ id: string }>(
    `insert into public.messages (owner_telegram_id, chat_id, telegram_message_id, text, kind, reply, received_at)
     values ($1, $1, $2, $3, $4, $5, now() - make_interval(mins => $6))
     returning id`,
    [owner, telegramMessageId, input.text ?? "завтра позвонить Игорю", input.kind ?? "text", input.reply ?? null, input.ago ?? 0],
  );
  return only(rows).id;
}

interface DeferInput {
  retryAt?: string;
  attempts?: number;
  input?: unknown;
  transcript?: string | null;
  confidence?: number | null;
  owner?: number;
}

async function defer(db: PGlite, messageId: string, input: DeferInput = {}): Promise<boolean> {
  const { rows } = await db.query<{ ok: boolean }>(
    "select public.defer_message($1, $2, $3::timestamptz, $4::smallint, $5::jsonb, $6, $7) as ok",
    [
      input.owner ?? OWNER,
      messageId,
      input.retryAt ?? minutesFromNow(1),
      input.attempts ?? 1,
      input.input === undefined ? null : JSON.stringify(input.input),
      input.transcript ?? null,
      input.confidence ?? null,
    ],
  );
  return only(rows).ok;
}

async function row(db: PGlite, messageId: string): Promise<Deferred> {
  const { rows } = await db.query<Deferred>("select * from public.messages where id = $1", [messageId]);
  return only(rows);
}

async function waiting(db: PGlite, messageId: string, owner = OWNER): Promise<boolean> {
  const { rows } = await db.query<{ ok: boolean }>("select public.deferred_waiting($1, $2) as ok", [owner, messageId]);
  return only(rows).ok;
}

async function take(
  db: PGlite,
  dueBefore = minutesFromNow(0),
  staleBefore = minutesFromNow(-10),
  owner = OWNER,
): Promise<Deferred[]> {
  const { rows } = await db.query<Deferred>(
    "select * from public.take_deferred($1, $2::timestamptz, $3::timestamptz)",
    [owner, dueBefore, staleBefore],
  );
  return rows;
}

async function release(db: PGlite, messageId: string, retryAt: string, owner = OWNER): Promise<boolean> {
  const { rows } = await db.query<{ ok: boolean }>(
    "select public.release_deferred($1, $2, $3::timestamptz) as ok",
    [owner, messageId, retryAt],
  );
  return only(rows).ok;
}

async function finish(db: PGlite, messageId: string, owner = OWNER): Promise<boolean> {
  const { rows } = await db.query<{ ok: boolean }>("select public.finish_deferred($1, $2) as ok", [owner, messageId]);
  return only(rows).ok;
}

/** Разбор прежним путём (§3.4): одна задача и ответ. */
async function understand(db: PGlite, messageId: string, reply = "✅ Записал: позвонить Игорю"): Promise<string[]> {
  const { rows } = await db.query<{ id: string }>(
    `select id from public.record_understanding(
       message_id => $1, owner_telegram_id => $2, analysis => '{"kind": "task"}'::jsonb,
       ai_model => 'claude-opus-5', ai_input_tokens => 300, ai_output_tokens => 120,
       reply => $3,
       tasks => '[{"item": 1, "task": {"title": "позвонить Игорю", "kind": "task"}, "reminders": []}]'::jsonb,
       facts => '[]'::jsonb
     )`,
    [messageId, OWNER, reply],
  );
  return rows.map((each) => each.id);
}

// --- Схема и права -------------------------------------------------------------

test("у каждой функции одна перегрузка, и зовёт её только service_role", async () => {
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
        Object.fromEntries(rights.rows.map((each) => [each.role, each.allowed])),
        { anon: false, authenticated: false, service_role: true },
        signature,
      );
    }
  });
});

test("функции отложенных сообщений под anon и authenticated — отказ в праве, ничего не записано", async () => {
  await withDatabase(async (db) => {
    const id = await message(db);
    for (const role of ["anon", "authenticated"] as const) {
      await asRole(db, role, OWNER, async () => {
        await assert.rejects(
          db.query("select public.defer_message($1, $2, now(), 1::smallint)", [OWNER, id]),
          /permission denied/,
        );
        await assert.rejects(
          db.query("select * from public.take_deferred($1, now(), now())", [OWNER]),
          /permission denied/,
        );
      });
    }
    assert.equal((await row(db, id)).retry_at, null);
  });
});

test("отложенное сообщение — только с временем следующей попытки; взятое и вход — только у ждущего", async () => {
  await withDatabase(async (db) => {
    const id = await message(db);
    await assert.rejects(
      db.query("update public.messages set retry_started_at = now() where id = $1", [id]),
      /messages_retry_check/,
    );
    await assert.rejects(
      db.query("update public.messages set retry_input = '{}'::jsonb where id = $1", [id]),
      /messages_retry_check/,
    );
    await assert.rejects(
      db.query("update public.messages set retry_attempts = -1 where id = $1", [id]),
      /messages_retry_attempts_check/,
    );
  });
});

// --- Отложить ----------------------------------------------------------------------

test("defer_message помечает сообщение ждущим: время, попытки и вход; ответа нет", async () => {
  await withDatabase(async (db) => {
    const id = await message(db);
    const at = minutesFromNow(1);

    assert.equal(await defer(db, id, { retryAt: at, attempts: 1, input: { swipe: { telegram_message_id: 7 } } }), true);

    const saved = await row(db, id);
    assert.equal(saved.retry_at?.toISOString(), at);
    assert.equal(saved.retry_attempts, 1);
    assert.equal(saved.retry_started_at, null);
    assert.deepEqual(saved.retry_input, { swipe: { telegram_message_id: 7 } });
    // «⏳ Принял…» в ответ не пишется: заполненный ответ — это повтор (§3.4).
    assert.equal(saved.reply, null);
  });
});

test("расшифровка голоса ложится текстом сообщения — разбор позже пойдёт по ней", async () => {
  await withDatabase(async (db) => {
    const id = await message(db, { text: "", kind: "voice" });

    await defer(db, id, { transcript: "в пятницу отправить расчёт Олегу", confidence: 0.92 });

    const saved = await row(db, id);
    assert.equal(saved.text, "в пятницу отправить расчёт Олегу");
    assert.equal(Number(saved.transcript_confidence), 0.92);
  });
});

test("повтор того же обновления ничего не меняет; у сообщения с ответом — false", async () => {
  await withDatabase(async (db) => {
    const id = await message(db);
    const first = minutesFromNow(1);
    await defer(db, id, { retryAt: first, attempts: 1 });

    assert.equal(await defer(db, id, { retryAt: minutesFromNow(30), attempts: 0 }), true);
    assert.equal((await row(db, id)).retry_at?.toISOString(), first);
    assert.equal((await row(db, id)).retry_attempts, 1);

    const answered = await message(db, { reply: "✅ Записал: позвонить Игорю" });
    assert.equal(await defer(db, answered), false);
    assert.equal((await row(db, answered)).retry_at, null);
  });
});

test("чужое или несуществующее сообщение отложить нельзя", async () => {
  await withDatabase(async (db) => {
    const id = await message(db);
    await assert.rejects(defer(db, id, { owner: STRANGER }), /not owned/);
    await assert.rejects(defer(db, "5b0c7a52-8f3e-4c1d-9a6b-2e4f1d3c8b90"), /not owned/);
    assert.equal((await row(db, id)).retry_at, null);
  });
});

test("новое сообщение будит очередь: ждущие не ждут дольше него", async () => {
  await withDatabase(async (db) => {
    const head = await message(db, { ago: 40 });
    await defer(db, head, { retryAt: minutesFromNow(25), attempts: 6 });
    const stranger = await message(db, { owner: STRANGER, ago: 40 });
    await defer(db, stranger, { owner: STRANGER, retryAt: minutesFromNow(25) });

    const now = minutesFromNow(0);
    await defer(db, await message(db), { retryAt: now, attempts: 0 });

    assert.equal((await row(db, head)).retry_at?.toISOString(), now);
    assert.notEqual((await row(db, stranger)).retry_at?.toISOString(), now, "чужая очередь не трогается");
  });
});

test("deferred_waiting — есть ли другое ждущее сообщение владельца", async () => {
  await withDatabase(async (db) => {
    const first = await message(db, { ago: 5 });
    const second = await message(db);

    assert.equal(await waiting(db, second), false);
    await defer(db, first);
    assert.equal(await waiting(db, second), true);
    assert.equal(await waiting(db, first), false, "само сообщение не в счёт");
    assert.equal(await waiting(db, second, STRANGER), false);

    await finish(db, first);
    assert.equal(await waiting(db, second), false);
  });
});

// --- Очередь -------------------------------------------------------------------------

test("take_deferred берёт голову по порядку получения: время начала и попытка плюс одна", async () => {
  await withDatabase(async (db) => {
    const later = await message(db, { ago: 5, text: "перенеси на пятницу" });
    const earlier = await message(db, { ago: 30 });
    await defer(db, later, { retryAt: minutesFromNow(-1) });
    await defer(db, earlier, { retryAt: minutesFromNow(-1) });

    const taken = only(await take(db));
    assert.equal(taken.id, earlier);
    assert.equal(taken.retry_attempts, 2);
    assert.ok(taken.retry_started_at);
  });
});

test("голова не созрела — очередь ждёт, даже если дальше созревшее", async () => {
  await withDatabase(async (db) => {
    const head = await message(db, { ago: 30 });
    const next = await message(db, { ago: 5 });
    await defer(db, head, { retryAt: minutesFromNow(10) });
    await defer(db, next, { retryAt: minutesFromNow(10) });
    await db.query("update public.messages set retry_at = now() - interval '1 minute' where id = $1", [next]);

    assert.deepEqual(await take(db), []);
    assert.equal(only(await take(db, minutesFromNow(11))).id, head);
  });
});

test("взятая голова держит очередь десять минут; брошенную берут снова", async () => {
  await withDatabase(async (db) => {
    const head = await message(db, { ago: 30 });
    const next = await message(db, { ago: 5 });
    await defer(db, head, { retryAt: minutesFromNow(-1) });
    await defer(db, next, { retryAt: minutesFromNow(-1) });

    assert.equal(only(await take(db)).id, head);
    assert.deepEqual(await take(db), [], "голова в работе — второе сообщение не обгоняет её");

    const again = only(await take(db, minutesFromNow(0), minutesFromNow(1)));
    assert.equal(again.id, head);
    assert.equal(again.retry_attempts, 3);
  });
});

test("записанный и не ушедший ответ берётся сразу и без новой попытки", async () => {
  await withDatabase(async (db) => {
    const id = await message(db, { ago: 30 });
    await defer(db, id, { retryAt: minutesFromNow(20), attempts: 4 });
    await understand(db, id);

    const taken = only(await take(db));
    assert.equal(taken.reply, "✅ Записал: позвонить Игорю");
    assert.equal(taken.retry_attempts, 4);
  });
});

test("чужая очередь не берётся; пустая — ничего", async () => {
  await withDatabase(async (db) => {
    assert.deepEqual(await take(db), []);
    const id = await message(db, { owner: STRANGER });
    await defer(db, id, { owner: STRANGER, retryAt: minutesFromNow(-1) });

    assert.deepEqual(await take(db), []);
    assert.equal(only(await take(db, minutesFromNow(0), minutesFromNow(-10), STRANGER)).id, id);
  });
});

test("release_deferred возвращает сообщение в очередь со следующим временем", async () => {
  await withDatabase(async (db) => {
    const id = await message(db);
    await defer(db, id, { retryAt: minutesFromNow(-1) });
    await take(db);
    const next = minutesFromNow(2);

    assert.equal(await release(db, id, next, STRANGER), false);
    assert.equal(await release(db, id, next), true);

    const saved = await row(db, id);
    assert.equal(saved.retry_started_at, null);
    assert.equal(saved.retry_at?.toISOString(), next);
    assert.equal(await release(db, await message(db), next), false, "не ждущее — нечего возвращать");
  });
});

test("finish_deferred снимает ожидание и вход, счёт попыток остаётся следом", async () => {
  await withDatabase(async (db) => {
    const id = await message(db);
    await defer(db, id, { retryAt: minutesFromNow(-1), input: { media_type: "image/jpeg" } });
    await take(db);
    await understand(db, id);

    assert.equal(await finish(db, id, STRANGER), false);
    assert.equal(await finish(db, id), true);
    assert.equal(await finish(db, id), false, "второй раз — уже не ждёт");

    const saved = await row(db, id);
    assert.equal(saved.retry_at, null);
    assert.equal(saved.retry_started_at, null);
    assert.equal(saved.retry_input, null);
    assert.equal(saved.retry_attempts, 2);
  });
});

test("разбор позже пишется прежним record_understanding, и повтор второй задачи не заводит", async () => {
  await withDatabase(async (db) => {
    const id = await message(db);
    await defer(db, id);

    const first = await understand(db, id, "✅ Разобрал ваше сообщение от 16:18:\nЗаписал: позвонить Игорю");
    const second = await understand(db, id, "другой ответ");

    assert.equal(first.length, 1);
    assert.deepEqual(second, first);
    assert.equal((await row(db, id)).reply, "✅ Разобрал ваше сообщение от 16:18:\nЗаписал: позвонить Игорю");
    const { rows } = await db.query<{ n: number }>("select count(*)::int as n from public.tasks");
    assert.equal(only(rows).n, 1);
  });
});

// --- Переписка ---------------------------------------------------------------------------

/** Чат Игоря с согласием и одним неразобранным сообщением; затих час назад. */
async function quietChat(db: PGlite, owner = OWNER, chat = "1001"): Promise<{ threadId: string; ids: string[] }> {
  await db.query("select public.connect_chat_source($1, 'telegram', $2, true)", [owner, `biz-${owner}`]);
  await db.query("select public.mark_consent_asked($1, 'telegram')", [owner]);
  await db.query("select public.answer_consent($1, 'telegram', true)", [owner]);
  const { rows } = await db.query<{ message_id: string }>(
    `select * from public.store_chat_message($1, 'telegram', $2, $3, 'Игорь Петров', '1', 'in',
       'Игорь Петров', now() - interval '1 hour', 'text', 'Пришлёшь расчёт в пятницу?', true)`,
    [owner, `biz-${owner}`, chat],
  );
  await db.query("update public.chat_threads set last_message_at = now() - interval '1 hour'");
  const { rows: threads } = await db.query<{ id: string }>(
    "select id from public.chat_threads where owner_telegram_id = $1 and chat_key = $2",
    [owner, chat],
  );
  return { threadId: only(threads).id, ids: [only(rows).message_id] };
}

interface DueChat {
  thread_id: string;
  retry_failures: number;
  retry_since: Date | null;
}

async function dueChats(db: PGlite): Promise<DueChat[]> {
  const { rows } = await db.query<DueChat>(
    "select * from public.chats_to_analyze($1, now() - interval '20 minutes', now() - interval '2 hours')",
    [OWNER],
  );
  return rows;
}

async function unavailable(db: PGlite, threadId: string, retryAt: string, owner = OWNER): Promise<number | null> {
  const { rows } = await db.query<{ n: number | null }>(
    "select public.chat_unavailable($1, $2, $3::timestamptz) as n",
    [owner, threadId, retryAt],
  );
  return only(rows).n;
}

interface ThreadRetry {
  failures: number;
  retry_failures: number;
  retry_since: Date | null;
  retry_at: Date | null;
}

async function threadRetry(db: PGlite, threadId: string): Promise<ThreadRetry> {
  const { rows } = await db.query<ThreadRetry>(
    "select failures, retry_failures, retry_since, retry_at from public.chat_threads where id = $1",
    [threadId],
  );
  return only(rows);
}

test("временная неудача: счёт плюс один, начало — при первой, постоянный счёт не трогается", async () => {
  await withDatabase(async (db) => {
    const { threadId } = await quietChat(db);
    const first = minutesFromNow(1);

    assert.equal(await unavailable(db, threadId, first), 1);
    const once = await threadRetry(db, threadId);
    assert.ok(once.retry_since);
    assert.equal(once.retry_at?.toISOString(), first);

    assert.equal(await unavailable(db, threadId, minutesFromNow(2)), 2);
    const twice = await threadRetry(db, threadId);
    assert.equal(twice.retry_since?.toISOString(), once.retry_since?.toISOString());
    assert.equal(twice.failures, 0, "три неудачи подряд — только постоянные");

    assert.equal(await unavailable(db, threadId, minutesFromNow(5), STRANGER), null);
  });
});

test("к разбору не идёт чат, чья следующая попытка ещё не пришла; счёт и начало — в ответе", async () => {
  await withDatabase(async (db) => {
    const { threadId } = await quietChat(db);
    const [fresh] = await dueChats(db);
    assert.equal(fresh?.retry_failures, 0);
    assert.equal(fresh?.retry_since, null);

    await unavailable(db, threadId, minutesFromNow(5));
    assert.deepEqual(await dueChats(db), []);

    await db.query("update public.chat_threads set retry_at = now() - interval '1 minute' where id = $1", [threadId]);
    const [due] = await dueChats(db);
    assert.equal(due?.thread_id, threadId);
    assert.equal(due?.retry_failures, 1);
    assert.ok(due?.retry_since);
  });
});

test("удачный разбор и пропуск куска обнуляют временные неудачи — и под ключом бота", async () => {
  await withDatabase(async (db) => {
    const { threadId, ids } = await quietChat(db);
    await unavailable(db, threadId, minutesFromNow(-1));
    await db.query(
      `select public.record_chat_analysis($1, $2, $3::uuid[], '{}'::jsonb, 'claude-opus-5', 1800, 240, 9500,
                                          'Игорем', null, '[]'::jsonb)`,
      [OWNER, threadId, ids],
    );
    assert.deepEqual(await threadRetry(db, threadId), {
      failures: 0,
      retry_failures: 0,
      retry_since: null,
      retry_at: null,
    });

    const oleg = await quietChat(db, OWNER, "1002");
    await unavailable(db, oleg.threadId, minutesFromNow(-1));
    // Триггер право исполнителя не проверяет: пропуск под ключом бота его зовёт.
    await db.exec("set role service_role");
    try {
      await db.query("select public.skip_chat_messages($1, $2, $3::uuid[])", [OWNER, oleg.threadId, oleg.ids]);
    } finally {
      await db.exec("reset role");
    }
    assert.equal((await threadRetry(db, oleg.threadId)).retry_failures, 0);
    assert.equal((await threadRetry(db, oleg.threadId)).retry_since, null);
  });
});
