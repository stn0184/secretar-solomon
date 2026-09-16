/**
 * Криптографические мелочи поверх Web Crypto.
 *
 * Только стандартные API: тот же код работает и в Deno (Edge Function),
 * и в Node (тесты) — без зависимостей и без второй реализации.
 */

export const encoder = new TextEncoder();

/** HMAC-SHA-256: ключ произвольными байтами, сообщение — строкой. */
export async function hmacSha256(key: Uint8Array, message: string): Promise<Uint8Array> {
  const cryptoKey = await crypto.subtle.importKey(
    "raw",
    key,
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const signature = await crypto.subtle.sign("HMAC", cryptoKey, encoder.encode(message));
  return new Uint8Array(signature);
}

/** Байты — в шестнадцатеричную строку. */
export function toHex(bytes: Uint8Array): string {
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

/** Байты — в base64url без выравнивающих знаков: так требует JWT. */
export function toBase64Url(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replaceAll("=", "");
}

/**
 * Сравнение за постоянное время.
 *
 * Обычное сравнение строк выходит на первом несовпавшем знаке, и по времени
 * ответа подпись можно подбирать посимвольно.
 */
export function equalsConstantTime(left: string, right: string): boolean {
  if (left.length !== right.length) {
    return false;
  }
  let diff = 0;
  for (let i = 0; i < left.length; i += 1) {
    diff |= left.charCodeAt(i) ^ right.charCodeAt(i);
  }
  return diff === 0;
}
