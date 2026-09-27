/**
 * Доступ к данным: обмен initData на токен и его обновление.
 *
 * Своего сервера нет, поэтому Mini App ходит в базу напрямую. Чтобы правила
 * доступа знали, кто пришёл, запросу нужен токен: его выдаёт Edge Function
 * `telegram-auth`, проверив подпись Telegram (`supabase/README.md`).
 *
 * Токен живёт час, а `initData` функция принимает сутки: истёк токен —
 * тот же `initData` обменивается на новый. Отказ — текст, не исключение.
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

export type AccessResult = { ok: true; access: AccessToken } | { ok: false; message: string };

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
export async function requestAccess(env: SupabaseEnv, initData: string): Promise<AccessResult> {
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
    return { ok: false, message: "Не получилось связаться с базой. Проверьте связь." };
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

/** Запас: токен считается истёкшим чуть раньше срока, чтобы не упереться в границу. */
const EXPIRY_MARGIN_MS = 30 * 1000;

/**
 * Сессия: держит токен и умеет запросить новый.
 *
 * Одновременные обновления схлопываются в одно — два запроса, упавшие на
 * истёкший токен разом, не идут в функцию дважды.
 */
export interface Session {
  readonly env: SupabaseEnv;
  /** Текущий токен для клиента базы; пусто — ещё не запрашивался или не выдан. */
  token(): string | null;
  /** Токена нет или срок вышел (с запасом). */
  expired(): boolean;
  /** Обменять initData на новый токен. */
  refresh(): Promise<AccessResult>;
}

export function createSession(env: SupabaseEnv, initData: string): Session {
  let access: AccessToken | null = null;
  let inFlight: Promise<AccessResult> | null = null;

  return {
    env,
    token: () => access?.token ?? null,
    expired: () => access === null || Date.now() >= access.expiresAt - EXPIRY_MARGIN_MS,
    refresh() {
      if (inFlight) {
        return inFlight;
      }
      inFlight = requestAccess(env, initData).then((result) => {
        if (result.ok) {
          access = result.access;
        }
        inFlight = null;
        return result;
      });
      return inFlight;
    },
  };
}
