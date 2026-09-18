/**
 * telegram-auth — обмен initData Telegram на токен для правил доступа.
 *
 * Своего сервера у проекта нет, Mini App ходит в базу напрямую. Проверить,
 * что человек — это он, может только тот, кто знает токен бота; в браузер
 * токен не отдаётся, поэтому проверка живёт в Edge Function (бесплатный
 * тариф Supabase), а наружу уходит короткоживущий JWT.
 *
 * Секреты задаются один раз:
 *   supabase secrets set TELEGRAM_BOT_TOKEN=... OWNER_TELEGRAM_ID=... \
 *     JWT_SIGNING_SECRET=...
 */

import { InitDataError, verifyInitData } from "../_shared/init-data.ts";
import { buildAccessToken } from "../_shared/token.ts";

const CORS_HEADERS: Record<string, string> = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, apikey, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

function json(body: Record<string, unknown>, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...CORS_HEADERS, "Content-Type": "application/json" },
  });
}

async function readInitData(request: Request): Promise<string> {
  const body: unknown = await request.json();
  if (typeof body !== "object" || body === null) {
    return "";
  }
  const value = (body as { initData?: unknown }).initData;
  return typeof value === "string" ? value : "";
}

Deno.serve(async (request: Request): Promise<Response> => {
  if (request.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: CORS_HEADERS });
  }
  if (request.method !== "POST") {
    return json({ error: "Сюда приходят только запросы POST." }, 405);
  }

  const botToken = Deno.env.get("TELEGRAM_BOT_TOKEN");
  const jwtSecret = Deno.env.get("JWT_SIGNING_SECRET");
  const ownerId = Number(Deno.env.get("OWNER_TELEGRAM_ID"));
  if (!botToken || !jwtSecret || !Number.isFinite(ownerId)) {
    console.error("telegram-auth: не заданы секреты функции");
    return json({ error: "Помощник настроен не до конца. Загляните в секреты функции." }, 500);
  }

  let initData = "";
  try {
    initData = await readInitData(request);
  } catch {
    return json({ error: "Ожидается JSON с полем initData." }, 400);
  }

  try {
    const verified = await verifyInitData(initData, botToken);

    // Инвариант 2: помощник личный, токен получает только владелец.
    if (verified.telegramId !== ownerId) {
      return json({ error: "Это личный помощник. Он отвечает только своему владельцу." }, 403);
    }

    const { token, expiresIn, userId } = await buildAccessToken(verified.telegramId, jwtSecret);
    return json({
      access_token: token,
      token_type: "bearer",
      expires_in: expiresIn,
      user_id: userId,
      telegram_id: verified.telegramId,
    });
  } catch (error) {
    if (error instanceof InitDataError) {
      return json({ error: error.message }, 401);
    }
    console.error("telegram-auth: неожиданная ошибка", error);
    return json({ error: "Не получилось проверить данные Telegram." }, 500);
  }
});
