/**
 * Сфера дела из карточки приложения (этап 034): ключ `sphere_id` в `edit_task`
 * и ядре `change_task`.
 *
 * Правила — `techspec/32-redesign.md` §32.2 и `techspec/03-schema.md` §3.6:
 * сфера — живая сфера этого владельца или `null` (снять); чужая, убранная,
 * несуществующая или не uuid — отказ целиком. Смена сферы расписание и
 * отметку переноса не трогает. Сферы и люди выдуманные: VoiceFin, РЕЙВА.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import type { PGlite } from "@electric-sql/pglite";

import { asRole, withDatabase } from "./database.ts";

const OWNER = 777;
const STRANGER = 999;

type Json = null | boolean | number | string | Json[] | { [key: string]: Json };

interface TaskRow {
  id: string | null;
  title: string;
  sphere_id: string | null;
  needs_review: boolean;
  due_moved_at: Date | null;
}

/** Пятница, 4 октября 2030 года: 18:00 у владельца (UTC+5) — 13:00 UTC. */
const FRIDAY_DUE = "2030-10-04T13:00:00.000Z";

function only<T>(rows: T[]): T {
  assert.equal(rows.length, 1, `ждали одну строку, пришло ${rows.length}`);
  return rows[0]!;
}

async function saveZone(db: PGlite, owner = OWNER): Promise<void> {
  await db.query("select public.save_owner_timezone($1, $2)", [owner, "Asia/Yekaterinburg"]);
}

/** Сфера владельца, как её заводит бот (`sphere_id_of`, service-role). */
async function sphere(db: PGlite, name: string, owner = OWNER): Promise<string> {
  const { rows } = await db.query<{ id: string }>("select public.sphere_id_of($1, $2, true) as id", [
    owner,
    name,
  ]);
  return only(rows).id;
}

/** Дело, какое оставил бот: срок в пятницу, сфера по желанию, пометка «Перепроверьте». */
async function seedTask(db: PGlite, sphereId: string | null = null, owner = OWNER): Promise<string> {
  const { rows } = await db.query<{ id: string }>(
    `insert into public.tasks (owner_telegram_id, title, due_at, due_precision, needs_review, sphere_id)
     values ($1, 'показ бухгалтерам', $2, 'day', true, $3) returning id`,
    [owner, FRIDAY_DUE, sphereId],
  );
  return only(rows).id;
}

async function seedReminder(db: PGlite, taskId: string): Promise<void> {
  await db.query(
    `insert into public.reminders (owner_telegram_id, task_id, stage, fire_at)
     values ($1, $2, 'due', $3)`,
    [OWNER, taskId, FRIDAY_DUE],
  );
}

/** Вызов, как из приложения. Пустой ответ базы — `null`. */
async function edit(db: PGlite, taskId: string, changes: Json): Promise<TaskRow | null> {
  return asRole(db, "authenticated", OWNER, async () => {
    const { rows } = await db.query<TaskRow>("select * from public.edit_task($1::uuid, $2::jsonb)", [
      taskId,
      JSON.stringify(changes),
    ]);
    const row = only(rows);
    return row.id === null ? null : row;
  });
}

async function taskOf(db: PGlite, id: string): Promise<TaskRow> {
  return only((await db.query<TaskRow>("select * from public.tasks where id = $1", [id])).rows);
}

test("своя живая сфера ставится и меняется; ответ — строка из базы", () =>
  withDatabase(async (db) => {
    await saveZone(db);
    const voicefin = await sphere(db, "VoiceFin");
    const reiva = await sphere(db, "РЕЙВА");
    const id = await seedTask(db);

    const saved = await edit(db, id, { sphere_id: voicefin });
    assert.equal(saved?.sphere_id, voicefin);
    assert.equal(saved?.needs_review, false);

    await edit(db, id, { sphere_id: reiva });
    assert.equal((await taskOf(db, id)).sphere_id, reiva);
  }));

test("null снимает сферу; вместе с сутью — одной правкой", () =>
  withDatabase(async (db) => {
    await saveZone(db);
    const id = await seedTask(db, await sphere(db, "VoiceFin"));

    const saved = await edit(db, id, { sphere_id: null, title: "показ подписки бухгалтерам" });
    assert.equal(saved?.sphere_id, null);
    assert.equal(saved?.title, "показ подписки бухгалтерам");
  }));

test("смена сферы расписание и отметку переноса не трогает", () =>
  withDatabase(async (db) => {
    await saveZone(db);
    const id = await seedTask(db);
    await seedReminder(db, id);
    const before = await db.query<{ id: string }>("select id from public.reminders where task_id = $1", [id]);

    await edit(db, id, { sphere_id: await sphere(db, "VoiceFin") });

    const after = await db.query<{ id: string }>("select id from public.reminders where task_id = $1", [id]);
    assert.deepEqual(after.rows, before.rows);
    assert.equal((await taskOf(db, id)).due_moved_at, null);
  }));

test("чужая, убранная и несуществующая сфера — отказ целиком, дело не тронуто", () =>
  withDatabase(async (db) => {
    await saveZone(db);
    await saveZone(db, STRANGER);
    const own = await sphere(db, "VoiceFin");
    const foreign = await sphere(db, "РЕЙВА", STRANGER);
    const dropped = await sphere(db, "Спорт");
    await db.query("update public.spheres set removed_at = now() where id = $1", [dropped]);
    const id = await seedTask(db, own);

    for (const wrong of [foreign, dropped, "00000000-0000-4000-8000-000000000000"]) {
      await assert.rejects(edit(db, id, { sphere_id: wrong, title: "другое" }), /change_task: unknown sphere/);
    }
    const row = await taskOf(db, id);
    assert.equal(row.sphere_id, own);
    assert.equal(row.title, "показ бухгалтерам");
    assert.equal(row.needs_review, true);
  }));

test("не uuid и не строка — отказ", () =>
  withDatabase(async (db) => {
    await saveZone(db);
    const id = await seedTask(db);
    for (const wrong of ["VoiceFin", 5, true, { id: "x" }] as Json[]) {
      await assert.rejects(edit(db, id, { sphere_id: wrong }), /change_task: invalid sphere_id/);
    }
    assert.equal((await taskOf(db, id)).sphere_id, null);
  }));

test("ядро правки принимает сферу и от бота — с явным владельцем", () =>
  withDatabase(async (db) => {
    await saveZone(db);
    await saveZone(db, STRANGER);
    const own = await sphere(db, "VoiceFin");
    const foreign = await sphere(db, "РЕЙВА", STRANGER);
    const id = await seedTask(db);

    const { rows } = await db.query<TaskRow>(
      "select * from public.change_task($1, $2, $3::jsonb, '[]'::jsonb)",
      [OWNER, id, JSON.stringify({ sphere_id: own })],
    );
    assert.equal(only(rows).sphere_id, own);

    await assert.rejects(
      db.query("select * from public.change_task($1, $2, $3::jsonb, '[]'::jsonb)", [
        OWNER,
        id,
        JSON.stringify({ sphere_id: foreign }),
      ]),
      /change_task: unknown sphere/,
    );
  }));
