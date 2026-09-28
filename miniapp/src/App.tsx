import { useCallback, useEffect, useRef, useState } from "react";

import { type FactsState, FactList } from "./components/FactList.tsx";
import { type ListState, TaskList } from "./components/TaskList.tsx";
import { TaskCard } from "./components/TaskCard.tsx";
import { type Tab, Tabs } from "./components/Tabs.tsx";
import { type Fact, confirmFact, loadFacts, removeFact } from "./lib/facts.ts";
import { createSession, readSupabaseEnv } from "./lib/session.ts";
import { type ActionResult, type Db, createDb } from "./lib/supabase.ts";
import { type Task, loadActiveTasks } from "./lib/tasks.ts";
import {
  closeApp,
  confirmDiscard,
  getInitData,
  initTelegram,
  showBackButton,
} from "./lib/telegram.ts";

/**
 * Собрать доступ к базе. Нет переменных сборки или приложение открыто не из
 * Telegram — база недоступна, и экран сразу говорит почему.
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

/**
 * Роутера нет: два корневых экрана переключаются вкладкой, карточка задачи
 * открывается поверх списка состоянием, форма правки — на месте карточки.
 * Кнопка «назад» Telegram — в карточке и в форме; на корневых экранах
 * внизу вкладки.
 */
export default function App() {
  const [boot] = useState(bootstrap);
  const [tab, setTab] = useState<Tab>("tasks");
  const [card, setCard] = useState<Task | null>(null);
  const [editing, setEditing] = useState(false);
  // Есть ли в форме несохранённое — сообщает сама форма; читается только в «назад».
  const dirty = useRef(false);
  const [list, setList] = useState<ListState>(() =>
    "error" in boot ? { kind: "failed", message: boot.error } : { kind: "loading" },
  );
  const [facts, setFacts] = useState<FactsState>(() =>
    "error" in boot ? { kind: "failed", message: boot.error } : { kind: "idle" },
  );
  const [reloadKey, setReloadKey] = useState(0);
  const [factsKey, setFactsKey] = useState(0);
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

  // Память читается при первом открытии вкладки и по «Обновить».
  useEffect(() => {
    if ("error" in boot || factsKey === 0) {
      return;
    }
    let cancelled = false;
    void loadFacts(boot.db).then((result) => {
      if (cancelled) {
        return;
      }
      setNow(new Date());
      setFacts(
        result.ok
          ? { kind: "ready", facts: result.facts }
          : { kind: "failed", message: result.message },
      );
    });
    return () => {
      cancelled = true;
    };
  }, [boot, factsKey]);

  const closeEdit = useCallback(() => {
    dirty.current = false;
    setNow(new Date());
    setEditing(false);
  }, []);

  /**
   * «Назад» — одно на три входа: кнопка Telegram, своя «‹» и «Отмена».
   * Из формы с несохранённым — сначала «Бросить правку?»; без него —
   * сразу в карточку. Из карточки — в список.
   */
  const back = useCallback(() => {
    if (editing) {
      if (dirty.current) {
        void confirmDiscard().then((discard) => {
          if (discard) {
            closeEdit();
          }
        });
      } else {
        closeEdit();
      }
      return;
    }
    setNow(new Date());
    setCard(null);
  }, [editing, closeEdit]);

  const reportDirty = useCallback((value: boolean) => {
    dirty.current = value;
  }, []);

  // В карточке и форме видна кнопка «назад» Telegram; на корневых экранах её нет.
  useEffect(() => {
    window.scrollTo(0, 0);
    return card !== null ? showBackButton(back) : undefined;
  }, [card, tab, editing, back]);

  function open(task: Task) {
    setNow(new Date());
    setEditing(false);
    setCard(task);
  }

  function edit() {
    dirty.current = false;
    setNow(new Date());
    setEditing(true);
  }

  /**
   * База записала правку: в списке и карточке — её строка, а не черновик.
   * Карточку успели закрыть, пока шёл запрос, — она не открывается снова.
   */
  function saved(task: Task) {
    setList((current) =>
      current.kind === "ready"
        ? { ...current, tasks: current.tasks.map((t) => (t.id === task.id ? task : t)) }
        : current,
    );
    setCard((current) => (current?.id === task.id ? task : current));
    closeEdit();
  }

  function switchTab(next: Tab) {
    setNow(new Date());
    setTab(next);
    if (next === "me" && facts.kind === "idle") {
      setFacts({ kind: "loading" });
      setFactsKey((k) => k + 1);
    }
  }

  function reload() {
    if ("db" in boot) {
      setList({ kind: "loading" });
    }
    setReloadKey((k) => k + 1);
  }

  function reloadFacts() {
    if ("db" in boot) {
      setFacts({ kind: "loading" });
    }
    setFactsKey((k) => k + 1);
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

  /** «Подтвердить»: после ответа базы запись становится фактом и теряет метку. */
  async function confirm(fact: Fact): Promise<ActionResult> {
    if ("error" in boot) {
      return { ok: false, message: boot.error };
    }
    const result = await confirmFact(boot.db, fact.id);
    if (result.ok) {
      setFacts((current) =>
        current.kind === "ready"
          ? {
              ...current,
              facts: current.facts.map((f) => (f.id === fact.id ? { ...f, status: "fact" } : f)),
            }
          : current,
      );
    }
    return result;
  }

  /** «Удалить»: после ответа базы запись исчезает; отказ — остаётся на месте. */
  async function remove(fact: Fact): Promise<ActionResult> {
    if ("error" in boot) {
      return { ok: false, message: boot.error };
    }
    const result = await removeFact(boot.db, fact.id);
    if (result.ok) {
      setFacts((current) =>
        current.kind === "ready"
          ? { ...current, facts: current.facts.filter((f) => f.id !== fact.id) }
          : current,
      );
    }
    return result;
  }

  if (card !== null && "db" in boot) {
    return (
      <TaskCard
        db={boot.db}
        task={card}
        now={now}
        editing={editing}
        onBack={back}
        onGone={gone}
        onEdit={edit}
        onSaved={saved}
        onDirty={reportDirty}
      />
    );
  }

  return (
    <>
      {tab === "tasks" ? (
        <TaskList state={list} now={now} onOpen={open} onReload={reload} onClose={closeApp} />
      ) : (
        <FactList
          state={facts}
          now={now}
          onReload={reloadFacts}
          onClose={closeApp}
          onConfirm={confirm}
          onRemove={remove}
        />
      )}
      <Tabs active={tab} onChange={switchTab} />
    </>
  );
}
