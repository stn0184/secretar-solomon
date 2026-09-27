import { useCallback, useEffect, useState } from "react";

import { type ListState, TaskList } from "./components/TaskList.tsx";
import { TaskCard } from "./components/TaskCard.tsx";
import { createSession, readSupabaseEnv } from "./lib/session.ts";
import { type Db, createDb } from "./lib/supabase.ts";
import { type Task, loadActiveTasks } from "./lib/tasks.ts";
import { closeApp, getInitData, initTelegram, showBackButton } from "./lib/telegram.ts";

/** Два экрана переключаются состоянием: роутера нет, Telegram открывает всегда корень. */
type Screen = { kind: "list" } | { kind: "card"; task: Task };

/**
 * Собрать доступ к базе. Нет переменных сборки или приложение открыто не из
 * Telegram — база недоступна, и список сразу говорит почему.
 */
function bootstrap(): { db: Db } | { error: string } {
  const env = readSupabaseEnv();
  if (!env) {
    return { error: "Приложение собрано без адреса и ключа Supabase. Проверьте сборку." };
  }
  const initData = getInitData();
  if (!initData) {
    return { error: "Откройте приложение из Telegram: вне мессенджера задачи не видны." };
  }
  return { db: createDb(createSession(env, initData)) };
}

export default function App() {
  const [boot] = useState(bootstrap);
  const [list, setList] = useState<ListState>(() =>
    "error" in boot ? { kind: "failed", message: boot.error } : { kind: "loading" },
  );
  const [screen, setScreen] = useState<Screen>({ kind: "list" });
  const [reloadKey, setReloadKey] = useState(0);
  const [now, setNow] = useState(() => new Date());

  // Telegram — внешняя система: ей говорят, что приложение готово.
  useEffect(() => initTelegram(), []);

  // Список читается при старте и по «Обновить»; «загрузка» ставится там,
  // где нажали, а не в эффекте.
  useEffect(() => {
    if ("error" in boot) {
      return;
    }
    let cancelled = false;
    void loadActiveTasks(boot.db).then((result) => {
      if (cancelled) {
        return;
      }
      setNow(new Date());
      setList(
        result.ok
          ? { kind: "ready", tasks: result.tasks, more: result.more }
          : { kind: "failed", message: result.message },
      );
    });
    return () => {
      cancelled = true;
    };
  }, [boot, reloadKey]);

  const back = useCallback(() => {
    setNow(new Date());
    setScreen({ kind: "list" });
  }, []);

  // На карточке видна кнопка «назад» Telegram; в списке её нет.
  useEffect(() => {
    window.scrollTo(0, 0);
    return screen.kind === "card" ? showBackButton(back) : undefined;
  }, [screen, back]);

  function open(task: Task) {
    setNow(new Date());
    setScreen({ kind: "card", task });
  }

  function reload() {
    if ("db" in boot) {
      setList({ kind: "loading" });
    }
    setReloadKey((k) => k + 1);
  }

  /** База подтвердила действие: задача уходит из списка, экран — назад. */
  function gone(taskId: string) {
    setList((current) =>
      current.kind === "ready"
        ? { ...current, tasks: current.tasks.filter((t) => t.id !== taskId) }
        : current,
    );
    back();
  }

  if (screen.kind === "card" && "db" in boot) {
    return (
      <TaskCard db={boot.db} task={screen.task} now={now} onBack={back} onGone={gone} />
    );
  }

  return (
    <TaskList
      state={list}
      now={now}
      onOpen={open}
      onReload={reload}
      onClose={closeApp}
    />
  );
}
