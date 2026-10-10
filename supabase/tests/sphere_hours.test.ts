/**
 * Часы по сферам в базе (этап 035): `sphere_hours` и `day_spans` на настоящем
 * Postgres. Одна функция счёта на бота и приложение (`techspec/32-redesign.md`
 * §32.3); правила — `techspec/31-hours.md` §31.1–31.4.
 *
 * Случаи счёта — те же, что проверяли прежний счёт бота
 * (`bot/tests/test_hours.py` до этапа 035), с теми же минутами: функция
 * обязана считать так же. Окна тест собирает так же, как бот, — по поясу
 * владельца (+05:00). Сессии переписки видны через `day_spans`.
 *
 * Бот зовёт функции ключом service-role — здесь это владелец базы с
 * владельцем параметром; приложение — роль `authenticated` с клеймом.
 * Имена и сферы выдуманные: VoiceFin, РЕЙВА, Игорь, Олег.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import type { PGlite } from "@electric-sql/pglite";

import { asRole, withDatabase } from "./database.ts";

const OWNER = 777;
const STRANGER = 999;

// Суббота, 10 октября 2026, 18:00 у владельца (+05:00). Неделя — с
// понедельника, 5 октября; вчера — пятница, 9 октября.
const SATURDAY = "2026-10-10";
const FRIDAY = "2026-10-09";
const MONDAY = "2026-10-05";
const SUNDAY = "2026-10-04";

/** Этот час в этот день по поясу владельца, строкой ISO. */
function at(hour: number, minute = 0, day = SATURDAY, second = 0): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${day}T${pad(hour)}:${pad(minute)}:${pad(second)}+05:00`;
}

const NOW = at(18);
type Window = readonly [string, string];
const TODAY: Window = [at(0), NOW];
const YESTERDAY: Window = [at(0, 0, FRIDAY), at(0)];
const WEEK: Window = [at(0, 0, MONDAY), NOW];

interface HoursRow {
  sphere_id: string | null;
  name: string | null;
  meetings: number;
  chat: number;
}

interface SpanRow {
  kind: string;
  starts_at: Date;
  ends_at: Date;
  sphere_id: string | null;
  task_id: string | null;
}

/** Сфера владельца; время заведения — явное: от него порядок и место. */
async function sphere(
  db: PGlite,
  name: string,
  createdAt: string,
  owner = OWNER,
  removed = false,
): Promise<string> {
  const { rows } = await db.query<{ id: string }>(
    `insert into public.spheres (owner_telegram_id, name, created_at, removed_at)
     values ($1, $2, $3, case when $4 then $3::timestamptz + interval '1 hour' end)
     returning id`,
    [owner, name, createdAt, removed],
  );
  return rows[0]!.id;
}

/** Встреча — дело с часом и длительностью (§31.1). */
async function meeting(
  db: PGlite,
  start: string,
  minutes: number | null,
  sphereId: string | null = null,
  { owner = OWNER, status = "active" }: { owner?: number; status?: string } = {},
): Promise<string> {
  const { rows } = await db.query<{ id: string }>(
    `insert into public.tasks (owner_telegram_id, title, due_at, due_precision, duration, sphere_id, status)
     values ($1, 'созвон с Игорем', $2, 'time', $3, $4, $5)
     returning id`,
    [owner, start, minutes, sphereId, status],
  );
  return rows[0]!.id;
}

let chatKey = 0;

/** Личный чат со сферой и его сообщения: время площадки и «моё ли». */
async function chat(
  db: PGlite,
  sphereId: string | null,
  moments: readonly (readonly [string, boolean])[],
  owner = OWNER,
): Promise<string> {
  chatKey += 1;
  const { rows } = await db.query<{ id: string }>(
    `insert into public.chat_threads (owner_telegram_id, platform, chat_key, name, sphere_id)
     values ($1, 'telegram', $2, 'Олег', $3)
     returning id`,
    [owner, String(chatKey), sphereId],
  );
  const thread = rows[0]!.id;
  let external = 0;
  for (const [sentAt, mine] of moments) {
    external += 1;
    await db.query(
      `insert into public.chat_messages (owner_telegram_id, thread_id, external_id, direction, sent_at, text)
       values ($1, $2, $3, $4, $5, 'созвонимся в пятницу?')`,
      [owner, thread, String(external), mine ? "out" : "in", sentAt],
    );
  }
  return thread;
}

/** Часы окна, как их зовёт бот: ключ service-role, владелец параметром. */
async function hours(db: PGlite, [start, end]: Window, owner: number | null = OWNER): Promise<HoursRow[]> {
  const { rows } = await db.query<HoursRow>(
    "select * from public.sphere_hours($1, $2, $3)",
    [start, end, owner],
  );
  return rows;
}

async function spans(db: PGlite, [start, end]: Window, owner = OWNER): Promise<SpanRow[]> {
  const { rows } = await db.query<SpanRow>("select * from public.day_spans($1, $2, $3)", [
    start,
    end,
    owner,
  ]);
  return rows;
}

/** Сфера → [встречи, переписка] в минутах; «без сферы» — `null`. */
function minutesOf(rows: readonly HoursRow[]): Map<string | null, [number, number]> {
  return new Map(rows.map((row) => [row.name, [row.meetings, row.chat]]));
}

/** Сессии переписки из `day_spans`: [сфера, начало, конец] строками ISO. */
function sessionsOf(rows: readonly SpanRow[]): [string | null, string, string][] {
  return rows
    .filter((row) => row.kind === "chat")
    .map((row): [string | null, string, string] => [
      row.sphere_id,
      row.starts_at.toISOString(),
      row.ends_at.toISOString(),
    ])
    .sort((a, b) => a[1].localeCompare(b[1]));
}

const iso = (moment: string) => new Date(moment).toISOString();

/** Две сферы владельца: VoiceFin заведена раньше РЕЙВА. */
async function book(db: PGlite): Promise<{ voicefin: string; reiva: string }> {
  const voicefin = await sphere(db, "VoiceFin", "2026-10-01T09:00:00+05:00");
  const reiva = await sphere(db, "РЕЙВА", "2026-10-02T09:00:00+05:00");
  return { voicefin, reiva };
}

// --- сессии переписки ---------------------------------------------------------

test("сообщения ближе десяти минут — одна сессия с хвостом в две минуты", () =>
  withDatabase(async (db) => {
    const { voicefin } = await book(db);
    await chat(db, voicefin, [
      [at(10, 0), false],
      [at(10, 4), true],
      [at(10, 14), false],
    ]);

    assert.deepEqual(sessionsOf(await spans(db, TODAY)), [[voicefin, iso(at(10, 0)), iso(at(10, 16))]]);
  }));

test("промежуток больше десяти минут начинает новую сессию", () =>
  withDatabase(async (db) => {
    await chat(db, null, [
      [at(10, 0), true],
      [at(10, 11), true],
    ]);

    assert.deepEqual(sessionsOf(await spans(db, TODAY)), [
      [null, iso(at(10, 0)), iso(at(10, 2))],
      [null, iso(at(10, 11)), iso(at(10, 13))],
    ]);
  }));

test("сессия без сообщения владельца не считается", () =>
  withDatabase(async (db) => {
    const { voicefin } = await book(db);
    await chat(db, voicefin, [
      [at(10, 0), false],
      [at(10, 5), false],
    ]);

    assert.deepEqual(await spans(db, TODAY), []);
    assert.deepEqual(await hours(db, TODAY), []);
  }));

test("сессии — по чатам, по времени площадки, а не по порядку прихода", () =>
  withDatabase(async (db) => {
    const { voicefin, reiva } = await book(db);
    await chat(db, reiva, [[at(10, 3), true]]);
    await chat(db, voicefin, [
      [at(10, 5), false],
      [at(10, 0), true],
    ]);

    assert.deepEqual(sessionsOf(await spans(db, TODAY)), [
      [voicefin, iso(at(10, 0)), iso(at(10, 7))],
      [reiva, iso(at(10, 3)), iso(at(10, 5))],
    ]);
  }));

// --- подсчёт --------------------------------------------------------------------

test("прошедшая встреча идёт в часы своей сферы", () =>
  withDatabase(async (db) => {
    const { voicefin } = await book(db);
    await meeting(db, at(14), 120, voicefin);

    assert.deepEqual(minutesOf(await hours(db, TODAY)), new Map([["VoiceFin", [120, 0]]]));
  }));

test("встреча считается только прошедшей частью", () =>
  withDatabase(async (db) => {
    const { voicefin, reiva } = await book(db);
    await meeting(db, at(17), 120, voicefin);
    await meeting(db, at(19), 60, reiva);

    assert.deepEqual(minutesOf(await hours(db, TODAY)), new Map([["VoiceFin", [60, 0]]]));
  }));

test("встреча и переписка в одно время в пределах сферы — один раз, встречей", () =>
  withDatabase(async (db) => {
    const { voicefin } = await book(db);
    await meeting(db, at(14), 60, voicefin);
    await chat(db, voicefin, [
      [at(14, 50), true],
      [at(14, 58), false],
      [at(15, 8), true],
    ]);

    assert.deepEqual(minutesOf(await hours(db, TODAY)), new Map([["VoiceFin", [60, 10]]]));
  }));

test("переписка двух чатов одной сферы в одно время — один раз", () =>
  withDatabase(async (db) => {
    const { voicefin } = await book(db);
    await chat(db, voicefin, [
      [at(10, 0), true],
      [at(10, 8), false],
    ]);
    await chat(db, voicefin, [[at(10, 5), true]]);

    assert.deepEqual(minutesOf(await hours(db, TODAY)), new Map([["VoiceFin", [0, 10]]]));
  }));

test("пересечение разных сфер считается у каждой", () =>
  withDatabase(async (db) => {
    const { voicefin, reiva } = await book(db);
    await meeting(db, at(14), 60, voicefin);
    await chat(db, reiva, [[at(14, 10), true]]);

    assert.deepEqual(
      minutesOf(await hours(db, TODAY)),
      new Map([
        ["VoiceFin", [60, 0]],
        ["РЕЙВА", [0, 2]],
      ]),
    );
  }));

test("убранная или пустая сфера — без сферы, и «без сферы» последней", () =>
  withDatabase(async (db) => {
    const { reiva } = await book(db);
    const gone = await sphere(db, "Дача", "2026-10-03T09:00:00+05:00", OWNER, true);
    await meeting(db, at(9), 30, null);
    await meeting(db, at(11), 30, gone);
    await chat(db, reiva, [[at(12), true]]);

    const rows = await hours(db, TODAY);
    assert.deepEqual(
      rows.map((row) => [row.sphere_id, row.name]),
      [
        [reiva, "РЕЙВА"],
        [null, null],
      ],
    );
    assert.deepEqual(minutesOf(rows).get(null), [60, 0]);
  }));

test("сферы идут в порядке заведения", () =>
  withDatabase(async (db) => {
    const { voicefin, reiva } = await book(db);
    await meeting(db, at(9), 30, reiva);
    await meeting(db, at(11), 30, voicefin);

    assert.deepEqual(
      (await hours(db, TODAY)).map((row) => row.name),
      ["VoiceFin", "РЕЙВА"],
    );
  }));

test("сегодня, вчера и неделя с понедельника — по часам владельца", () =>
  withDatabase(async (db) => {
    const { voicefin } = await book(db);
    await meeting(db, at(10), 60, voicefin);
    await meeting(db, at(10, 0, FRIDAY), 90, voicefin);
    await meeting(db, at(10, 0, MONDAY), 30, voicefin);
    await meeting(db, at(10, 0, SUNDAY), 45, voicefin);

    const found = [];
    for (const window of [TODAY, YESTERDAY, WEEK]) {
      found.push(minutesOf(await hours(db, window)));
    }
    assert.deepEqual(found, [
      new Map([["VoiceFin", [60, 0]]]),
      new Map([["VoiceFin", [90, 0]]]),
      new Map([["VoiceFin", [180, 0]]]),
    ]);
  }));

test("сессия через полночь делится между днями", () =>
  withDatabase(async (db) => {
    await chat(db, null, [
      [at(23, 55, FRIDAY), true],
      [at(0, 5), true],
    ]);

    assert.deepEqual(minutesOf(await hours(db, TODAY)), new Map([[null, [0, 7]]]));
    assert.deepEqual(minutesOf(await hours(db, YESTERDAY)), new Map([[null, [0, 5]]]));
  }));

test("сессия через край окна режется, а не обрывается: владелец ответил после полуночи", () =>
  withDatabase(async (db) => {
    await chat(db, null, [
      [at(23, 50, FRIDAY), false],
      [at(23, 58, FRIDAY), false],
      [at(0, 6), false],
      [at(0, 14), true],
    ]);

    assert.deepEqual(minutesOf(await hours(db, YESTERDAY)), new Map([[null, [0, 10]]]));
    assert.deepEqual(minutesOf(await hours(db, TODAY)), new Map([[null, [0, 16]]]));
  }));

test("в понедельник вчера — прошлое воскресенье, а неделя — сегодня", () =>
  withDatabase(async (db) => {
    const { reiva } = await book(db);
    await meeting(db, at(10, 0, SUNDAY), 60, reiva);
    const mondayEvening = at(20, 0, MONDAY);

    assert.deepEqual(
      minutesOf(await hours(db, [at(0, 0, SUNDAY), at(0, 0, MONDAY)])),
      new Map([["РЕЙВА", [60, 0]]]),
    );
    assert.deepEqual(await hours(db, [at(0, 0, MONDAY), mondayEvening]), []);
  }));

test("встреча, начатая накануне, задевает окно своим концом", () =>
  withDatabase(async (db) => {
    const { voicefin } = await book(db);
    await meeting(db, at(23, 0, FRIDAY), 120, voicefin);

    assert.deepEqual(minutesOf(await hours(db, TODAY)), new Map([["VoiceFin", [60, 0]]]));
    assert.deepEqual(minutesOf(await hours(db, YESTERDAY)), new Map([["VoiceFin", [60, 0]]]));
  }));

test("встречи и переписка двух сфер — как строки блока бота", () =>
  withDatabase(async (db) => {
    const { voicefin } = await book(db);
    await meeting(db, at(14), 120, voicefin);
    await chat(db, voicefin, [
      [at(16, 30), true],
      [at(16, 38), false],
    ]);
    await chat(db, null, [
      [at(11, 0), true],
      [at(11, 20), true],
      [at(11, 10), false],
      [at(11, 23), true],
    ]);

    const expected = new Map<string | null, [number, number]>([
      ["VoiceFin", [120, 10]],
      [null, [0, 25]],
    ]);
    assert.deepEqual(minutesOf(await hours(db, TODAY)), expected);
    assert.deepEqual(await hours(db, YESTERDAY), []);
    assert.deepEqual(minutesOf(await hours(db, WEEK)), expected);
  }));

test("секунды округляются до минут, а встречи и переписка в сумме — итог", () =>
  withDatabase(async (db) => {
    const { voicefin } = await book(db);
    await meeting(db, at(9), 31, voicefin);
    await chat(db, voicefin, [[at(9, 30, SATURDAY, 40), true]]);

    const [row] = await hours(db, TODAY);
    assert.deepEqual([row!.meetings, row!.chat, row!.meetings + row!.chat], [31, 2, 33]);
  }));

test("убранная встреча не считается, сделанная — считается, простое дело с часом — нет", () =>
  withDatabase(async (db) => {
    const { voicefin } = await book(db);
    await meeting(db, at(9), 60, voicefin, { status: "cancelled" });
    await meeting(db, at(11), 30, voicefin, { status: "done" });
    await meeting(db, at(13), null, voicefin);

    assert.deepEqual(minutesOf(await hours(db, TODAY)), new Map([["VoiceFin", [30, 0]]]));
  }));

test("идущая встреча — только до «сейчас» базы, какое бы окно ни передали", () =>
  withDatabase(async (db) => {
    const { voicefin } = await book(db);
    const { rows } = await db.query<{ start: string; from: string; to: string }>(
      `select (now() - interval '30 minutes')::text as start,
              (now() - interval '1 day')::text as from,
              (now() + interval '1 day')::text as to`,
    );
    const times = rows[0]!;
    await meeting(db, times.start, 120, voicefin);

    const [row] = await hours(db, [times.from, times.to]);
    assert.ok(row!.meetings >= 29 && row!.meetings <= 31, `встреча — ${row!.meetings} мин`);
  }));

// --- отрезки дня -----------------------------------------------------------------

test("отрезки дня: встречи — и будущие, переписка — сессиями, текста нет", () =>
  withDatabase(async (db) => {
    const { voicefin, reiva } = await book(db);
    const call = await meeting(db, at(14), 120, voicefin);
    const later = await meeting(db, "2030-10-10T19:00:00+05:00", 60, reiva);
    await meeting(db, at(9), 60, voicefin, { status: "cancelled" });
    await meeting(db, at(12), null, voicefin);
    await chat(db, reiva, [
      [at(16, 10), true],
      [at(16, 20), false],
    ]);
    await meeting(db, at(15), 60, null, { owner: STRANGER });

    const { rows, fields } = await db.query<SpanRow>("select * from public.day_spans($1, $2, $3)", [
      at(0),
      "2030-10-11T00:00:00+05:00",
      OWNER,
    ]);
    assert.deepEqual(
      fields.map((field) => field.name),
      ["kind", "starts_at", "ends_at", "sphere_id", "task_id"],
    );
    assert.deepEqual(
      rows.map((row) => [row.kind, row.starts_at.toISOString(), row.ends_at.toISOString(), row.sphere_id, row.task_id]),
      [
        ["meeting", iso(at(14)), iso(at(16)), voicefin, call],
        ["chat", iso(at(16, 10)), iso(at(16, 22)), reiva, null],
        ["meeting", iso("2030-10-10T19:00:00+05:00"), iso("2030-10-10T20:00:00+05:00"), reiva, later],
      ],
    );
  }));

test("отрезки дня: встреча через полночь видна в обоих днях, чужое — нет", () =>
  withDatabase(async (db) => {
    const { voicefin } = await book(db);
    await meeting(db, at(23, 0, FRIDAY), 120, voicefin);
    await chat(db, null, [[at(9), true]], STRANGER);

    assert.equal((await spans(db, YESTERDAY)).length, 1);
    assert.equal((await spans(db, TODAY)).length, 1);
    assert.deepEqual(await spans(db, TODAY, STRANGER), [
      {
        kind: "chat",
        starts_at: new Date(at(9)),
        ends_at: new Date(at(9, 2)),
        sphere_id: null,
        task_id: null,
      },
    ]);
  }));

// --- доступ ------------------------------------------------------------------------

/** Вызов из приложения: роль `authenticated`, владелец — клейм токена. */
async function asApp<T>(db: PGlite, owner: number | null, sql: string, params: unknown[]): Promise<T[]> {
  return asRole(db, "authenticated", owner, async () => (await db.query<T>(sql, params)).rows);
}

test("приложение видит только свои часы и отрезки — владелец из токена", () =>
  withDatabase(async (db) => {
    const { voicefin } = await book(db);
    await meeting(db, at(14), 60, voicefin);
    await chat(db, null, [[at(10), true]]);
    await meeting(db, at(14), 120, null, { owner: STRANGER });
    await chat(db, null, [[at(11), true]], STRANGER);

    const own = await asApp<HoursRow>(db, OWNER, "select * from public.sphere_hours($1, $2)", [...TODAY]);
    assert.deepEqual(
      minutesOf(own),
      new Map<string | null, [number, number]>([
        ["VoiceFin", [60, 0]],
        [null, [0, 2]],
      ]),
    );
    const sameOwner = await asApp<HoursRow>(db, OWNER, "select * from public.sphere_hours($1, $2, $3)", [
      ...TODAY,
      OWNER,
    ]);
    assert.deepEqual(sameOwner, own);

    const ownSpans = await asApp<SpanRow>(db, OWNER, "select * from public.day_spans($1, $2)", [...TODAY]);
    assert.deepEqual(
      ownSpans.map((row) => row.kind),
      ["chat", "meeting"],
    );
  }));

test("чужой владелец параметром при токене приложения — отказ", () =>
  withDatabase(async (db) => {
    await meeting(db, at(14), 120, null, { owner: STRANGER });

    await assert.rejects(
      asApp(db, OWNER, "select * from public.sphere_hours($1, $2, $3)", [...TODAY, STRANGER]),
      /owner refused/,
    );
    await assert.rejects(
      asApp(db, OWNER, "select * from public.day_spans($1, $2, $3)", [...TODAY, STRANGER]),
      /owner refused/,
    );
  }));

test("токен без клейма владельца — отказ, даже с владельцем параметром", () =>
  withDatabase(async (db) => {
    await meeting(db, at(14), 120);

    await assert.rejects(
      asApp(db, null, "select * from public.sphere_hours($1, $2, $3)", [...TODAY, OWNER]),
      /owner refused/,
    );
    await assert.rejects(
      asApp(db, null, "select * from public.day_spans($1, $2)", [...TODAY]),
      /owner refused/,
    );
  }));

test("бот без владельца — отказ; anon функции не зовёт вовсе", () =>
  withDatabase(async (db) => {
    await assert.rejects(hours(db, TODAY, null), /owner refused/);
    await asRole(db, "anon", null, async () => {
      await assert.rejects(
        db.query("select * from public.sphere_hours($1, $2, $3)", [...TODAY, OWNER]),
        /permission denied/,
      );
      await assert.rejects(
        db.query("select * from public.day_spans($1, $2, $3)", [...TODAY, OWNER]),
        /permission denied/,
      );
    });
  }));

test("права: часы и отрезки — приложению и боту, внутренние функции — никому", () =>
  withDatabase(async (db) => {
    const signatures = [
      ["public.sphere_hours(timestamptz, timestamptz, bigint)", { anon: false, authenticated: true, service_role: true }],
      ["public.day_spans(timestamptz, timestamptz, bigint)", { anon: false, authenticated: true, service_role: true }],
      ["public.hours_owner(bigint)", { anon: false, authenticated: false, service_role: false }],
      ["public.chat_sessions(bigint, timestamptz)", { anon: false, authenticated: false, service_role: false }],
    ] as const;
    for (const [signature, expected] of signatures) {
      const { rows } = await db.query<{ role: string; allowed: boolean }>(
        `select role, has_function_privilege(role, $1, 'execute') as allowed
           from unnest(array['anon', 'authenticated', 'service_role']) as role`,
        [signature],
      );
      assert.deepEqual(Object.fromEntries(rows.map((row) => [row.role, row.allowed])), expected, signature);
    }
  }));

test("переписку приложение не читает напрямую — только часы и отрезки функциями", () =>
  withDatabase(async (db) => {
    await chat(db, null, [[at(10), true]]);

    for (const table of ["chat_messages", "chat_threads", "chat_analyses"]) {
      for (const role of ["authenticated", "anon"] as const) {
        await asRole(db, role, OWNER, async () => {
          await assert.rejects(db.query(`select * from public.${table}`), /permission denied/, `${role}: ${table}`);
        });
      }
    }
    const rows = await asApp<SpanRow>(db, OWNER, "select * from public.day_spans($1, $2)", [...TODAY]);
    assert.equal(rows.length, 1);
    assert.ok(!("text" in rows[0]!));
  }));
