/**
 * Доступ к данным: обмен initData на токен.
 *
 * Своего сервера нет, поэтому Mini App ходит в базу напрямую. Чтобы правила
 * доступа знали, кто пришёл, запросу нужен токен: его выдаёт Edge Function
 * `telegram-auth`, проверив подпись Telegram (`supabase/README.md`).
 */

export interface SupabaseEnv {
  url: string;
  anonKey: string;
}

export interface AccessToken {
  token: string;
  userId: string;
  expiresAt: number;
}

export type AccessState =
  | { kind: "loading" }
  | { kind: "granted"; userId: string }
  | { kind: "outside-telegram" }
  | { kind: "not-configured" }
  | { kind: "refused"; message: string };

/** Переменные сборки. Нет — значит приложение собрано без настроек. */
export function readSupabaseEnv(): SupabaseEnv | null {
  const url = import.meta.env.VITE_SUPABASE_URL;
  const anonKey = import.meta.env.VITE_SUPABASE_ANON_KEY;
  if (!url || !anonKey) {
    return null;
  }
  return { url, anonKey };
}

function parseError(payload: unknown): string {
  if (typeof payload === "object" && payload !== null) {
    const message = (payload as { error?: unknown }).error;
    if (typeof message === "string" && message) {
      return message;
    }
  }
  return "База отказала в доступе.";
}

/** Попросить токен у Edge Function. Отказ возвращается текстом, а не исключением. */
export async function requestAccess(
  env: SupabaseEnv,
  initData: string,
): Promise<{ ok: true; access: AccessToken } | { ok: false; message: string }> {
  let response: Response;
  try {
    response = await fetch(`${env.url}/functions/v1/telegram-auth`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        apikey: env.anonKey,
      },
      body: JSON.stringify({ initData }),
    });
  } catch {
    return { ok: false, message: "Не получилось связаться с базой. Проверьте сеть." };
  }

  const payload: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    return { ok: false, message: parseError(payload) };
  }

  const data = payload as { access_token?: unknown; user_id?: unknown; expires_in?: unknown };
  if (typeof data.access_token !== "string" || typeof data.user_id !== "string") {
    return { ok: false, message: "База ответила непонятно." };
  }

  const expiresIn = typeof data.expires_in === "number" ? data.expires_in : 0;
  return {
    ok: true,
    access: {
      token: data.access_token,
      userId: data.user_id,
      expiresAt: Date.now() + expiresIn * 1000,
    },
  };
}
