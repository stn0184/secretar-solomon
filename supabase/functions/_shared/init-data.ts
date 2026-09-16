/**
 * Проверка initData Telegram Mini App.
 *
 * Telegram подписывает initData ключом, выведенным из токена бота. Токен в
 * браузер не отдаётся, поэтому проверка живёт здесь, в Edge Function: она
 * единственная в связке знает токен и может подтвердить, что человек — это он.
 *
 * Алгоритм — core.telegram.org/bots/webapps:
 *   secret_key = HMAC_SHA256(key="WebAppData", msg=<токен бота>)
 *   hash       = HMAC_SHA256(key=secret_key,  msg=<строка проверки>)
 * Строка проверки — все полученные поля, кроме hash, в виде «ключ=значение»,
 * отсортированные по ключу и склеенные переводом строки.
 */

import { encoder, equalsConstantTime, hmacSha256, toHex } from "./crypto.ts";

/** Сутки: дольше подпись не принимается, Mini App просит новую. */
export const DEFAULT_MAX_AGE_SECONDS = 24 * 60 * 60;

export interface TelegramUser {
  id: number;
  first_name?: string;
  last_name?: string;
  username?: string;
  language_code?: string;
}

export interface VerifiedInitData {
  telegramId: number;
  user: TelegramUser;
  authDate: Date;
}

export interface VerifyOptions {
  maxAgeSeconds?: number;
  now?: Date;
}

/** Подпись не сошлась или данные не годятся: наружу уходит текст, не трассировка. */
export class InitDataError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "InitDataError";
  }
}

function checkString(params: URLSearchParams): string {
  return Array.from(params.entries())
    .filter(([key]) => key !== "hash")
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([key, value]) => `${key}=${value}`)
    .join("\n");
}

function parseUser(raw: string | null): TelegramUser {
  if (!raw) {
    throw new InitDataError("В данных Telegram нет пользователя.");
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    throw new InitDataError("Поле user в данных Telegram испорчено.");
  }
  const user = parsed as TelegramUser;
  if (typeof parsed !== "object" || parsed === null || typeof user.id !== "number") {
    throw new InitDataError("В поле user нет Telegram-id.");
  }
  return user;
}

function parseAuthDate(raw: string | null, maxAgeSeconds: number, now: Date): Date {
  const seconds = Number(raw);
  if (!raw || !Number.isFinite(seconds)) {
    throw new InitDataError("В данных Telegram нет времени подписи.");
  }
  const authDate = new Date(seconds * 1000);
  const ageSeconds = (now.getTime() - authDate.getTime()) / 1000;
  if (ageSeconds > maxAgeSeconds) {
    throw new InitDataError("Данные Telegram устарели — откройте приложение заново.");
  }
  return authDate;
}

/** Проверить подпись и вернуть, кто пришёл. Не сошлось — InitDataError. */
export async function verifyInitData(
  initData: string,
  botToken: string,
  options: VerifyOptions = {},
): Promise<VerifiedInitData> {
  const { maxAgeSeconds = DEFAULT_MAX_AGE_SECONDS, now = new Date() } = options;

  const params = new URLSearchParams(initData);
  const hash = params.get("hash");
  if (!hash) {
    throw new InitDataError("В данных Telegram нет подписи.");
  }

  const secretKey = await hmacSha256(encoder.encode("WebAppData"), botToken);
  const expected = toHex(await hmacSha256(secretKey, checkString(params)));
  if (!equalsConstantTime(expected, hash)) {
    throw new InitDataError("Подпись Telegram не сходится.");
  }

  const authDate = parseAuthDate(params.get("auth_date"), maxAgeSeconds, now);
  const user = parseUser(params.get("user"));

  return { telegramId: user.id, user, authDate };
}
