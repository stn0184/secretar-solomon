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
import { readdirSync, readFileSync } from "node:fs";

import { PGlite } from "@electric-sql/pglite";

const MIGRATIONS = new URL("../migrations/", import.meta.url);

/**
 * То, что в Supabase есть до первой миграции: роли PostgREST и `auth.jwt()`.
 * Функция читает клеймы так же, как Supabase, — из `request.jwt.claims`.
 */
const SUPABASE_STUBS = `
  create role anon nologin;
  create role authenticated nologin;
  create role service_role nologin;
  create schema auth;
  create function auth.jwt() returns jsonb
  language sql stable
  as $$
    select coalesce(nullif(current_setting('request.jwt.claims', true), ''), '{}')::jsonb
  $$;
`;

/** Имена миграций в порядке применения — порядок имён, как у Supabase CLI. */
export function migrationNames(): string[] {
  return readdirSync(MIGRATIONS)
    .filter((name) => name.endsWith(".sql"))
    .sort();
}

async function migratedSnapshot(): Promise<File | Blob> {
  const db = new PGlite();
  try {
    await db.exec(SUPABASE_STUBS);
    for (const name of migrationNames()) {
      try {
        await db.exec(readFileSync(new URL(name, MIGRATIONS), "utf8"));
      } catch (error) {
        throw new Error(`migration ${name} failed: ${(error as Error).message}`, { cause: error });
      }
    }
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
