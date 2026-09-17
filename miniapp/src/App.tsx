import { useEffect, useRef, useState } from "react";

import { type AccessState, type AccessToken, readSupabaseEnv, requestAccess } from "./lib/session.ts";
import { createSupabaseClient } from "./lib/supabase.ts";
import { type Task, TASKS_SHOWN, loadActiveTasks } from "./lib/tasks.ts";
import { getWebApp, initTelegram } from "./lib/telegram.ts";

/** Состояния списка задач — те самые из `design.md` §2, кроме «штатно» и «мало». */
type TasksState =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "ready"; tasks: Task[]; more: boolean }
  | { kind: "failed"; message: string };

/** Токена ещё нет и не будет — список задач не запрашивается вовсе. */
function initialTasks(): TasksState {
  return readSupabaseEnv() && getWebApp()?.initData ? { kind: "loading" } : { kind: "idle" };
}

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

/** Что показать вместо списка, пока или если списка нет. */
function tasksBody(state: TasksState) {
  switch (state.kind) {
    case "idle":
      return null;
    case "loading":
      return <p className="subtitle">Загружаем задачи…</p>;
    case "failed":
      return <p className="subtitle">{state.message}</p>;
    case "ready":
      if (state.tasks.length === 0) {
        return <p className="subtitle">Задач пока нет. Напишите боту — и она появится здесь.</p>;
      }
      return (
        <>
          <div className="facts">
            {state.tasks.map((task) => (
              <div className="fact" key={task.id}>
                {task.title}
              </div>
            ))}
          </div>
          {state.more ? <p className="subtitle">Показаны первые {TASKS_SHOWN} задач.</p> : null}
        </>
      );
  }
}

/** Список задач. Экран без дизайна: настоящий — следующим этапом, по прототипу. */
function Tasks({ state }: { state: TasksState }) {
  // Без токена задачи не запрашивались: заголовок над пустотой не нужен,
  // причину показывает строка «Доступ к данным».
  if (state.kind === "idle") {
    return null;
  }
  return (
    <>
      <h2 className="subtitle">Активные задачи</h2>
      {tasksBody(state)}
    </>
  );
}

export default function App() {
  const [access, setAccess] = useState<AccessState>(initialAccess);
  const [tasks, setTasks] = useState<TasksState>(initialTasks);
  const inTelegram = getWebApp() !== null;

  // Токен живёт между перерисовками: клиент базы берёт его на каждый запрос.
  const tokenRef = useRef<AccessToken | null>(null);

  useEffect(() => {
    // Telegram — внешняя система: ей говорят, что приложение готово.
    initTelegram();

    const env = readSupabaseEnv();
    const initData = getWebApp()?.initData;
    if (!env || !initData) {
      return;
    }

    let cancelled = false;
    void requestAccess(env, initData).then(async (result) => {
      if (cancelled) {
        return;
      }
      if (!result.ok) {
        setAccess({ kind: "refused", message: result.message });
        setTasks({ kind: "failed", message: result.message });
        return;
      }
      tokenRef.current = result.access;
      setAccess({ kind: "granted", userId: result.access.userId });

      const loaded = await loadActiveTasks(createSupabaseClient(env, () => tokenRef.current));
      if (cancelled) {
        return;
      }
      setTasks(
        loaded.ok
          ? { kind: "ready", tasks: loaded.tasks, more: loaded.more }
          : { kind: "failed", message: loaded.message },
      );
    });

    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <main className="screen">
      <h1 className="title">Соломон</h1>
      <p className="subtitle">
        Личный секретарь. Пришлите боту текстом, что нужно сделать, — задача появится здесь.
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

      <Tasks state={tasks} />
    </main>
  );
}
