/**
 * Оформление приложения на настоящем Postgres (этап 034): колонка
 * `owner_settings.theme` и её запись из Mini App функцией `save_theme`.
 *
 * Правила — `techspec/32-redesign.md` §32.1 и `techspec/03-schema.md` §3.8:
 * выбор — `auto` (как в Telegram, по умолчанию), `bank` или `quiet`; пишет
 * его приложение под своим правилом доступа, владелец — из клейма токена.
 * Пояс в той же строке приложение переписать не может.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import type { PGlite } from "@electric-sql/pglite";

import { asRole, withDatabase, withDatabaseBefore } from "./database.ts";

const OWNER = 777;
const STRANGER = 999;

interface SettingsRow {
  owner_telegram_id: string | number;
  timezone: string;
  theme: string;
}

function only<T>(rows: T[]): T {
  assert.equal(rows.length, 1, `ждали одну строку, пришло ${rows.length}`);
  return rows[0]!;
}

async function saveZone(db: PGlite, owner = OWNER, zone = "Asia/Yekaterinburg"): Promise<void> {
  await db.query("select public.save_owner_timezone($1, $2)", [owner, zone]);
}

async function settings(db: PGlite, owner = OWNER): Promise<SettingsRow> {
  const { rows } = await db.query<SettingsRow>(
    "select owner_telegram_id, timezone, theme from public.owner_settings where owner_telegram_id = $1",
    [owner],
  );
  return only(rows);
}

/** Выбор темы, как его шлёт приложение: роль `authenticated`, владелец клеймом. */
async function saveTheme(db: PGlite, theme: string, owner: number | null = OWNER): Promise<SettingsRow | null> {
  return asRole(db, "authenticated", owner, async () => {
    const { rows } = await db.query<SettingsRow | { owner_telegram_id: null }>(
      "select * from public.save_theme($1)",
      [theme],
    );
    const row = only(rows);
    return row.owner_telegram_id === null ? null : (row as SettingsRow);
  });
}

test("по умолчанию — как в Telegram; бот, записывая пояс, тему не трогает", () =>
  withDatabase(async (db) => {
    await saveZone(db);
    assert.equal((await settings(db)).theme, "auto");

    await saveTheme(db, "bank");
    await saveZone(db, OWNER, "Europe/Moscow");
    const row = await settings(db);
    assert.equal(row.theme, "bank");
    assert.equal(row.timezone, "Europe/Moscow");
  }));

test("строка, записанная до миграции, получает «как в Telegram»", () =>
  withDatabaseBefore("20261011100000_theme.sql", async (db, migrate) => {
    await saveZone(db);
    await migrate();
    assert.equal((await settings(db)).theme, "auto");
  }));

test("приложение сохраняет выбор и читает его своей строкой", () =>
  withDatabase(async (db) => {
    await saveZone(db);
    await saveZone(db, STRANGER, "Europe/Moscow");

    const saved = await saveTheme(db, "quiet");
    assert.equal(saved?.theme, "quiet");
    assert.equal(Number(saved?.owner_telegram_id), OWNER);
    assert.equal((await settings(db)).theme, "quiet");
    assert.equal((await settings(db, STRANGER)).theme, "auto");

    const seen = await asRole(db, "authenticated", OWNER, async () => {
      const { rows } = await db.query<{ theme: string }>("select theme from public.owner_settings");
      return rows.map((row) => row.theme);
    });
    assert.deepEqual(seen, ["quiet"]);

    await saveTheme(db, "auto");
    assert.equal((await settings(db)).theme, "auto");
  }));

test("незнакомая тема — отказ, прежний выбор остаётся", () =>
  withDatabase(async (db) => {
    await saveZone(db);
    await saveTheme(db, "bank");
    await assert.rejects(saveTheme(db, "glass"), /owner_settings_theme_check/);
    await assert.rejects(saveTheme(db, ""), /owner_settings_theme_check/);
    assert.equal((await settings(db)).theme, "bank");
  }));

test("строки владельца нет или токен без клейма — пустой ответ, ничего не записано", () =>
  withDatabase(async (db) => {
    await saveZone(db, STRANGER);
    assert.equal(await saveTheme(db, "bank"), null);
    assert.equal(await saveTheme(db, "bank", null), null);
    assert.equal((await settings(db, STRANGER)).theme, "auto");
    const { rows } = await db.query("select * from public.owner_settings where owner_telegram_id = $1", [OWNER]);
    assert.deepEqual(rows, []);
  }));

test("приложение правит только тему: пояс, чужую строку и новую строку — нет", () =>
  withDatabase(async (db) => {
    await saveZone(db);
    await saveZone(db, STRANGER, "Europe/Moscow");

    await asRole(db, "authenticated", OWNER, async () => {
      await assert.rejects(
        db.query("update public.owner_settings set timezone = 'Europe/Moscow'"),
        /permission denied/,
      );
      await assert.rejects(
        db.query("insert into public.owner_settings (owner_telegram_id, timezone) values ($1, 'UTC')", [
          OWNER + 1,
        ]),
        /permission denied/,
      );
      await assert.rejects(db.query("delete from public.owner_settings"), /permission denied/);
      // Чужая строка под правилом доступа не видна — обновлять нечего.
      const foreign = await db.query(
        "update public.owner_settings set theme = 'bank' where owner_telegram_id = $1 returning *",
        [STRANGER],
      );
      assert.deepEqual(foreign.rows, []);
    });

    assert.equal((await settings(db)).timezone, "Asia/Yekaterinburg");
    assert.equal((await settings(db, STRANGER)).theme, "auto");
  }));

test("save_theme зовёт только authenticated", () =>
  withDatabase(async (db) => {
    const { rows } = await db.query<{ anon: boolean; authenticated: boolean; service: boolean }>(
      `select has_function_privilege('anon', 'public.save_theme(text)', 'execute') as anon,
              has_function_privilege('authenticated', 'public.save_theme(text)', 'execute')
                as authenticated,
              has_function_privilege('service_role', 'public.save_theme(text)', 'execute')
                as service`,
    );
    assert.deepEqual(only(rows), { anon: false, authenticated: true, service: false });

    await saveZone(db);
    await asRole(db, "anon", null, async () => {
      await assert.rejects(db.query("select * from public.save_theme('bank')"), /permission denied/);
    });
    assert.equal((await settings(db)).theme, "auto");
  }));
