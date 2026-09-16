/**
 * Клиент Supabase для Mini App.
 *
 * В сборку вшиты только адрес проекта и анонимный ключ — оба публичны по
 * замыслу Supabase. Данные разделяет не ключ, а правила доступа в базе: они
 * смотрят на токен, который выдаёт Edge Function по подписи Telegram.
 */

import { createClient, type SupabaseClient } from "@supabase/supabase-js";

import type { AccessToken, SupabaseEnv } from "./session.ts";

/**
 * Собрать клиент. Токен берётся на каждый запрос через `accessToken` —
 * так рекомендует Supabase для своих и сторонних JWT.
 */
export function createSupabaseClient(
  env: SupabaseEnv,
  currentAccess: () => AccessToken | null,
): SupabaseClient {
  return createClient(env.url, env.anonKey, {
    accessToken: () => Promise.resolve(currentAccess()?.token ?? null),
  });
}
