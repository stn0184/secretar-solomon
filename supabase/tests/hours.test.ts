/**
 * Длительность встречи на настоящем Postgres (этап 033): колонка `duration` у
 * задач, её запись из разбора, правка ключом `duration` и триггер, который
 * снимает её, когда срок уходит с часа.
 *
 * Правила — `techspec/31-hours.md` §31.1 и `techspec/03-schema.md` §3.3,
 * §3.6. Бот зовёт функции ключом service-role — здесь это владелец базы.
 * Дела и люди выдуманные: VoiceFin, Игорь.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import type { PGlite } from "@electric-sql/pglite";

import { asRole, withDatabase } from "./database.ts";

const OWNER = 777;

type Json = null | boolean | number | string | Json[] | { [key: string]: Json };

interface TaskRow {
  id: string;
  title: string;
  due_at: Date | null;
  due_precision: string | null;
  duration: number | null;
}

/** Суббота, 5 октября 2030 года, 14:00 у владельца (UTC+5) — 09:00 UTC. */
const SATURDAY_TWO = "2030-10-05T14:00:00+05:00";
const SATURDAY_THREE = "2030-10-05T15:00:00+05:00";

let telegramMessageId = 0;

function only<T>(rows: T[]): T {
  assert.equal(rows.length, 1, `ждали одну строку, пришло ${rows.length}`);
  return rows[0]!;
}

async function saveZone(db: PGlite): Promise<void> {
  await db.query("select public.save_owner_timezone($1, $2)", [OWNER, "Asia/Yekaterinburg"]);
}

function task(fields: { [key: string]: Json }): Json {
  return {
    title: "созвон по VoiceFin",
    kind: "task",
    due_at: SATURDAY_TWO,
    due_precision: "time",
    priority: "normal",
    promise: null,
    people: [],
    needs_review: false,
    ...fields,
  };
}

/** Задача из сообщения обычным путём (§3.4); её строка. */
async function recorded(db: PGlite, fields: { [key: string]: Json }): Promise<TaskRow> {
  telegramMessageId += 1;
  const { rows: saved } = await db.query<{ id: string }>(
    "select id from public.record_message($1, $2, $3, $4)",
    [OWNER, OWNER, telegramMessageId, "завтра с 14 до 16 созвон по VoiceFin"],
  );
  const { rows } = await db.query<TaskRow>(
    `select * from public.record_understanding(
       message_id => $1, owner_telegram_id => $2, analysis => '{"kind": "task"}'::jsonb,
       ai_model => 'claude-opus-5', ai_input_tokens => 300, ai_output_tokens => 120,
       reply => 'ok', tasks => $3::jsonb, facts => '[]'::jsonb
     )`,
    [only(saved).id, OWNER, JSON.stringify([{ item: 1, task: task(fields), reminders: [] }])],
  );
  return only(rows);
}

async function changed(db: PGlite, id: string, changes: Json): Promise<TaskRow> {
  const { rows } = await db.query<TaskRow>(
    "select * from public.change_task($1, $2, $3::jsonb, '[]'::jsonb)",
    [OWNER, id, JSON.stringify(changes)],
  );
  return only(rows);
}

async function row(db: PGlite, id: string): Promise<TaskRow> {
  return only((await db.query<TaskRow>("select * from public.tasks where id = $1", [id])).rows);
}

test("длительность из разбора ложится в задачу, у дела без неё — пусто", () =>
  withDatabase(async (db) => {
    const meeting = await recorded(db, { duration: 120 });
    const call = await recorded(db, { title: "позвонить маме", duration: null });

    assert.equal(meeting.duration, 120);
    assert.equal(call.duration, null);
  }));

test("у срока без часа длительность не держится: её снимает триггер", () =>
  withDatabase(async (db) => {
    const onDay = await recorded(db, { due_precision: "day", duration: 60 });
    const undated = await recorded(db, { due_at: null, due_precision: null, duration: 60 });

    assert.equal(onDay.duration, null);
    assert.equal(undated.duration, null);
  }));

test("«созвон до 16» меняет длительность, перенос на другой час её не теряет", () =>
  withDatabase(async (db) => {
    await saveZone(db);
    const meeting = await recorded(db, { duration: 60 });

    const longer = await changed(db, meeting.id, { duration: 120 });
    const moved = await changed(db, meeting.id, { due_at: SATURDAY_THREE });

    assert.equal(longer.duration, 120);
    assert.equal(moved.duration, 120);
    assert.equal(moved.due_at?.toISOString(), "2030-10-05T10:00:00.000Z");
  }));

test("перенос встречи на день или часть дня снимает длительность", () =>
  withDatabase(async (db) => {
    await saveZone(db);
    const first = await recorded(db, { duration: 90 });
    const second = await recorded(db, { title: "встреча с Игорем", duration: 90 });

    const onDay = await changed(db, first.id, { due_date: "2030-10-07" });
    const evening = await changed(db, second.id, {
      due_at: "2030-10-07T18:00:00+05:00",
      due_precision: "evening",
    });

    assert.equal(onDay.duration, null);
    assert.equal(evening.duration, null);
  }));

test("правка снимает длительность ключом null и не берёт чушь", () =>
  withDatabase(async (db) => {
    await saveZone(db);
    const meeting = await recorded(db, { duration: 60 });

    const cleared = await changed(db, meeting.id, { duration: null });

    assert.equal(cleared.duration, null);
    for (const wrong of [0, 1441, 30.5, "60"] as Json[]) {
      await assert.rejects(changed(db, meeting.id, { duration: wrong }), /invalid duration/);
    }
    assert.equal((await row(db, meeting.id)).duration, null);
  }));

test("предел держит и база: больше суток не пишется", () =>
  withDatabase(async (db) => {
    await assert.rejects(recorded(db, { duration: 1441 }), /tasks_duration_check/);
  }));

test("приложение правит ту же колонку под своим правилом доступа", () =>
  withDatabase(async (db) => {
    await saveZone(db);
    const meeting = await recorded(db, { duration: 60 });

    const edited = await asRole(db, "authenticated", OWNER, () =>
      db.query<TaskRow>("select * from public.edit_task($1, $2::jsonb)", [
        meeting.id,
        JSON.stringify({ due_date: "2030-10-08" }),
      ]),
    );

    assert.equal(only(edited.rows).duration, null);
  }));

test("триггерную функцию напрямую не зовут ни anon, ни authenticated", () =>
  withDatabase(async (db) => {
    const { rows } = await db.query<{ anon: boolean; authenticated: boolean }>(
      `select has_function_privilege('anon', 'public.tasks_duration_fit()', 'execute') as anon,
              has_function_privilege('authenticated', 'public.tasks_duration_fit()', 'execute')
                as authenticated`,
    );
    assert.deepEqual(only(rows), { anon: false, authenticated: false });
  }));
