/**
 * Утренний план (§20): `day_tasks` отбирает дела дня, `morning_plan_sent` и
 * `record_morning_plan` помнят, был ли план, таблица `morning_plans` — под
 * правилом «owner only».
 *
 * Все три функции зовёт только бот ключом service-role; вызовы здесь идут от
 * владельца базы, права — отдельным тестом. Границы дня считает бот
 * (`services/morning.py`) — тест задаёт их сам: полночь понедельника и
 * вторника по поясу владельца (UTC+5).
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import type { PGlite } from "@electric-sql/pglite";

import { asRole, withDatabase } from "./database.ts";

const OWNER = 777;
const STRANGER = 999;

/** Понедельник, 7 октября 2030 года, — день плана у владельца. */
const DAY = "2030-10-07";
const NEXT_DAY = "2030-10-08";
const DAY_START = "2030-10-07T00:00:00+05:00";
const DAY_END = "2030-10-08T00:00:00+05:00";

interface DayTask {
  task_id: string;
  title: string;
  due_at: Date;
  due_precision: string | null;
}

interface PlanRow {
  owner_telegram_id: string | number;
  day: string;
  telegram_message_id: string | number;
  created_at: Date;
}

function only<T>(rows: T[]): T {
  assert.equal(rows.length, 1, `ждали одну строку, пришло ${rows.length}`);
  return rows[0]!;
}

/** Момент у владельца: `at("09:00")` — 09:00 понедельника, дня плана. */
function at(clock: string, day = DAY): string {
  return `${day}T${clock}:00+05:00`;
}

function iso(moment: string): string {
  return new Date(moment).toISOString();
}

interface Seed {
  owner?: number;
  title?: string;
  kind?: "task" | "idea" | "wish";
  status?: "active" | "done" | "cancelled";
  due?: string | null;
  precision?: "day" | "time" | "morning" | "afternoon" | "evening" | null;
  repeat?: Record<string, unknown> | null;
  created?: string;
}

let seeded = 0;

/**
 * Задача прямой вставкой. По умолчанию — срок сегодня в 09:00 со временем;
 * каждая следующая записана на минуту позже предыдущей.
 */
async function seedTask(db: PGlite, seed: Seed = {}): Promise<string> {
  seeded += 1;
  const due = seed.due === undefined ? at("09:00") : seed.due;
  const precision = seed.precision === undefined ? (due === null ? null : "time") : seed.precision;
  const repeat = seed.repeat ?? null;
  const { rows } = await db.query<{ id: string }>(
    `insert into public.tasks (
       owner_telegram_id, title, kind, status, due_at, due_precision, repeat, occurrence_at, created_at
     )
     values ($1, $2, $3, $4, $5::timestamptz, $6, $7::jsonb, $8::timestamptz, $9::timestamptz)
     returning id`,
    [
      seed.owner ?? OWNER,
      seed.title ?? "встреча с Ольгой",
      seed.kind ?? "task",
      seed.status ?? "active",
      due,
      precision,
      repeat === null ? null : JSON.stringify(repeat),
      repeat === null ? null : due,
      seed.created ?? new Date(Date.UTC(2030, 9, 1, 0, seeded)).toISOString(),
    ],
  );
  return only(rows).id;
}

async function dayTasks(db: PGlite, owner = OWNER): Promise<DayTask[]> {
  const { rows } = await db.query<DayTask>(
    "select * from public.day_tasks($1, $2::timestamptz, $3::timestamptz)",
    [owner, DAY_START, DAY_END],
  );
  return rows;
}

async function planSent(db: PGlite, day = DAY, owner = OWNER): Promise<boolean> {
  const { rows } = await db.query<{ sent: boolean }>(
    "select public.morning_plan_sent($1, $2::date) as sent",
    [owner, day],
  );
  return only(rows).sent;
}

async function recordPlan(db: PGlite, messageId = 4242, day = DAY, owner = OWNER): Promise<boolean> {
  const { rows } = await db.query<{ recorded: boolean }>(
    "select public.record_morning_plan($1, $2::date, $3) as recorded",
    [owner, day, messageId],
  );
  return only(rows).recorded;
}

async function plans(db: PGlite): Promise<PlanRow[]> {
  const { rows } = await db.query<PlanRow>(
    `select owner_telegram_id, day::text as day, telegram_message_id, created_at
       from public.morning_plans order by owner_telegram_id, day`,
  );
  return rows;
}

// --- Схема и права -----------------------------------------------------------

test("у каждой функции плана одна перегрузка, и зовёт её только service_role", async () => {
  await withDatabase(async (db) => {
    const signatures = [
      "public.morning_plan_sent(bigint, date)",
      "public.day_tasks(bigint, timestamptz, timestamptz)",
      "public.record_morning_plan(bigint, date, bigint)",
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

test("функции плана под anon и authenticated — отказ в праве, ничего не записано", async () => {
  await withDatabase(async (db) => {
    const calls: [string, unknown[]][] = [
      ["select public.morning_plan_sent($1, $2::date)", [OWNER, DAY]],
      ["select * from public.day_tasks($1, $2::timestamptz, $3::timestamptz)", [OWNER, DAY_START, DAY_END]],
      ["select public.record_morning_plan($1, $2::date, $3)", [OWNER, DAY, 1]],
    ];
    for (const role of ["anon", "authenticated"] as const) {
      for (const [sql, params] of calls) {
        await asRole(db, role, OWNER, async () => {
          await assert.rejects(db.query(sql, params), /permission denied/, `${role}: ${sql}`);
        });
      }
    }
    assert.deepEqual(await plans(db), []);
  });
});

test("morning_plans под правилом «owner only»: владелец видит свою строку, anon — ничего", async () => {
  await withDatabase(async (db) => {
    await recordPlan(db, 4242);
    await recordPlan(db, 5151, DAY, STRANGER);

    const seen = await asRole(db, "authenticated", OWNER, async () => {
      const { rows } = await db.query<{ owner_telegram_id: string | number }>(
        "select owner_telegram_id from public.morning_plans",
      );
      return rows.map((row) => Number(row.owner_telegram_id));
    });
    assert.deepEqual(seen, [OWNER]);

    // `with check`: строку на чужого владельца не вставить.
    await asRole(db, "authenticated", OWNER, async () => {
      await assert.rejects(
        db.query(
          "insert into public.morning_plans (owner_telegram_id, day, telegram_message_id) values ($1, $2::date, 1)",
          [STRANGER, NEXT_DAY],
        ),
        /row-level security/,
      );
    });

    await asRole(db, "anon", null, async () => {
      const { rows } = await db.query("select * from public.morning_plans");
      assert.deepEqual(rows, []);
    });
  });
});

// --- Дела дня ----------------------------------------------------------------

test("в план идут только активные задачи владельца со сроком сегодня, обеих точностей", async () => {
  await withDatabase(async (db) => {
    await seedTask(db, { title: "идея", kind: "idea" });
    await seedTask(db, { title: "желание", kind: "wish" });
    await seedTask(db, { title: "закрыта", status: "done" });
    await seedTask(db, { title: "убрана", status: "cancelled" });
    await seedTask(db, { title: "без срока", due: null });
    await seedTask(db, { title: "вчера поздно", due: at("23:59", "2030-10-06") });
    await seedTask(db, { title: "вчера днём", due: at("18:00", "2030-10-06"), precision: "day" });
    await seedTask(db, { title: "завтра в полночь", due: DAY_END });
    await seedTask(db, { title: "завтра днём", due: at("18:00", NEXT_DAY), precision: "day" });
    await seedTask(db, { title: "чужая", owner: STRANGER });

    assert.deepEqual(await dayTasks(db), []);

    const midnight = await seedTask(db, { title: "сегодня в полночь", due: DAY_START });
    const meeting = await seedTask(db, { title: "встреча с Ольгой", due: at("09:00") });
    const late = await seedTask(db, { title: "поздний звонок", due: at("23:59") });
    const bulb = await seedTask(db, { title: "купить лампочку в коридор", due: at("18:00"), precision: "day" });

    const rows = await dayTasks(db);
    assert.deepEqual(
      rows.map((row) => row.task_id),
      [midnight, meeting, bulb, late],
    );
    const timed = rows[1]!;
    assert.equal(timed.title, "встреча с Ольгой");
    assert.equal(timed.due_precision, "time");
    assert.equal(timed.due_at.toISOString(), iso(at("09:00")));
    const daily = rows[2]!;
    assert.equal(daily.title, "купить лампочку в коридор");
    assert.equal(daily.due_precision, "day");
    assert.equal(daily.due_at.toISOString(), iso(at("18:00")));
  });
});

test("повторяющаяся задача — по сроку своего раза: сегодняшний раз в плане, завтрашний — нет", async () => {
  await withDatabase(async (db) => {
    const daily = { every: "day", interval: 1, time: "10:00" };
    const today = await seedTask(db, { title: "планёрка", due: at("10:00"), repeat: daily });
    await seedTask(db, { title: "зарядка", due: at("10:00", NEXT_DAY), repeat: daily });

    const row = only(await dayTasks(db));
    assert.equal(row.task_id, today);
    assert.equal(row.due_at.toISOString(), iso(at("10:00")));
    assert.equal(row.due_precision, "time");
  });
});

test("порядок: по сроку, при равном — по записи, при равной записи — по id", async () => {
  await withDatabase(async (db) => {
    const call = await seedTask(db, { title: "позвонить Сергею", due: at("15:30") });
    const parcel = await seedTask(db, { title: "забрать посылку", due: at("18:00"), precision: "day" });
    const bulb = await seedTask(db, {
      title: "купить лампочку",
      due: at("18:00"),
      precision: "day",
      created: "2030-09-01T00:00:00Z",
    });
    const meeting = await seedTask(db, { title: "встреча с Ольгой", due: at("09:00") });
    const same = "2030-09-02T00:00:00Z";
    const twins = [
      await seedTask(db, { title: "близнец", due: at("12:00"), created: same }),
      await seedTask(db, { title: "близнец", due: at("12:00"), created: same }),
    ].sort();

    assert.deepEqual(
      (await dayTasks(db)).map((row) => row.task_id),
      [meeting, ...twins, call, bulb, parcel],
    );
  });
});

test("часть дня — в плане по своему началу среди дел со временем, дела на день — после", async () => {
  await withDatabase(async (db) => {
    const bulb = await seedTask(db, { title: "купить лампочку", due: at("18:00"), precision: "day" });
    const evening = await seedTask(db, { title: "позвонить маме", due: at("18:00"), precision: "evening" });
    const early = await seedTask(db, { title: "выгулять собаку", due: at("07:30") });
    const morning = await seedTask(db, { title: "встреча с Ренатой", due: at("08:00"), precision: "morning" });
    const meeting = await seedTask(db, { title: "встреча с Ольгой", due: at("09:00") });
    const afternoon = await seedTask(db, { title: "забрать посылку", due: at("12:00"), precision: "afternoon" });

    const rows = await dayTasks(db);

    // У дела на день и у вечера одно 18:00: порядок — по записи, а дела на
    // день бот ставит в конец сам (§20.3).
    assert.deepEqual(
      rows.map((row) => row.task_id),
      [early, morning, meeting, afternoon, bulb, evening],
    );
    assert.deepEqual(
      rows.map((row) => row.due_precision),
      ["time", "morning", "time", "afternoon", "day", "evening"],
    );
  });
});

test("чужие дела владельцу не видны, а его — чужому", async () => {
  await withDatabase(async (db) => {
    const mine = await seedTask(db, { title: "моё" });
    const foreign = await seedTask(db, { title: "чужое", owner: STRANGER });

    assert.deepEqual(
      (await dayTasks(db)).map((row) => row.task_id),
      [mine],
    );
    assert.deepEqual(
      (await dayTasks(db, STRANGER)).map((row) => row.task_id),
      [foreign],
    );
  });
});

// --- Был ли план и запись ----------------------------------------------------

test("план записан: строка за день с сообщением, «был ли план» — да только за этот день", async () => {
  await withDatabase(async (db) => {
    assert.equal(await planSent(db), false);

    const before = Date.now();
    assert.equal(await recordPlan(db, 4242), true);

    assert.equal(await planSent(db), true);
    assert.equal(await planSent(db, NEXT_DAY), false, "завтра плана ещё не было");

    const row = only(await plans(db));
    assert.equal(Number(row.owner_telegram_id), OWNER);
    assert.equal(row.day, DAY);
    assert.equal(Number(row.telegram_message_id), 4242);
    assert.ok(row.created_at.getTime() >= before - 1000, "created_at — время записи");
  });
});

test("повторная запись за тот же день — false, первая строка не тронута; другой день — новая", async () => {
  await withDatabase(async (db) => {
    assert.equal(await recordPlan(db, 4242), true);
    assert.equal(await recordPlan(db, 5151), false);

    const row = only(await plans(db));
    assert.equal(Number(row.telegram_message_id), 4242);

    assert.equal(await recordPlan(db, 6161, NEXT_DAY), true);
    assert.equal(await recordPlan(db, 7171, DAY, STRANGER), true, "у чужого — свой день");
    assert.equal((await plans(db)).length, 3);
  });
});

test("чужой план владельцу не засчитывается, его — чужому", async () => {
  await withDatabase(async (db) => {
    await recordPlan(db, 4242, DAY, STRANGER);
    assert.equal(await planSent(db), false);
    assert.equal(await planSent(db, DAY, STRANGER), true);
  });
});
