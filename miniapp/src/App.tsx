import { useEffect, useRef, useState } from "react";
import type { SupabaseClient } from "@supabase/supabase-js";

import { type AccessState, type AccessToken, readSupabaseEnv, requestAccess } from "./lib/session.ts";
import { createSupabaseClient } from "./lib/supabase.ts";
import { getWebApp, initTelegram } from "./lib/telegram.ts";

/** Что известно про доступ ещё до первого запроса. */
function initialAccess(): AccessState {
  if (!readSupabaseEnv()) {
    return { kind: "not-configured" };
  }
  if (!getWebApp()?.initData) {
    return { kind: "outside-telegram" };
  }
  return { kind: "loading" };
}

function accessLine(state: AccessState): string {
  switch (state.kind) {
    case "loading":
      return "проверяем подпись Telegram…";
    case "granted":
      return "есть: подпись Telegram проверена";
    case "outside-telegram":
      return "нет: приложение открыто не из Telegram";
    case "not-configured":
      return "нет: в сборке не заданы VITE_SUPABASE_URL и VITE_SUPABASE_ANON_KEY";
    case "refused":
      return state.message;
  }
}

export default function App() {
  const [access, setAccess] = useState<AccessState>(initialAccess);
  const inTelegram = getWebApp() !== null;

  // Токен и клиент живут между перерисовками: запросы к данным появятся
  // следующим этапом, и им нужен уже настроенный клиент.
  const tokenRef = useRef<AccessToken | null>(null);
  const clientRef = useRef<SupabaseClient | null>(null);

  useEffect(() => {
    // Telegram — внешняя система: ей говорят, что приложение готово.
    initTelegram();

    const env = readSupabaseEnv();
    const initData = getWebApp()?.initData;
    if (!env || !initData) {
      return;
    }

    let cancelled = false;
    void requestAccess(env, initData).then((result) => {
      if (cancelled) {
        return;
      }
      if (!result.ok) {
        setAccess({ kind: "refused", message: result.message });
        return;
      }
      tokenRef.current = result.access;
      clientRef.current = createSupabaseClient(env, () => tokenRef.current);
      setAccess({ kind: "granted", userId: result.access.userId });
    });

    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <main className="screen">
      <h1 className="title">Соломон</h1>
      <p className="subtitle">
        Личный секретарь. Пока это скелет: приём поручений, задачи и напоминания появятся
        следующими шагами.
      </p>

      <dl className="facts">
        <div className="fact">
          <dt>Версия</dt>
          <dd>{__APP_VERSION__}</dd>
        </div>
        <div className="fact">
          <dt>Telegram</dt>
          <dd>{inTelegram ? "приложение открыто внутри Telegram" : "открыто вне Telegram"}</dd>
        </div>
        <div className="fact">
          <dt>Доступ к данным</dt>
          <dd>{accessLine(access)}</dd>
        </div>
      </dl>
    </main>
  );
}
