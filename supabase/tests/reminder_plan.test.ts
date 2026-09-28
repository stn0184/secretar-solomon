/**
 * `reminder_plan` — правило §6.1 в одном месте (`techspec/11-edit.md` §11.3).
 *
 * Прежние случаи питоновского `plan()` из `bot/tests/test_reminders.py`
 * перенесены один в один; к ним — края, которые Python не проверял: утро
 * не раньше срока, пустая точность, срок ровно сейчас и день срока, который
 * в UTC другой, чем у владельца.
 *
 * Функция чистая, поэтому «сейчас» и пояс — аргументы, а часы базы не
 * участвуют. Моменты сверяются строками ISO в UTC.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import type { PGlite } from "@electric-sql/pglite";

import { asRole, withDatabase } from "./database.ts";

/** Пояс владельца в тестах бота: UTC+5, без перехода на летнее время. */
const YEKATERINBURG = "Asia/Yekaterinburg";

/** Понедельник, 21 сентября 2026 года, 10:00 у владельца. */
const MONDAY_MORNING = "2026-09-21T10:00:00+05:00";
/** Пятница той же недели, 18:00 — срок днём без часа. */
const FRIDAY_END_OF_DAY = "2026-09-25T18:00:00+05:00";

type Stage = [stage: string, fireAt: string];

interface Case {
  dueAt: string | null;
  precision: string | null;
  now: string;
  kind?: string;
  timezone?: string;
}

async function stages(db: PGlite, call: Case): Promise<Stage[]> {
  const { rows } = await db.query<{ stage: string; fire_at: Date }>(
    "select stage, fire_at from public.reminder_plan($1::timestamptz, $2, $3, $4, $5::timestamptz)",
    [call.dueAt, call.precision, call.kind ?? "task", call.timezone ?? YEKATERINBURG, call.now],
  );
  return rows.map((row) => [row.stage, row.fire_at.toISOString()]);
}

/** Момент строкой ISO в UTC — как его вернёт `Date.toISOString()`. */
function utc(moment: string): string {
  return new Date(moment).toISOString();
}

test("назван день: заранее — 09:00 того дня, к сроку — сам due_at", async () => {
  await withDatabase(async (db) => {
    assert.deepEqual(await stages(db, { dueAt: FRIDAY_END_OF_DAY, precision: "day", now: MONDAY_MORNING }), [
      ["before", utc("2026-09-25T09:00:00+05:00")],
      ["due", utc(FRIDAY_END_OF_DAY)],
    ]);
  });
});

test("утро сегодняшнего дня уже прошло — эта ступень не заводится", async () => {
  await withDatabase(async (db) => {
    const today = "2026-09-21T18:00:00+05:00";
    assert.deepEqual(await stages(db, { dueAt: today, precision: "day", now: MONDAY_MORNING }), [
      ["due", utc(today)],
    ]);
  });
});

test("назван час: за час и в срок", async () => {
  await withDatabase(async (db) => {
    const atThree = "2026-09-25T15:00:00+05:00";
    assert.deepEqual(await stages(db, { dueAt: atThree, precision: "time", now: MONDAY_MORNING }), [
      ["before", utc("2026-09-25T14:00:00+05:00")],
      ["due", utc(atThree)],
    ]);
  });
});

test("час до срока уже прошёл — остаётся только срок, стучаться немедленно не повод", async () => {
  await withDatabase(async (db) => {
    const atThree = "2026-09-21T15:00:00+05:00";
    const halfPastTwo = "2026-09-21T14:30:00+05:00";
    assert.deepEqual(await stages(db, { dueAt: atThree, precision: "time", now: halfPastTwo }), [
      ["due", utc(atThree)],
    ]);
  });
});

test("срок в прошлом — ничего", async () => {
  await withDatabase(async (db) => {
    const yesterday = "2026-09-20T18:00:00+05:00";
    assert.deepEqual(await stages(db, { dueAt: yesterday, precision: "day", now: MONDAY_MORNING }), []);
  });
});

test("задача без срока — ничего", async () => {
  await withDatabase(async (db) => {
    assert.deepEqual(await stages(db, { dueAt: null, precision: null, now: MONDAY_MORNING }), []);
  });
});

test("идея и желание — не дела: стучаться не о чем", async () => {
  await withDatabase(async (db) => {
    for (const kind of ["idea", "wish"]) {
      assert.deepEqual(
        await stages(db, { dueAt: FRIDAY_END_OF_DAY, precision: "day", now: MONDAY_MORNING, kind }),
        [],
        kind,
      );
    }
  });
});

test("срок в UTC — утро считается по поясу владельца", async () => {
  await withDatabase(async (db) => {
    assert.deepEqual(
      await stages(db, { dueAt: "2026-09-25T13:00:00Z", precision: "day", now: MONDAY_MORNING }),
      [
        ["before", utc("2026-09-25T09:00:00+05:00")],
        ["due", utc(FRIDAY_END_OF_DAY)],
      ],
    );
  });
});

test("утро не раньше срока — только срок", async () => {
  await withDatabase(async (db) => {
    // День без часа, но в due_at 08:00: 09:00 того же дня уже позже срока.
    const early = "2026-09-25T08:00:00+05:00";
    assert.deepEqual(await stages(db, { dueAt: early, precision: "day", now: MONDAY_MORNING }), [
      ["due", utc(early)],
    ]);
    // Ровно 09:00 — «заранее» совпало бы со сроком: одного напоминания хватит.
    const nine = "2026-09-25T09:00:00+05:00";
    assert.deepEqual(await stages(db, { dueAt: nine, precision: "day", now: MONDAY_MORNING }), [
      ["due", utc(nine)],
    ]);
  });
});

test("пустая точность у срока читается как день", async () => {
  await withDatabase(async (db) => {
    assert.deepEqual(await stages(db, { dueAt: FRIDAY_END_OF_DAY, precision: null, now: MONDAY_MORNING }), [
      ["before", utc("2026-09-25T09:00:00+05:00")],
      ["due", utc(FRIDAY_END_OF_DAY)],
    ]);
  });
});

test("срок ровно сейчас — уже не будущее, ничего", async () => {
  await withDatabase(async (db) => {
    assert.deepEqual(await stages(db, { dueAt: MONDAY_MORNING, precision: "time", now: MONDAY_MORNING }), []);
  });
});

test("«заранее» ровно сейчас — не заводится, остаётся срок", async () => {
  await withDatabase(async (db) => {
    const atEleven = "2026-09-21T11:00:00+05:00";
    assert.deepEqual(await stages(db, { dueAt: atEleven, precision: "time", now: MONDAY_MORNING }), [
      ["due", utc(atEleven)],
    ]);
  });
});

test("день срока берётся в поясе владельца, даже когда в UTC это другой день", async () => {
  await withDatabase(async (db) => {
    // 04:00 25-го в Екатеринбурге — это ещё 24-е в UTC. Утро 25-го позже
    // срока, поэтому только срок; день по UTC дал бы лишнее «заранее» 24-го.
    const smallHours = "2026-09-25T04:00:00+05:00";
    assert.deepEqual(await stages(db, { dueAt: smallHours, precision: "day", now: MONDAY_MORNING }), [
      ["due", utc(smallHours)],
    ]);

    // 18:00 25-го в Лос-Анджелесе — уже 26-е в UTC; утро — 09:00 25-го там же.
    const losAngeles = "America/Los_Angeles";
    const evening = "2026-09-25T18:00:00-07:00";
    assert.deepEqual(
      await stages(db, { dueAt: evening, precision: "day", now: MONDAY_MORNING, timezone: losAngeles }),
      [
        ["before", utc("2026-09-25T09:00:00-07:00")],
        ["due", utc(evening)],
      ],
    );
  });
});

test("у функции одна перегрузка; зовут её бот и Mini App, anon — нет", async () => {
  await withDatabase(async (db) => {
    const { rows } = await db.query<{ n: number }>(
      "select count(*)::int as n from pg_proc where proname = 'reminder_plan' and pronamespace = 'public'::regnamespace",
    );
    assert.equal(rows[0]?.n, 1);

    const rights = await db.query<{ role: string; allowed: boolean }>(
      `select role,
              has_function_privilege(role, 'public.reminder_plan(timestamptz, text, text, text, timestamptz)', 'execute')
                as allowed
         from unnest(array['anon', 'authenticated', 'service_role']) as role`,
    );
    assert.deepEqual(Object.fromEntries(rights.rows.map((row) => [row.role, row.allowed])), {
      anon: false,
      authenticated: true,
      service_role: true,
    });

    await asRole(db, "anon", null, async () => {
      await assert.rejects(
        stages(db, { dueAt: FRIDAY_END_OF_DAY, precision: "day", now: MONDAY_MORNING }),
        /permission denied/,
      );
    });
  });
});
