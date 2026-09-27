/**
 * Клиент Supabase для Mini App и один способ сходить в базу — `query`.
 *
 * В сборку вшиты только адрес проекта и анонимный ключ — оба публичны по
 * замыслу Supabase. Данные разделяет не ключ, а правила доступа в базе: они
 * смотрят на токен, который выдаёт Edge Function по подписи Telegram.
 *
 * `query` знает про срок токена: истёк — запрашивает новый и повторяет
 * запрос один раз; не вышло — отказ текстом. Тексты отказов свои, на
 * русском: английский хвост Supabase человеку не показывается.
 */

import { createClient, type SupabaseClient } from "@supabase/supabase-js";

import type { Session } from "./session.ts";

export interface Db {
  client: SupabaseClient;
  session: Session;
}

/**
 * Собрать клиент. Токен берётся на каждый запрос через `accessToken` —
 * так рекомендует Supabase для своих и сторонних JWT.
 */
export function createDb(session: Session): Db {
  const client = createClient(session.env.url, session.env.anonKey, {
    accessToken: () => Promise.resolve(session.token()),
  });
  return { client, session };
}

/** Почему не вышло: связь, доступ или отказ базы. */
export type DbReason = "network" | "access" | "refused";

export type DbFailure = { ok: false; reason: DbReason; message: string };
/** `data: null` — база ответила, но строки нет (rpc вернул null); это не отказ. */
export type DbResult<T> = { ok: true; data: T | null } | DbFailure;

/** Что говорится человеку по каждой причине — после префикса операции. */
export const REASON_TEXT: Record<DbReason, string> = {
  network: "Проверьте связь и обновите.",
  access: "Не получилось подтвердить, что это вы. Откройте приложение из Telegram заново.",
  refused: "База ответила отказом. Попробуйте ещё раз.",
};

/** Ответ PostgREST в том виде, в каком его отдаёт supabase-js. */
interface RestResponse<T> {
  data: T | null;
  error: { message: string; code?: string } | null;
  status?: number;
}

/** Просроченный или негодный токен: PostgREST отвечает 401 и кодом PGRST30x. */
function isAuthError(response: RestResponse<unknown>): boolean {
  return response.status === 401 || /^PGRST30[123]$/.test(response.error?.code ?? "");
}

function failure(reason: DbReason, message = REASON_TEXT[reason]): DbFailure {
  return { ok: false, reason, message };
}

/**
 * Сходить в базу с живым токеном.
 *
 * До запроса: токена нет или он истёк по местным часам — запросить новый.
 * После: база ответила 401 — запросить новый один раз и повторить.
 * Сеть упала — отказ «связь»; отказ базы — отказ «refused» без её текста.
 */
export async function query<T>(
  db: Db,
  call: (client: SupabaseClient) => PromiseLike<RestResponse<T>>,
): Promise<DbResult<T>> {
  if (db.session.expired()) {
    const refreshed = await db.session.refresh();
    if (!refreshed.ok) {
      return failure("access", refreshed.message);
    }
  }

  let response: RestResponse<T>;
  try {
    response = await call(db.client);
    if (response.error && isAuthError(response)) {
      const refreshed = await db.session.refresh();
      if (!refreshed.ok) {
        return failure("access", refreshed.message);
      }
      response = await call(db.client);
    }
  } catch {
    return failure("network");
  }

  if (response.error) {
    // Сеть не ответила: supabase-js не бросает, а отдаёт ошибку со статусом 0.
    if (response.status === 0) {
      return failure("network");
    }
    return failure(isAuthError(response) ? "access" : "refused");
  }
  return { ok: true, data: response.data };
}
