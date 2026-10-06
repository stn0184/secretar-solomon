/**
 * Поиск по поручению (§24.5): таблица `searches` под правилом «owner only» и
 * восемь функций бота — завести, взять в работу, записать ответ, пометить
 * `done`, вернуть попытку, пометить `failed`, найти поиски для тика и прошлый
 * завершённый поиск.
 *
 * Функции зовёт только бот ключом service-role; вызовы здесь идут от
 * владельца базы, права — отдельным тестом. `started_at` и `finished_at`
 * ставит база (`now()`), поэтому «прошло больше десяти минут» тест задаёт
 * границей `stale_before` в будущем, а не ожиданием.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import type { PGlite } from "@electric-sql/pglite";

import { asRole, withDatabase } from "./database.ts";

const OWNER = 777;
const STRANGER = 999;
const QUERY = "билеты на самолёт Екатеринбург — Москва 15 октября, обратно 18 октября";
const ANSWER = "1. Победа — прямые, от 3 500 ₽ (на момент поиска).\nhttps://www.flypobeda.ru/flights/SVX/MOW\n\nСоветую: Победа.";

interface SearchRow {
  id: string;
  query: string;
  attempts: number;
  answer: string | null;
  created_at: Date;
  request_chat_id: string | number;
  request_message_id: string | number;
}

interface StoredSearch {
  owner_telegram_id: string | number;
  message_id: string;
  query: string;
  status: string;
  attempts: number;
  started_at: Date | null;
  answer: string | null;
  telegram_message_id: string | number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  web_searches: number | null;
  web_fetches: number | null;
  duration_ms: number | null;
  created_at: Date;
  finished_at: Date | null;
}

const SIGNATURES = [
  "public.start_search(bigint, uuid, text)",
  "public.take_search(bigint, uuid, timestamptz)",
  "public.record_search_answer(bigint, uuid, text, integer, integer, integer, integer, integer)",
  "public.finish_search(bigint, uuid, bigint)",
  "public.release_search(bigint, uuid)",
  "public.fail_search(bigint, uuid)",
  "public.searches_to_resume(bigint, timestamptz)",
  "public.previous_search(bigint, timestamptz, timestamptz)",
];

let telegramMessageId = 100;

function only<T>(rows: T[]): T {
  assert.equal(rows.length, 1, `ждали одну строку, пришло ${rows.length}`);
  return rows[0]!;
}

/** Сейчас со сдвигом в минутах: граница «начат раньше» в будущем — «прошло». */
function minutesFromNow(minutes: number): string {
  return new Date(Date.now() + minutes * 60_000).toISOString();
}

/** Сообщение с просьбой; `reply` — что бот ответил, `null` — ответа ещё нет. */
async function message(db: PGlite, reply: string | null = "Ищу: …", owner = OWNER): Promise<string> {
  telegramMessageId += 1;
  const { rows } = await db.query<{ id: string }>(
    `insert into public.messages (owner_telegram_id, chat_id, telegram_message_id, text, reply)
     values ($1, $1, $2, 'найди билеты в Москву на 15-е', $3) returning id`,
    [owner, telegramMessageId, reply],
  );
  return only(rows).id;
}

async function start(db: PGlite, messageId: string, query = QUERY, owner = OWNER): Promise<string> {
  const { rows } = await db.query<{ id: string }>("select public.start_search($1, $2, $3) as id", [
    owner,
    messageId,
    query,
  ]);
  return only(rows).id;
}

async function take(db: PGlite, id: string, staleBefore = minutesFromNow(-10), owner = OWNER): Promise<SearchRow[]> {
  const { rows } = await db.query<SearchRow>("select * from public.take_search($1, $2, $3::timestamptz)", [
    owner,
    id,
    staleBefore,
  ]);
  return rows;
}

async function recordAnswer(db: PGlite, id: string, answer = ANSWER, owner = OWNER): Promise<boolean> {
  const { rows } = await db.query<{ ok: boolean }>(
    "select public.record_search_answer($1, $2, $3, 2400, 870, 2, 1, 41500) as ok",
    [owner, id, answer],
  );
  return only(rows).ok;
}

async function finish(db: PGlite, id: string, telegramId = 5151, owner = OWNER): Promise<boolean> {
  const { rows } = await db.query<{ ok: boolean }>("select public.finish_search($1, $2, $3) as ok", [
    owner,
    id,
    telegramId,
  ]);
  return only(rows).ok;
}

async function release(db: PGlite, id: string, owner = OWNER): Promise<number | null> {
  const { rows } = await db.query<{ attempts: number | null }>(
    "select public.release_search($1, $2) as attempts",
    [owner, id],
  );
  return only(rows).attempts;
}

async function fail(db: PGlite, id: string, owner = OWNER): Promise<boolean> {
  const { rows } = await db.query<{ ok: boolean }>("select public.fail_search($1, $2) as ok", [owner, id]);
  return only(rows).ok;
}

async function toResume(db: PGlite, staleBefore = minutesFromNow(-10), owner = OWNER): Promise<SearchRow[]> {
  const { rows } = await db.query<SearchRow>("select * from public.searches_to_resume($1, $2::timestamptz)", [
    owner,
    staleBefore,
  ]);
  return rows;
}

async function previous(
  db: PGlite,
  before: string,
  since: string,
  owner = OWNER,
): Promise<{ query: string; answer: string }[]> {
  const { rows } = await db.query<{ query: string; answer: string }>(
    "select * from public.previous_search($1, $2::timestamptz, $3::timestamptz)",
    [owner, before, since],
  );
  return rows;
}

async function stored(db: PGlite, id: string): Promise<StoredSearch> {
  const { rows } = await db.query<StoredSearch>("select * from public.searches where id = $1", [id]);
  return only(rows);
}

/** Поиск доведён до конца: взят, ответ записан, ушёл. */
async function done(db: PGlite, query = QUERY, owner = OWNER): Promise<string> {
  const id = await start(db, await message(db, "Ищу: …", owner), query, owner);
  await take(db, id, minutesFromNow(-10), owner);
  await recordAnswer(db, id, `ответ на «${query}»`, owner);
  await finish(db, id, 5151, owner);
  return id;
}

// --- Схема и права -----------------------------------------------------------

test("у каждой функции поиска одна перегрузка, и зовёт её только service_role", async () => {
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

test("функции поиска под anon и authenticated — отказ в праве, ничего не записано", async () => {
  await withDatabase(async (db) => {
    const messageId = await message(db);
    const id = "00000000-0000-4000-8000-000000000001";
    const calls: [string, unknown[]][] = [
      ["select public.start_search($1, $2, $3)", [OWNER, messageId, QUERY]],
      ["select * from public.take_search($1, $2, $3::timestamptz)", [OWNER, id, minutesFromNow(0)]],
      ["select public.record_search_answer($1, $2, $3, 1, 1, 1, 1, 1)", [OWNER, id, ANSWER]],
      ["select public.finish_search($1, $2, $3)", [OWNER, id, 1]],
      ["select public.release_search($1, $2)", [OWNER, id]],
      ["select public.fail_search($1, $2)", [OWNER, id]],
      ["select * from public.searches_to_resume($1, $2::timestamptz)", [OWNER, minutesFromNow(0)]],
      [
        "select * from public.previous_search($1, $2::timestamptz, $3::timestamptz)",
        [OWNER, minutesFromNow(0), minutesFromNow(-60)],
      ],
    ];
    for (const role of ["anon", "authenticated"] as const) {
      for (const [sql, params] of calls) {
        await asRole(db, role, OWNER, async () => {
          await assert.rejects(db.query(sql, params), /permission denied/, `${role}: ${sql}`);
        });
      }
    }
    const { rows } = await db.query("select * from public.searches");
    assert.deepEqual(rows, []);
  });
});

test("searches под правилом «owner only»: владелец видит свою строку, anon — ничего", async () => {
  await withDatabase(async (db) => {
    await start(db, await message(db));
    const foreignMessage = await message(db, "Ищу: …", STRANGER);
    await start(db, foreignMessage, QUERY, STRANGER);

    const seen = await asRole(db, "authenticated", OWNER, async () => {
      const { rows } = await db.query<{ owner_telegram_id: string | number }>(
        "select owner_telegram_id from public.searches",
      );
      return rows.map((row) => Number(row.owner_telegram_id));
    });
    assert.deepEqual(seen, [OWNER]);

    // `with check`: строку на чужого владельца не вставить.
    const mine = await message(db);
    await asRole(db, "authenticated", OWNER, async () => {
      await assert.rejects(
        db.query("insert into public.searches (owner_telegram_id, message_id, query) values ($1, $2, $3)", [
          STRANGER,
          mine,
          QUERY,
        ]),
        /row-level security/,
      );
    });

    await asRole(db, "anon", null, async () => {
      const { rows } = await db.query("select * from public.searches");
      assert.deepEqual(rows, []);
    });
  });
});

// --- Завести -----------------------------------------------------------------

test("start_search заводит строку pending без попыток; повтор по тому же сообщению — тот же id", async () => {
  await withDatabase(async (db) => {
    const messageId = await message(db);

    const id = await start(db, messageId);
    const row = await stored(db, id);
    assert.equal(Number(row.owner_telegram_id), OWNER);
    assert.equal(row.message_id, messageId);
    assert.equal(row.query, QUERY);
    assert.equal(row.status, "pending");
    assert.equal(row.attempts, 0);
    assert.equal(row.started_at, null);
    assert.equal(row.answer, null);
    assert.equal(row.finished_at, null);

    assert.equal(await start(db, messageId, "другой запрос"), id);
    assert.equal((await stored(db, id)).query, QUERY, "повтор запрос не меняет");
    const { rows } = await db.query<{ n: number }>("select count(*)::int as n from public.searches");
    assert.equal(rows[0]?.n, 1);
  });
});

test("start_search по чужому или несуществующему сообщению — отказ", async () => {
  await withDatabase(async (db) => {
    const foreign = await message(db, "Ищу: …", STRANGER);
    await assert.rejects(start(db, foreign), /not owned/);
    await assert.rejects(start(db, "00000000-0000-4000-8000-000000000009"), /not owned/);
    const { rows } = await db.query("select * from public.searches");
    assert.deepEqual(rows, []);
  });
});

test("запрос — от 1 до 500 знаков", async () => {
  await withDatabase(async (db) => {
    await assert.rejects(start(db, await message(db), ""), /searches_query_check/);
    await assert.rejects(start(db, await message(db), "я".repeat(501)), /searches_query_check/);
    const id = await start(db, await message(db), "я".repeat(500));
    assert.equal((await stored(db, id)).query.length, 500);
  });
});

// --- Взять в работу ----------------------------------------------------------

test("take_search берёт не начатый поиск: попытка, время начала и сообщение с просьбой", async () => {
  await withDatabase(async (db) => {
    const messageId = await message(db);
    const id = await start(db, messageId);

    const taken = only(await take(db, id));
    assert.equal(taken.id, id);
    assert.equal(taken.query, QUERY);
    assert.equal(taken.attempts, 1);
    assert.equal(taken.answer, null);
    assert.equal(Number(taken.request_chat_id), OWNER);
    assert.equal(Number(taken.request_message_id), telegramMessageId);

    const row = await stored(db, id);
    assert.ok(row.started_at !== null);
    assert.equal(row.attempts, 1);
  });
});

test("начатый поиск второй раз не берётся, пока не прошло десять минут; потом — с новой попыткой", async () => {
  await withDatabase(async (db) => {
    const id = await start(db, await message(db));
    assert.equal((await take(db, id)).length, 1);

    assert.deepEqual(await take(db, id), [], "начат только что");
    const again = only(await take(db, id, minutesFromNow(1)));
    assert.equal(again.attempts, 2);
  });
});

test("take_search не берёт поиск с ответом, завершённый, не удавшийся и чужой", async () => {
  await withDatabase(async (db) => {
    const answered = await start(db, await message(db));
    await take(db, answered);
    await recordAnswer(db, answered);

    const finished = await done(db);

    const failed = await start(db, await message(db));
    await fail(db, failed);

    const foreign = await start(db, await message(db, "Ищу: …", STRANGER), QUERY, STRANGER);

    const later = minutesFromNow(1);
    assert.deepEqual(await take(db, answered, later), []);
    assert.deepEqual(await take(db, finished, later), []);
    assert.deepEqual(await take(db, failed, later), []);
    assert.deepEqual(await take(db, foreign, later), []);
    assert.equal((await stored(db, foreign)).attempts, 0, "чужая строка не тронута");
  });
});

// --- Ответ, done, попытка, failed --------------------------------------------

test("ответ и след ложатся одной записью; второй раз и у чужого — false", async () => {
  await withDatabase(async (db) => {
    const id = await start(db, await message(db));
    await take(db, id);

    assert.equal(await recordAnswer(db, id), true);
    const row = await stored(db, id);
    assert.equal(row.answer, ANSWER);
    assert.equal(row.input_tokens, 2400);
    assert.equal(row.output_tokens, 870);
    assert.equal(row.web_searches, 2);
    assert.equal(row.web_fetches, 1);
    assert.equal(row.duration_ms, 41500);
    assert.equal(row.status, "pending", "ответ записан, но ещё не ушёл");

    assert.equal(await recordAnswer(db, id, "другой ответ"), false);
    assert.equal((await stored(db, id)).answer, ANSWER);

    const foreign = await start(db, await message(db, "Ищу: …", STRANGER), QUERY, STRANGER);
    assert.equal(await recordAnswer(db, foreign), false);
    assert.equal((await stored(db, foreign)).answer, null);
  });
});

test("finish_search: done с id сообщения и временем; без ответа и второй раз — false", async () => {
  await withDatabase(async (db) => {
    const id = await start(db, await message(db));
    await take(db, id);
    assert.equal(await finish(db, id), false, "без ответа помечать нечего");

    await recordAnswer(db, id);
    assert.equal(await finish(db, id, 4242), true);
    const row = await stored(db, id);
    assert.equal(row.status, "done");
    assert.equal(Number(row.telegram_message_id), 4242);
    assert.ok(row.finished_at !== null);

    assert.equal(await finish(db, id, 5353), false);
    assert.equal(Number((await stored(db, id)).telegram_message_id), 4242);
  });
});

test("release_search возвращает попытку и называет число попыток; с ответом и чужой — null", async () => {
  await withDatabase(async (db) => {
    const id = await start(db, await message(db));
    await take(db, id);

    assert.equal(await release(db, id), 1);
    const row = await stored(db, id);
    assert.equal(row.started_at, null);
    assert.equal(row.status, "pending");

    // Возвращённая попытка — поиск снова не начат и берётся сразу.
    assert.equal(only(await take(db, id)).attempts, 2);
    assert.equal(await release(db, id), 2);

    const answered = await start(db, await message(db));
    await take(db, answered);
    await recordAnswer(db, answered);
    assert.equal(await release(db, answered), null);

    const foreign = await start(db, await message(db, "Ищу: …", STRANGER), QUERY, STRANGER);
    assert.equal(await release(db, foreign), null);
  });
});

test("fail_search: failed и время; у поиска с ответом и у завершённого — false", async () => {
  await withDatabase(async (db) => {
    const id = await start(db, await message(db));
    assert.equal(await fail(db, id), true);
    const row = await stored(db, id);
    assert.equal(row.status, "failed");
    assert.ok(row.finished_at !== null);
    assert.equal(await fail(db, id), false);

    const answered = await start(db, await message(db));
    await take(db, answered);
    await recordAnswer(db, answered);
    assert.equal(await fail(db, answered), false, "записанный ответ ещё уйдёт");

    assert.equal(await fail(db, await done(db)), false);
  });
});

test("строка держит согласие статуса: done без сообщения и pending со временем конца не записать", async () => {
  await withDatabase(async (db) => {
    const id = await start(db, await message(db));
    await assert.rejects(
      db.query("update public.searches set status = 'done', finished_at = now() where id = $1", [id]),
      /searches_done_check/,
    );
    await assert.rejects(
      db.query("update public.searches set finished_at = now() where id = $1", [id]),
      /searches_finished_check/,
    );
    await assert.rejects(
      db.query("update public.searches set status = 'lost', finished_at = now() where id = $1", [id]),
      /searches_status_check/,
    );
  });
});

// --- Тик ---------------------------------------------------------------------

test("тику — поиски, о которых сказано «Ищу»: не начатые, начатые давно и с записанным ответом", async () => {
  await withDatabase(async (db) => {
    const fresh = await start(db, await message(db));

    const running = await start(db, await message(db));
    await take(db, running);

    const answered = await start(db, await message(db));
    await take(db, answered);
    await recordAnswer(db, answered);

    const unsaid = await start(db, await message(db, null));
    const finished = await done(db);
    const failed = await start(db, await message(db));
    await fail(db, failed);
    await start(db, await message(db, "Ищу: …", STRANGER), QUERY, STRANGER);

    const now = await toResume(db);
    assert.deepEqual(
      now.map((row) => row.id),
      [fresh, answered],
      "начатый только что — ещё в работе, без «Ищу» — не наш, завершённые — нет",
    );
    const withAnswer = now[1]!;
    assert.equal(withAnswer.answer, ANSWER);
    assert.equal(withAnswer.attempts, 1);
    assert.equal(Number(withAnswer.request_chat_id), OWNER);

    const later = await toResume(db, minutesFromNow(1));
    assert.deepEqual(
      later.map((row) => row.id),
      [fresh, running, answered],
      "через десять минут начатый — брошенный",
    );
    assert.ok(!later.some((row) => [unsaid, finished, failed].includes(row.id)));
  });
});

// --- Прошлый поиск -----------------------------------------------------------

test("прошлый поиск — последний завершённый до текущего и не раньше чем за час до него", async () => {
  await withDatabase(async (db) => {
    const old = await done(db, "старый запрос");
    const recent = await done(db, "свежий запрос");
    const failed = await start(db, await message(db));
    await fail(db, failed);
    await start(db, await message(db));
    await done(db, "чужой запрос", STRANGER);

    // Даты — как было бы час с лишним назад и десять минут назад.
    await db.query(
      `update public.searches set created_at = now() - interval '3 hours',
                                  finished_at = now() - interval '2 hours'
        where id = $1`,
      [old],
    );
    await db.query(
      `update public.searches set created_at = now() - interval '20 minutes',
                                  finished_at = now() - interval '10 minutes'
        where id = $1`,
      [recent],
    );

    const nowIso = minutesFromNow(0);
    assert.deepEqual(await previous(db, nowIso, minutesFromNow(-60)), [
      { query: "свежий запрос", answer: "ответ на «свежий запрос»" },
    ]);
    assert.deepEqual(
      await previous(db, nowIso, minutesFromNow(-180)),
      [{ query: "свежий запрос", answer: "ответ на «свежий запрос»" }],
      "из двух — последний",
    );
    assert.deepEqual(await previous(db, minutesFromNow(-25), minutesFromNow(-60)), [], "заведён позже границы");
    assert.deepEqual(await previous(db, nowIso, minutesFromNow(-5)), [], "закончился раньше часа");
    assert.deepEqual(
      (await previous(db, nowIso, minutesFromNow(-60), STRANGER)).map((row) => row.query),
      ["чужой запрос"],
    );
  });
});
