/**
 * Свежая база для SQL-тестов: настоящий Postgres в WASM (PGlite), без сети.
 *
 * Функции базы — plpgsql, и ошибки в них видны только при исполнении:
 * подменённая база тестов бота их не ловит. Поэтому миграции из
 * `supabase/migrations/` применяются по порядку имён к пустой базе, как
 * `supabase db push` к живой, и функции зовутся по-настоящему.
 *
 * Базу с миграциями собираем один раз на процесс и снимаем с неё слепок;
 * каждый тест получает свою копию слепка — чистую и независимую от соседей.
 */
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";

import { PGlite } from "@electric-sql/pglite";

const MIGRATIONS = new URL("../migrations/", import.meta.url);

/**
 * То, что в Supabase есть до первой миграции: роли PostgREST, их гранты и
 * `auth.jwt()`. Функция читает клеймы так же, как Supabase, — из
 * `request.jwt.claims`.
 *
 * Гранты — как в проекте Supabase: все три роли видят схему `public`, и
 * всё, что в ней создают миграции, по умолчанию доступно всем трём.
 * Разделяют их RLS и `revoke` в самих миграциях, поэтому тест под ролью
 * `authenticated` или `anon` проверяет ровно то, что проверит живая база.
 * `service_role` обходит RLS, как ключ бота.
 */
const SUPABASE_STUBS = `
  create role anon nologin;
  create role authenticated nologin;
  create role service_role nologin bypassrls;
  create schema auth;
  create function auth.jwt() returns jsonb
  language sql stable
  as $$
    select coalesce(nullif(current_setting('request.jwt.claims', true), ''), '{}')::jsonb
  $$;
  grant usage on schema public, auth to anon, authenticated, service_role;
  alter default privileges in schema public
    grant all on tables to anon, authenticated, service_role;
  alter default privileges in schema public
    grant all on functions to anon, authenticated, service_role;
  alter default privileges in schema public
    grant all on sequences to anon, authenticated, service_role;
`;

/** Имена миграций в порядке применения — порядок имён, как у Supabase CLI. */
export function migrationNames(): string[] {
  return readdirSync(MIGRATIONS)
    .filter((name) => name.endsWith(".sql"))
    .sort();
}

async function applyMigrations(db: PGlite, names: string[]): Promise<void> {
  for (const name of names) {
    try {
      await db.exec(readFileSync(new URL(name, MIGRATIONS), "utf8"));
    } catch (error) {
      throw new Error(`migration ${name} failed: ${(error as Error).message}`, { cause: error });
    }
  }
}

async function migratedSnapshot(): Promise<File | Blob> {
  const db = new PGlite();
  try {
    await db.exec(SUPABASE_STUBS);
    await applyMigrations(db, migrationNames());
    return await db.dumpDataDir("none");
  } finally {
    await db.close();
  }
}

let snapshot: Promise<File | Blob> | undefined;

/** Пустая база со всеми миграциями. Закрывать обязательно: иначе процесс не выйдет. */
export async function freshDatabase(): Promise<PGlite> {
  snapshot ??= migratedSnapshot();
  const db = new PGlite({ loadDataDir: await snapshot });
  await db.waitReady;
  return db;
}

/** Тест на своей базе: открыть, прогнать, закрыть даже при падении. */
export async function withDatabase(body: (db: PGlite) => Promise<void>): Promise<void> {
  const db = await freshDatabase();
  try {
    await body(db);
  } finally {
    await db.close();
  }
}

/**
 * Тест того, что миграция делает со строками, записанными до неё: база
 * собирается миграциями строго раньше `name`, тест заводит в ней старые
 * строки и зовёт `migrate` — оставшиеся миграции, начиная с `name`,
 * применяются к ним, как `supabase db push` к живой базе.
 */
export async function withDatabaseBefore(
  name: string,
  body: (db: PGlite, migrate: () => Promise<void>) => Promise<void>,
): Promise<void> {
  const names = migrationNames();
  assert.ok(names.includes(name), `нет миграции ${name}`);
  const db = new PGlite();
  try {
    await db.exec(SUPABASE_STUBS);
    await applyMigrations(
      db,
      names.filter((each) => each < name),
    );
    await body(db, () =>
      applyMigrations(
        db,
        names.filter((each) => each >= name),
      ),
    );
  } finally {
    await db.close();
  }
}

/** Клеймы токена Mini App (§4.4): владелец — `telegram_id`; `null` — клейма нет. */
function claimsOf(owner: number | null): string {
  return JSON.stringify(owner === null ? { role: "authenticated" } : { role: "authenticated", telegram_id: owner });
}

/**
 * Вызов под ролью PostgREST, как из Mini App: `authenticated` с клеймом
 * владельца (или без него), либо `anon`. Роль и клеймы снимаются и при
 * падении — следующий запрос теста снова идёт от владельца базы.
 */
export async function asRole<T>(
  db: PGlite,
  role: "authenticated" | "anon",
  owner: number | null,
  body: () => Promise<T>,
): Promise<T> {
  await db.query("select set_config('request.jwt.claims', $1, false)", [
    role === "anon" ? "" : claimsOf(owner),
  ]);
  await db.exec(`set role ${role}`);
  try {
    return await body();
  } finally {
    await db.exec("reset role");
    await db.query("select set_config('request.jwt.claims', '', false)");
  }
}

/**
 * Строка `messages`, как её видят тесты: вид, текст, файл, разбор и то,
 * что модель прочитала со снимка (`photo_text`, §14.5).
 */
export interface MessageRow {
  id: string;
  kind: string;
  text: string;
  telegram_file_id: string | null;
  duration_seconds: number | null;
  photo_text: string | null;
  reply: string | null;
  analysis: unknown;
  task_id: string | null;
}

/** Сообщение по `id` — ровно одна строка, иначе тест падает. */
export async function messageRow(db: PGlite, id: string): Promise<MessageRow> {
  const { rows } = await db.query<MessageRow>(
    `select id, kind, text, telegram_file_id, duration_seconds, photo_text, reply,
            analysis, task_id
       from public.messages where id = $1`,
    [id],
  );
  assert.equal(rows.length, 1, `сообщение ${id}: ждали одну строку, пришло ${rows.length}`);
  return rows[0]!;
}
