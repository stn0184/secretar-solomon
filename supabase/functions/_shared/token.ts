/**
 * Токен доступа к данным.
 *
 * Mini App ходит в базу напрямую, значит ограничивать её запросы должен сам
 * Postgres. Для этого запросу нужен JWT, подписанный секретом проекта: по нему
 * PostgREST даёт роль authenticated, а правила доступа (RLS) видят владельца
 * в клейме telegram_id и в sub.
 */

import { encoder, hmacSha256, toBase64Url } from "./crypto.ts";

/** Час: дальше Mini App просит новый токен, заново подтверждая initData. */
export const DEFAULT_TTL_SECONDS = 60 * 60;

/**
 * Пространство имён для uuid владельца (UUIDv5).
 *
 * Telegram-id — число, а sub в JWT и auth.uid() в Postgres — uuid. Один и тот
 * же Telegram-id всегда даёт один и тот же uuid, поэтому владелец своих данных
 * не теряет и отдельная таблица соответствия не нужна.
 */
export const OWNER_NAMESPACE = "f2fe3092-4b31-45c8-9d62-2b88709f8852";

export interface AccessToken {
  token: string;
  expiresIn: number;
  userId: string;
}

export interface TokenOptions {
  now?: Date;
  ttlSeconds?: number;
}

function namespaceBytes(uuid: string): Uint8Array {
  const hex = uuid.replaceAll("-", "");
  const bytes = new Uint8Array(16);
  for (let i = 0; i < bytes.length; i += 1) {
    bytes[i] = Number.parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  }
  return bytes;
}

function formatUuid(bytes: Uint8Array): string {
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  return [
    hex.slice(0, 8),
    hex.slice(8, 12),
    hex.slice(12, 16),
    hex.slice(16, 20),
    hex.slice(20, 32),
  ].join("-");
}

/** Постоянный uuid владельца по его Telegram-id (UUIDv5, RFC 4122). */
export async function uuidForTelegramId(telegramId: number): Promise<string> {
  const name = encoder.encode(String(telegramId));
  const input = new Uint8Array(16 + name.length);
  input.set(namespaceBytes(OWNER_NAMESPACE), 0);
  input.set(name, 16);

  const digest = new Uint8Array(await crypto.subtle.digest("SHA-1", input));
  const uuid = digest.slice(0, 16);
  uuid[6] = (uuid[6]! & 0x0f) | 0x50; // версия 5
  uuid[8] = (uuid[8]! & 0x3f) | 0x80; // вариант RFC 4122
  return formatUuid(uuid);
}

/** Подписать JWT алгоритмом HS256 — так проверяет токены сам Supabase. */
export async function signHs256(payload: Record<string, unknown>, secret: string): Promise<string> {
  const header = toBase64Url(encoder.encode(JSON.stringify({ alg: "HS256", typ: "JWT" })));
  const body = toBase64Url(encoder.encode(JSON.stringify(payload)));
  const signature = await hmacSha256(encoder.encode(secret), `${header}.${body}`);
  return `${header}.${body}.${toBase64Url(signature)}`;
}

/** Собрать токен для Mini App: роль, срок и владелец. */
export async function buildAccessToken(
  telegramId: number,
  jwtSecret: string,
  options: TokenOptions = {},
): Promise<AccessToken> {
  const { now = new Date(), ttlSeconds = DEFAULT_TTL_SECONDS } = options;
  const issuedAt = Math.floor(now.getTime() / 1000);
  const userId = await uuidForTelegramId(telegramId);

  const token = await signHs256(
    {
      sub: userId,
      aud: "authenticated",
      role: "authenticated",
      telegram_id: telegramId,
      iat: issuedAt,
      exp: issuedAt + ttlSeconds,
    },
    jwtSecret,
  );

  return { token, expiresIn: ttlSeconds, userId };
}
