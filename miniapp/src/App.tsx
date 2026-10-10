import { useCallback, useEffect, useRef, useState } from "react";

import { Backdrop } from "./components/Backdrop.tsx";
import { type FactsState, FactList } from "./components/FactList.tsx";
import { type SpheresState, SpheresTab } from "./components/SpheresTab.tsx";
import { type ListState, type ListView, TaskList } from "./components/TaskList.tsx";
import { TaskCard } from "./components/TaskCard.tsx";
import { ThemeSettings } from "./components/ThemeSettings.tsx";
import { type Tab, Tabs } from "./components/Tabs.tsx";
import { type TodayState, TodayTab } from "./components/TodayTab.tsx";
import { WeekTab } from "./components/WeekTab.tsx";
import { type Fact, aboutFacts, confirmFact, loadFacts, removeFact } from "./lib/facts.ts";
import { loadToday } from "./lib/hours.ts";
import { createSession, readSupabaseEnv } from "./lib/session.ts";
import { loadSpheres, sphereOf } from "./lib/spheres.ts";
import { type ActionResult, type Db, createDb } from "./lib/supabase.ts";
import { type Task, loadActiveTasks } from "./lib/tasks.ts";
import {
  type ColorScheme,
  closeApp,
  confirmDiscard,
  getColorScheme,
  getInitData,
  initTelegram,
  onColorScheme,
  showBackButton,
  showSettingsButton,
} from "./lib/telegram.ts";
import {
  type ThemeChoice,
  applySkin,
  cacheChoice,
  loadTheme,
  readCachedChoice,
  saveTheme,
  skinOf,
} from "./lib/theme.ts";

/**
 * Собрать доступ к базе. Нет переменных сборки или приложение открыто не из
 * Telegram — база недоступна, и экран сразу говорит почему.
 */
/** Часы старше минуты перечитываются при возврате на вкладку: «сейчас» ушло вперёд. */
const HOURS_FRESH_MS = 60 * 1000;

function bootstrap(): { db: Db } | { error: string } {
  const env = readSupabaseEnv();
  if (!env) {
    return { error: "Приложение собрано без адреса и ключа Supabase. Проверьте сборку." };
  }
  const initData = getInitData();
  if (!initData) {
    return { error: "Откройте приложение из Telegram: вне мессенджера дела не видны." };
  }
  return { db: createDb(createSession(env, initData)) };
}

/**
 * Роутера нет: четыре корневых экрана переключаются вкладкой, открывается
 * «Сегодня» (`techspec/32-redesign.md` §32.2). Карточка дела открывается
 * поверх «Дел» состоянием, форма правки — на месте карточки; «О себе» —
 * поверх «Сфер» из карточки «Без сферы и о себе». Кнопка «назад» Telegram —
 * на этих экранах; на корневых внизу вкладки.
 */
export default function App() {
  const [boot] = useState(bootstrap);
  const [tab, setTab] = useState<Tab>("today");
  const [card, setCard] = useState<Task | null>(null);
  // «О себе» открыт поверх «Сфер».
  const [about, setAbout] = useState(false);
  const [editing, setEditing] = useState(false);
  // Есть ли в форме несохранённое — сообщает сама форма; читается только в «назад».
  const dirty = useRef(false);
  const [list, setList] = useState<ListState>(() =>
    "error" in boot ? { kind: "failed", message: boot.error } : { kind: "loading" },
  );
  const [facts, setFacts] = useState<FactsState>(() =>
    "error" in boot ? { kind: "failed", message: boot.error } : { kind: "idle" },
  );
  // «Дела» по сроку или по сферам — на время открытого приложения.
  const [view, setView] = useState<ListView>("due");
  const [reloadKey, setReloadKey] = useState(0);
  const [factsKey, setFactsKey] = useState(0);
  // Повторяющаяся задача, которую только что отметили: строка «✓ Сделано»
  // под ней держится до следующего чтения списка.
  const [justDone, setJustDone] = useState<string | null>(null);
  const [now, setNow] = useState(() => new Date());
  // «Сегодня»: часы и отрезки дня и дела дня — при старте, по «Обновить» и
  // при возврате на вкладку, если прочитано больше минуты назад. Перечитывание
  // идёт поверх прочитанного: цифры едут от прежнего значения к новому.
  const [today, setToday] = useState<TodayState>(() =>
    "error" in boot ? { kind: "failed", message: boot.error } : { kind: "loading" },
  );
  const [todayKey, setTodayKey] = useState(0);
  const todayAt = useRef(0);
  // Оформление: выбор владельца и схема Telegram; тема на экране — из обоих.
  const [themeChoice, setThemeChoice] = useState<ThemeChoice>(readCachedChoice);
  const [scheme, setScheme] = useState<ColorScheme>(getColorScheme);
  // «Оформление» открыто поверх текущего экрана; идёт ли запись выбора и отказ.
  const [settings, setSettings] = useState(false);
  const [themeBusy, setThemeBusy] = useState(false);
  const [themeError, setThemeError] = useState<string | null>(null);

  // Telegram — внешняя система: ей говорят, что приложение готово.
  useEffect(() => initTelegram(), []);

  // Вход в «Оформление» — пункт «Настройки» в меню ⋯ Telegram (`design.md` §1).
  useEffect(
    () =>
      showSettingsButton(() => {
        setThemeError(null);
        setSettings(true);
      }),
    [],
  );

  // Схема Telegram сменилась (ночной режим) — «Как в Telegram» следует за ней сразу.
  useEffect(() => onColorScheme(setScheme), []);

  useEffect(() => {
    applySkin(skinOf(themeChoice, scheme));
  }, [themeChoice, scheme]);

  // Выбор из базы — правда; копия на устройстве только для первого кадра.
  // Не прочиталось — остаётся копия: оформление не повод для ошибки на экране.
  useEffect(() => {
    if ("error" in boot) {
      return;
    }
    let cancelled = false;
    void loadTheme(boot.db).then((result) => {
      if (!cancelled && result.ok) {
        setThemeChoice(result.choice);
        cacheChoice(result.choice);
      }
    });
    return () => {
      cancelled = true;
    };
  }, [boot]);

  // Дела и сферы читаются вместе при старте и по «Обновить»: строке дела
  // нужны название и цвет сферы. «Загрузка» ставится там, где нажали.
  useEffect(() => {
    if ("error" in boot) {
      return;
    }
    let cancelled = false;
    void Promise.all([loadActiveTasks(boot.db), loadSpheres(boot.db)]).then(([tasks, spheres]) => {
      if (cancelled) {
        return;
      }
      setNow(new Date());
      setJustDone(null);
      if (!tasks.ok) {
        setList({ kind: "failed", message: tasks.message });
      } else if (!spheres.ok) {
        setList({ kind: "failed", message: spheres.message });
      } else {
        setList({ kind: "ready", tasks: tasks.tasks, more: tasks.more, spheres: spheres.spheres });
      }
    });
    return () => {
      cancelled = true;
    };
  }, [boot, reloadKey]);

  useEffect(() => {
    if ("error" in boot) {
      return;
    }
    let cancelled = false;
    void loadToday(boot.db, new Date()).then((result) => {
      if (cancelled) {
        return;
      }
      todayAt.current = Date.now();
      setNow(new Date());
      setToday(result.ok ? { kind: "ready", today: result.today } : { kind: "failed", message: result.message });
    });
    return () => {
      cancelled = true;
    };
  }, [boot, todayKey]);

  // Память читается при первом открытии «Сфер» или «О себе» и по «Обновить».
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
   * Из «Оформления» — к экрану под ним. Из формы с несохранённым — сначала
   * «Бросить правку?»; без него — сразу в карточку. Из карточки — в список,
   * из «О себе» — в «Сферы».
   */
  const back = useCallback(() => {
    if (settings) {
      setSettings(false);
      return;
    }
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
    if (card !== null) {
      setCard(null);
    } else {
      setAbout(false);
    }
  }, [settings, editing, card, closeEdit]);

  const reportDirty = useCallback((value: boolean) => {
    dirty.current = value;
  }, []);

  // В карточке, форме, «О себе» и «Оформлении» видна кнопка «назад» Telegram;
  // на корневых экранах её нет.
  const nested = card !== null || about || settings;
  useEffect(() => {
    window.scrollTo(0, 0);
    return nested ? showBackButton(back) : undefined;
  }, [nested, tab, editing, settings, back]);

  /**
   * Выбор в «Оформлении»: вид меняется сразу, а сохранённым выбор считается,
   * когда его записала база (инвариант 4). Отказ — текстом, и выбор
   * возвращается к прежнему: иначе после перезапуска вид сменился бы сам.
   */
  async function chooseTheme(choice: ThemeChoice) {
    if ("error" in boot || choice === themeChoice) {
      return;
    }
    const previous = themeChoice;
    setThemeChoice(choice);
    setThemeError(null);
    setThemeBusy(true);
    const result = await saveTheme(boot.db, choice);
    setThemeBusy(false);
    if (result.ok) {
      setThemeChoice(result.choice);
      cacheChoice(result.choice);
    } else {
      setThemeChoice(previous);
      setThemeError(result.message);
    }
  }

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

  /** Дело изменилось в карточке — часы и нить перечитываются, если уже прочитаны. */
  function hoursChanged() {
    if (today.kind === "ready") {
      setTodayKey((k) => k + 1);
    }
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
    hoursChanged();
    closeEdit();
  }

  /** Память читается при первом открытии «Сфер»: карточке «Без сферы и о себе» нужен счёт. */
  function loadFactsOnce() {
    if (facts.kind === "idle") {
      setFacts({ kind: "loading" });
      setFactsKey((k) => k + 1);
    }
  }

  function switchTab(next: Tab) {
    setNow(new Date());
    setTab(next);
    if (next === "spheres") {
      loadFactsOnce();
    }
    if (next === "today" && today.kind === "ready" && Date.now() - todayAt.current > HOURS_FRESH_MS) {
      setTodayKey((k) => k + 1);
    }
  }

  function reloadToday() {
    if ("db" in boot) {
      setToday({ kind: "loading" });
    }
    setTodayKey((k) => k + 1);
  }

  function openAbout() {
    setNow(new Date());
    setAbout(true);
    loadFactsOnce();
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
    hoursChanged();
    back();
  }

  /**
   * «Сделано» у повторяющейся: база перевела задачу на следующий раз. Она
   * остаётся в списке с новым сроком и строкой «✓ Сделано», экран — назад.
   */
  function advanced(task: Task) {
    setList((current) =>
      current.kind === "ready"
        ? { ...current, tasks: current.tasks.map((t) => (t.id === task.id ? task : t)) }
        : current,
    );
    setJustDone(task.id);
    hoursChanged();
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

  // «Сферы» знают о сферах то же, что «Дела»: они читаются вместе.
  const spheresState: SpheresState =
    list.kind === "ready" ? { kind: "ready", spheres: list.spheres } : list;
  const unsphered =
    list.kind === "ready"
      ? list.tasks.filter((task) => sphereOf(task.sphereId, list.spheres) === null).length
      : 0;
  // «О себе» — записи без сферы; знания сфер покажет их карточка (035).
  const aboutState: FactsState =
    facts.kind === "ready" ? { kind: "ready", facts: aboutFacts(facts.facts) } : facts;

  /** Экран под «Оформлением»: карточка, «О себе» или корневой с вкладками. */
  function screen() {
    if (card !== null && "db" in boot) {
      return (
        <TaskCard
          db={boot.db}
          task={card}
          spheres={list.kind === "ready" ? list.spheres : null}
          now={now}
          editing={editing}
          onBack={back}
          onGone={gone}
          onAdvanced={advanced}
          onEdit={edit}
          onSaved={saved}
          onDirty={reportDirty}
        />
      );
    }
    if (about) {
      return (
        <FactList
          state={aboutState}
          now={now}
          onBack={back}
          onReload={reloadFacts}
          onClose={closeApp}
          onConfirm={confirm}
          onRemove={remove}
        />
      );
    }
    return (
      <>
        {tab === "today" ? (
          <TodayTab
            state={today}
            spheres={list.kind === "ready" ? list.spheres : []}
            now={now}
            onOpen={open}
            onReload={reloadToday}
            onClose={closeApp}
          />
        ) : null}
        {tab === "week" ? <WeekTab now={now} onClose={closeApp} /> : null}
        {tab === "spheres" ? (
          <SpheresTab
            state={spheresState}
            unsphered={unsphered}
            facts={aboutState.kind === "ready" ? aboutState.facts.length : null}
            onReload={reload}
            onClose={closeApp}
            onOpenAbout={openAbout}
          />
        ) : null}
        {tab === "tasks" ? (
          <TaskList
            state={list}
            view={view}
            now={now}
            doneId={justDone}
            onView={setView}
            onOpen={open}
            onReload={reload}
            onClose={closeApp}
          />
        ) : null}
        <Tabs active={tab} onChange={switchTab} />
      </>
    );
  }

  // «Оформление» встаёт поверх, а экран под ним остаётся смонтированным и
  // скрытым: форма правки, открытая под ним, не теряет введённое.
  return (
    <>
      <Backdrop />
      <div className="layer" hidden={settings}>
        {screen()}
      </div>
      {settings ? (
        <ThemeSettings
          choice={themeChoice}
          scheme={scheme}
          busy={themeBusy}
          error={themeError}
          onChoose={(choice) => void chooseTheme(choice)}
          onBack={back}
        />
      ) : null}
    </>
  );
}
