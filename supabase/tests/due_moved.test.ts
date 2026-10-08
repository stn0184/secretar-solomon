/**
 * Функции бота для правки из приложения: пояс владельца и строка «Перенёс».
 *
 * `save_owner_timezone` — зеркало `OWNER_TIMEZONE` в базе (§11.3);
 * `moved_tasks` и `clear_due_moved` — чтение и снятие отметки «срок
 * перенесён» в минутном цикле (§11.4). Все три зовёт только бот ключом
 * service-role; вызовы здесь идут от владельца базы, права — отдельным
 * тестом. Таблица настроек — под тем же правилом «owner only», что и данные.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import type { PGlite } from "@electric-sql/pglite";

import { asRole, withDatabase } from "./database.ts";

const OWNER = 777;
const STRANGER = 999;

/** Пятница, 4 октября 2030 года: 18:00 у владельца (UTC+5) — 13:00 UTC. */
const FRIDAY_DUE = "2030-10-04T13:00:00.000Z";

interface MovedRow {
  id: string;
  title: string;
  due_at: Date | null;
  due_precision: string | null;
  due_moved_at: Date;
  next_fire_at: Date | null;
}

function only<T>(rows: T[]): T {
  assert.equal(rows.length, 1, `ждали одну строку, пришло ${rows.length}`);
  return rows[0]!;
}

async function saveZone(db: PGlite, owner = OWNER, zone = "Asia/Yekaterinburg"): Promise<void> {
  await db.query("select public.save_owner_timezone($1, $2)", [owner, zone]);
}

/** Задача со сроком в пятницу; `edit_task` двигает её, как приложение. */
async function seedTask(db: PGlite, owner = OWNER, title = "отправить расчёт клиенту"): Promise<string> {
  const { rows } = await db.query<{ id: string }>(
    `insert into public.tasks (owner_telegram_id, title, due_at, due_precision)
     values ($1, $2, $3, 'day') returning id`,
    [owner, title, FRIDAY_DUE],
  );
  return only(rows).id;
}

async function move(db: PGlite, id: string, changes: object, owner = OWNER): Promise<void> {
  await asRole(db, "authenticated", owner, async () => {
    await db.query("select * from public.edit_task($1::uuid, $2::jsonb)", [id, JSON.stringify(changes)]);
  });
}

async function moved(db: PGlite, owner = OWNER): Promise<MovedRow[]> {
  const { rows } = await db.query<MovedRow>("select * from public.moved_tasks($1)", [owner]);
  return rows;
}

async function clear(db: PGlite, id: string, seen: Date, owner = OWNER): Promise<boolean> {
  const { rows } = await db.query<{ cleared: boolean }>(
    "select public.clear_due_moved($1, $2, $3) as cleared",
    [owner, id, seen],
  );
  return only(rows).cleared;
}

test("у каждой функции одна перегрузка, и зовёт её только service_role", async () => {
  await withDatabase(async (db) => {
    const signatures = [
      "public.save_owner_timezone(bigint, text)",
      "public.moved_tasks(bigint)",
      "public.clear_due_moved(bigint, uuid, timestamptz)",
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

test("пояс пишется при каждом запуске одной строкой; незнакомый пояс — отказ", async () => {
  await withDatabase(async (db) => {
    await saveZone(db, OWNER, "Europe/Moscow");
    await saveZone(db, OWNER, "Asia/Yekaterinburg");

    const { rows } = await db.query<{ timezone: string }>(
      "select timezone from public.owner_settings where owner_telegram_id = $1",
      [OWNER],
    );
    assert.equal(only(rows).timezone, "Asia/Yekaterinburg");

    await assert.rejects(saveZone(db, OWNER, "Mars/Olympus"), /time zone/);
    const after = await db.query<{ timezone: string }>(
      "select timezone from public.owner_settings where owner_telegram_id = $1",
      [OWNER],
    );
    assert.equal(only(after.rows).timezone, "Asia/Yekaterinburg");
  });
});

test("настройки владельца под правилом «owner only»: чужой строки не видно", async () => {
  await withDatabase(async (db) => {
    await saveZone(db);
    await saveZone(db, STRANGER, "Europe/Moscow");

    const seen = await asRole(db, "authenticated", OWNER, async () => {
      const { rows } = await db.query<{ owner_telegram_id: string | number }>(
        "select owner_telegram_id from public.owner_settings",
      );
      return rows.map((row) => Number(row.owner_telegram_id));
    });
    assert.deepEqual(seen, [OWNER]);

    await asRole(db, "anon", null, async () => {
      const { rows } = await db.query("select * from public.owner_settings");
      assert.deepEqual(rows, []);
    });
  });
});

test("перенесённая задача видна с ближайшим неотправленным напоминанием", async () => {
  await withDatabase(async (db) => {
    await saveZone(db);
    await saveZone(db, STRANGER);
    const id = await seedTask(db);
    const still = await seedTask(db, OWNER, "не трогали");
    const foreign = await seedTask(db, STRANGER);

    await move(db, id, { due_date: "2030-10-04" });
    assert.deepEqual(await moved(db), [], "тот же день — не перенос");

    await move(db, id, { due_date: "2030-10-05" });
    await move(db, foreign, { due_date: "2030-10-05" }, STRANGER);

    const row = only(await moved(db));
    assert.equal(row.id, id);
    assert.equal(row.title, "отправить расчёт клиенту");
    assert.equal(row.due_at?.toISOString(), "2030-10-05T13:00:00.000Z");
    assert.equal(row.due_precision, "day");
    assert.equal(row.next_fire_at?.toISOString(), "2030-10-05T04:00:00.000Z");
    assert.notEqual(row.id, still);
  });
});

test("срок снят — строка без ближайшего напоминания", async () => {
  await withDatabase(async (db) => {
    await saveZone(db);
    const id = await seedTask(db);

    await move(db, id, { due_at: null });

    const row = only(await moved(db));
    assert.equal(row.due_at, null);
    assert.equal(row.next_fire_at, null);
  });
});

test("закрытая и удалённая задача строки не дают", async () => {
  await withDatabase(async (db) => {
    await saveZone(db);
    const closed = await seedTask(db);
    const removed = await seedTask(db);
    await move(db, closed, { due_date: "2030-10-05" });
    await move(db, removed, { due_date: "2030-10-05" });

    await db.query("select public.mark_task_done($1, $2)", [OWNER, closed]);
    await db.query("delete from public.tasks where id = $1", [removed]);

    assert.deepEqual(await moved(db), []);
  });
});

test("отметка снимается, только если она та, что прочитана", async () => {
  await withDatabase(async (db) => {
    await saveZone(db);
    const id = await seedTask(db);
    await move(db, id, { due_date: "2030-10-05" });
    const first = only(await moved(db));

    // Правка между чтением и снятием: отметка сменилась.
    await move(db, id, { due_date: "2030-10-06" });
    assert.equal(await clear(db, id, first.due_moved_at), false);

    const second = only(await moved(db));
    assert.equal(second.due_at?.toISOString(), "2030-10-06T13:00:00.000Z");
    assert.equal(await clear(db, id, second.due_moved_at, STRANGER), false, "чужой владелец");
    assert.equal(await clear(db, id, second.due_moved_at), true);
    assert.deepEqual(await moved(db), []);
  });
});

test("строка «Напомню» берёт ближайшее из неотправленных", async () => {
  await withDatabase(async (db) => {
    await saveZone(db);
    const id = await seedTask(db);
    // Тот же момент, но с часом: перенос, «заранее» — за час до срока.
    await move(db, id, { due_at: "2030-10-04T18:00:00+05:00" });
    assert.equal(only(await moved(db)).next_fire_at?.toISOString(), "2030-10-04T12:00:00.000Z");

    // «За час» ушло — ближайшее «за 5 минут» до срока (этап 029, §6.1).
    await db.query("update public.reminders set sent_at = now() where task_id = $1 and stage = 'before'", [id]);
    assert.equal(only(await moved(db)).next_fire_at?.toISOString(), "2030-10-04T12:55:00.000Z");
  });
});
