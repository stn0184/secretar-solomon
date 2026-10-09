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

import { asRole, withDatabase, withDatabaseBefore } from "./database.ts";

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

test("назван час: за час и за 5 минут до срока, в сам срок — нет", async () => {
  await withDatabase(async (db) => {
    const atThree = "2026-09-25T15:00:00+05:00";
    assert.deepEqual(await stages(db, { dueAt: atThree, precision: "time", now: MONDAY_MORNING }), [
      ["before", utc("2026-09-25T14:00:00+05:00")],
      ["due", utc("2026-09-25T14:55:00+05:00")],
    ]);
  });
});

test("час до срока уже прошёл — остаётся только за 5 минут, стучаться немедленно не повод", async () => {
  await withDatabase(async (db) => {
    const atThree = "2026-09-21T15:00:00+05:00";
    const halfPastTwo = "2026-09-21T14:30:00+05:00";
    assert.deepEqual(await stages(db, { dueAt: atThree, precision: "time", now: halfPastTwo }), [
      ["due", utc("2026-09-21T14:55:00+05:00")],
    ]);
  });
});

test("до срока 5 минут и меньше — напоминаний нет: ступень в прошлом не заводится", async () => {
  await withDatabase(async (db) => {
    const atThree = "2026-09-21T15:00:00+05:00";
    for (const now of ["2026-09-21T14:55:00+05:00", "2026-09-21T14:57:00+05:00"]) {
      assert.deepEqual(await stages(db, { dueAt: atThree, precision: "time", now }), [], now);
    }
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

test("«заранее» ровно сейчас — не заводится, остаётся за 5 минут", async () => {
  await withDatabase(async (db) => {
    const atEleven = "2026-09-21T11:00:00+05:00";
    assert.deepEqual(await stages(db, { dueAt: atEleven, precision: "time", now: MONDAY_MORNING }), [
      ["due", utc("2026-09-21T10:55:00+05:00")],
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

// --- Часть дня (§21.3) ----------------------------------------------------------

test("часть дня — одно напоминание в её начале, без «заранее»", async () => {
  await withDatabase(async (db) => {
    const parts: [string, string][] = [
      ["morning", "2026-09-25T08:00:00+05:00"],
      ["afternoon", "2026-09-25T12:00:00+05:00"],
      ["evening", "2026-09-25T18:00:00+05:00"],
    ];
    for (const [precision, start] of parts) {
      assert.deepEqual(
        await stages(db, { dueAt: start, precision, now: MONDAY_MORNING }),
        [["due", utc(start)]],
        precision,
      );
    }
  });
});

test("начало части уже прошло — напоминаний нет", async () => {
  await withDatabase(async (db) => {
    // «вечером позвонить маме» в 18:30: сегодняшний вечер начался в 18:00.
    const tonight = "2026-09-21T18:00:00+05:00";
    assert.deepEqual(
      await stages(db, { dueAt: tonight, precision: "evening", now: "2026-09-21T18:30:00+05:00" }),
      [],
    );
    // Ровно в начале части — тоже уже не будущее.
    assert.deepEqual(await stages(db, { dueAt: tonight, precision: "evening", now: tonight }), []);
  });
});

test("часть дня у идеи — не дело: стучаться не о чем", async () => {
  await withDatabase(async (db) => {
    const morning = "2026-09-25T08:00:00+05:00";
    assert.deepEqual(await stages(db, { dueAt: morning, precision: "morning", now: MONDAY_MORNING, kind: "idea" }), []);
  });
});

test("миграция части дня не трогает задачи и напоминания, записанные до неё", () =>
  withDatabaseBefore("20261004100000_part_of_day.sql", async (db, migrate) => {
    const owner = 777;
    // «вечером» до этапа — 19:00 со временем; дело на день — 18:00.
    const { rows: tasks } = await db.query<{ id: string }>(
      `insert into public.tasks (owner_telegram_id, title, due_at, due_precision)
       values ($1, 'позвонить маме', '2030-10-04T19:00:00+05:00', 'time'),
              ($1, 'отправить отчёт', '2030-10-04T18:00:00+05:00', 'day')
       returning id`,
      [owner],
    );
    await db.query(
      `insert into public.reminders (owner_telegram_id, task_id, stage, fire_at)
       values ($1, $2, 'before', '2030-10-04T18:00:00+05:00'),
              ($1, $2, 'due', '2030-10-04T19:00:00+05:00')`,
      [owner, tasks[0]!.id],
    );
    const snapshot = async () => ({
      tasks: (
        await db.query(
          "select id, title, due_at, due_precision, updated_at from public.tasks order by title",
        )
      ).rows,
      reminders: (
        await db.query<{ task_id: string; stage: string; fire_at: Date; sent_at: Date | null }>(
          "select task_id, stage, fire_at, sent_at from public.reminders order by stage",
        )
      ).rows,
    });
    const before = await snapshot();

    await migrate();

    // Миграция 020 строк не трогает. `migrate` катит и все следующие, а
    // миграция 029 переносит неотправленное «в срок» дела с часом на 5 минут
    // раньше (§6.1) — только этот сдвиг и отличает снимок.
    const meetingDue = before.reminders.find((row) => row.stage === "due")!;
    assert.deepEqual(await snapshot(), {
      ...before,
      reminders: before.reminders.map((row) =>
        row === meetingDue ? { ...row, fire_at: new Date(utc("2030-10-04T18:55:00+05:00")) } : row,
      ),
    });
    assert.deepEqual(
      (await db.query<{ due_precision: string }>("select due_precision from public.tasks order by title")).rows,
      [{ due_precision: "day" }, { due_precision: "time" }],
    );
  }));

// --- Встречи: за час и за 5 минут (этап 029, §6.1) -----------------------------

test("миграция встреч: неотправленное «в срок» у живого дела с часом — за 5 минут, остальное как было", () =>
  withDatabaseBefore("20261008100000_meeting_reminders.sql", async (db, migrate) => {
    const owner = 777;
    const { rows: tasks } = await db.query<{ id: string; title: string }>(
      `insert into public.tasks (owner_telegram_id, title, due_at, due_precision, status, repeat, occurrence_at)
       values ($1, 'созвон с Игорем', '2030-10-09T15:00:00+05:00', 'time', 'active', null, null),
              ($1, 'встреча с Олегом', '2030-10-09T12:00:00+05:00', 'time', 'active', null, null),
              ($1, 'планёрка', '2030-10-09T10:00:00+05:00', 'time', 'active',
                   '{"every": "day", "interval": 1, "time": "10:00"}', '2030-10-09T10:00:00+05:00'),
              ($1, 'позвонить Игорю', '2030-10-08T16:00:00+05:00', 'time', 'active', null, null),
              ($1, 'созвон отменился', '2030-10-09T17:00:00+05:00', 'time', 'cancelled', null, null),
              ($1, 'купить лампочку', '2030-10-09T18:00:00+05:00', 'day', 'active', null, null),
              ($1, 'позвонить маме', '2030-10-09T18:00:00+05:00', 'evening', 'active', null, null)
       returning id, title`,
      [owner],
    );
    const id = (title: string) => tasks.find((task) => task.title === title)!.id;
    // «Позвонить Игорю» напомнило в срок до миграции, у «встречи с Олегом» ушло
    // «за час»: ушедшее не переписывается.
    await db.query(
      `insert into public.reminders (owner_telegram_id, task_id, stage, fire_at, sent_at)
       values ($1, $2, 'before', '2030-10-09T14:00:00+05:00', null),
              ($1, $2, 'due', '2030-10-09T15:00:00+05:00', null),
              ($1, $3, 'before', '2030-10-09T11:00:00+05:00', '2030-10-09T11:00:05+05:00'),
              ($1, $3, 'due', '2030-10-09T12:00:00+05:00', null),
              ($1, $4, 'due', '2030-10-09T10:00:00+05:00', null),
              ($1, $5, 'due', '2030-10-08T16:00:00+05:00', '2030-10-08T16:00:05+05:00'),
              ($1, $6, 'due', '2030-10-09T17:00:00+05:00', null),
              ($1, $7, 'before', '2030-10-09T09:00:00+05:00', null),
              ($1, $7, 'due', '2030-10-09T18:00:00+05:00', null),
              ($1, $8, 'due', '2030-10-09T18:00:00+05:00', null)`,
      [
        owner,
        id("созвон с Игорем"),
        id("встреча с Олегом"),
        id("планёрка"),
        id("позвонить Игорю"),
        id("созвон отменился"),
        id("купить лампочку"),
        id("позвонить маме"),
      ],
    );
    const plan = async () =>
      (
        await db.query<{ title: string; stage: string; fire_at: Date; sent: boolean }>(
          `select t.title, r.stage, r.fire_at, r.sent_at is not null as sent
             from public.reminders r join public.tasks t on t.id = r.task_id
            order by t.title, r.stage`,
        )
      ).rows.map((row): [string, string, string, boolean] => [
        row.title,
        row.stage,
        row.fire_at.toISOString(),
        row.sent,
      ]);
    const tasksBefore = (await db.query("select * from public.tasks order by title")).rows;

    await migrate();

    assert.deepEqual(await plan(), [
      ["встреча с Олегом", "before", utc("2030-10-09T11:00:00+05:00"), true],
      ["встреча с Олегом", "due", utc("2030-10-09T11:55:00+05:00"), false],
      ["купить лампочку", "before", utc("2030-10-09T09:00:00+05:00"), false],
      ["купить лампочку", "due", utc("2030-10-09T18:00:00+05:00"), false],
      ["планёрка", "due", utc("2030-10-09T09:55:00+05:00"), false],
      ["позвонить Игорю", "due", utc("2030-10-08T16:00:00+05:00"), true],
      ["позвонить маме", "due", utc("2030-10-09T18:00:00+05:00"), false],
      ["созвон отменился", "due", utc("2030-10-09T17:00:00+05:00"), false],
      ["созвон с Игорем", "before", utc("2030-10-09T14:00:00+05:00"), false],
      ["созвон с Игорем", "due", utc("2030-10-09T14:55:00+05:00"), false],
    ]);
    // Задачи не тронуты: ни срок, ни `updated_at`. Сверяются колонки, что были
    // до миграции: следующие миграции добавляют свои (этап 032 — `sphere_id`).
    const columns = Object.keys(tasksBefore[0] ?? {});
    const tasksAfter = (await db.query<Record<string, unknown>>("select * from public.tasks order by title")).rows;
    assert.deepEqual(
      tasksAfter.map((row) => Object.fromEntries(columns.map((column) => [column, row[column]]))),
      tasksBefore,
    );
  }));

test("точность — пять значений: часть дня ложится, другое — отказ", async () => {
  await withDatabase(async (db) => {
    for (const precision of ["day", "time", "morning", "afternoon", "evening"]) {
      await db.query(
        `insert into public.tasks (owner_telegram_id, title, due_at, due_precision)
         values (777, 'дело', '2030-10-04T08:00:00+05:00', $1)`,
        [precision],
      );
    }
    await assert.rejects(
      db.query(
        `insert into public.tasks (owner_telegram_id, title, due_at, due_precision)
         values (777, 'дело', '2030-10-04T13:00:00+05:00', 'noon')`,
      ),
      /tasks_due_precision_check/,
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
