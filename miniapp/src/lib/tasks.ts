/**
 * Задачи владельца: список, карточка, правка и два действия.
 *
 * Фильтра по владельцу здесь нет намеренно: строки режет сама база правилами
 * доступа (`techspec/04-access.md` §4.2) — она смотрит на клейм `telegram_id`
 * в токене. Приложение чужой id подставить не может, потому что не задаёт его
 * вовсе; без токена запрос не вернёт ни строки.
 *
 * Отказ — это текст на экране, а не исключение (`supabase.ts`). Действие
 * считается сделанным только когда база вернула строку (инвариант 4).
 *
 * Группировка и разбор строк — чистые функции, проверяются на Node.
 */

import type { SupabaseClient } from "@supabase/supabase-js";

import {
  dateInputValue,
  isoWithOffset,
  momentFromInputs,
  sameDay,
  timeInputValue,
} from "./format.ts";
import { dateOrNull, oneOf, recordOf } from "./parse.ts";
import { type ActionResult, type Db, failed, query } from "./supabase.ts";

export type { ActionResult } from "./supabase.ts";

export type TaskKind = "task" | "idea" | "wish";
export type Priority = "low" | "normal" | "high";
export type PromiseSide = "mine" | "to_me";
export type DuePrecision = "day" | "time";

export interface Task {
  id: string;
  title: string;
  kind: TaskKind;
  dueAt: Date | null;
  duePrecision: DuePrecision | null;
  priority: Priority;
  promise: PromiseSide | null;
  people: string[];
  needsReview: boolean;
  sourceMessageId: string | null;
  createdAt: Date;
  /** Вопрос, который бот задал по задаче и на который ещё нет ответа. */
  openQuestion: OpenQuestion | null;
}

/** Уточняющий вопрос бота (§10.4): текст и когда задан; время может не прийти. */
export interface OpenQuestion {
  text: string;
  askedAt: Date | null;
}

/** Вид исходного сообщения (`techspec/03-schema.md` §3.2): текст, голосовое, кружок. */
export type MessageKind = "text" | "voice" | "video_note";

/**
 * Исходное сообщение задачи — целиком, как его читал помощник. У голосового
 * и кружка `text` — расшифровка, а `durationSeconds` — длина звука: по
 * подписи «Голосовое · 0:32» видно, откуда ошибки в словах (§9.4).
 */
export interface SourceMessage {
  text: string;
  receivedAt: Date;
  kind: MessageKind;
  durationSeconds: number | null;
}

export interface Reminder {
  id: string;
  stage: "before" | "due";
  fireAt: Date;
  sentAt: Date | null;
}

export interface TaskDetails {
  message: SourceMessage | null;
  reminders: Reminder[];
}

/** Сколько задач показывает экран. Пагинации нет: дальше — строка «показаны первые». */
export const TASKS_SHOWN = 100;

export type TasksResult =
  | { ok: true; tasks: Task[]; more: boolean }
  | { ok: false; message: string };
export type DetailsResult = { ok: true; details: TaskDetails } | { ok: false; message: string };

/* ----------------------------------------------------------------- разбор */

const COLUMNS =
  "id, title, kind, due_at, due_precision, priority, promise, people, needs_review, " +
  "open_question, question_asked_at, source_message_id, created_at";

/** Строка `tasks` → задача. Не годится (нет id или названия) — `null`. */
export function parseTask(row: unknown): Task | null {
  const r = recordOf(row);
  if (!r || typeof r.id !== "string" || typeof r.title !== "string") {
    return null;
  }
  const dueAt = dateOrNull(r.due_at);
  return {
    id: r.id,
    title: r.title,
    kind: oneOf<TaskKind>(r.kind, ["task", "idea", "wish"], "task"),
    dueAt,
    duePrecision: dueAt ? oneOf<DuePrecision>(r.due_precision, ["day", "time"], "day") : null,
    priority: oneOf<Priority>(r.priority, ["low", "normal", "high"], "normal"),
    promise: r.promise === "mine" || r.promise === "to_me" ? r.promise : null,
    people: Array.isArray(r.people)
      ? r.people.filter((p): p is string => typeof p === "string")
      : [],
    needsReview: r.needs_review === true,
    sourceMessageId: typeof r.source_message_id === "string" ? r.source_message_id : null,
    createdAt: dateOrNull(r.created_at) ?? new Date(0),
    openQuestion:
      typeof r.open_question === "string" && r.open_question.trim() !== ""
        ? { text: r.open_question, askedAt: dateOrNull(r.question_asked_at) }
        : null,
  };
}

/** Строка `messages` → исходное сообщение. Не годится (нет текста или даты) — `null`. */
export function parseSourceMessage(row: unknown): SourceMessage | null {
  const r = recordOf(row);
  if (!r) {
    return null;
  }
  const receivedAt = dateOrNull(r.received_at);
  if (typeof r.text !== "string" || !receivedAt) {
    return null;
  }
  const duration = r.duration_seconds;
  return {
    text: r.text,
    receivedAt,
    // Незнакомый вид читается как текст: подписи не будет, но цитата останется.
    kind: oneOf<MessageKind>(r.kind, ["text", "voice", "video_note"], "text"),
    durationSeconds: typeof duration === "number" && Number.isFinite(duration) ? duration : null,
  };
}

/* ------------------------------------------------------------------ голос */

/** «0:32», «1:35», «62:05» — минуты и секунды, часов нет: кружок и голосовое короткие. */
export function formatDuration(seconds: number): string {
  const whole = Math.max(0, Math.floor(seconds));
  const minutes = Math.floor(whole / 60);
  const rest = whole % 60;
  return `${minutes}:${String(rest).padStart(2, "0")}`;
}

const VOICE_WORD: Record<Exclude<MessageKind, "text">, string> = {
  voice: "Голосовое",
  video_note: "Кружок",
};

/**
 * Подпись над расшифровкой: «Голосовое · 0:32», «Кружок · 0:15»; у текста
 * подписи нет — `null`. Без длительности остаётся одно слово.
 */
export function voiceCaption(message: SourceMessage): string | null {
  if (message.kind === "text") {
    return null;
  }
  const word = VOICE_WORD[message.kind];
  return message.durationSeconds === null ? word : `${word} · ${formatDuration(message.durationSeconds)}`;
}

function parseReminder(row: unknown): Reminder | null {
  const r = recordOf(row);
  if (!r) {
    return null;
  }
  const fireAt = dateOrNull(r.fire_at);
  if (typeof r.id !== "string" || !fireAt || (r.stage !== "before" && r.stage !== "due")) {
    return null;
  }
  return { id: r.id, stage: r.stage, fireAt, sentAt: dateOrNull(r.sent_at) };
}

/* ------------------------------------------------------------ группировка */

export type GroupKey = "overdue" | "today" | "week" | "later" | "none" | "ideas";

export interface TaskGroup {
  key: GroupKey;
  title: string;
  tasks: Task[];
}

/** Порядок групп на экране — фиксированный (`design.md`, прототип 005). */
export const GROUP_ORDER: readonly GroupKey[] = [
  "overdue",
  "today",
  "week",
  "later",
  "none",
  "ideas",
];

export const GROUP_TITLES: Record<GroupKey, string> = {
  overdue: "Просрочено",
  today: "Сегодня",
  week: "На неделе",
  later: "Позже",
  none: "Без срока",
  ideas: "Идеи и желания",
};

const DAY_MS = 24 * 60 * 60 * 1000;

function startOfDay(moment: Date): Date {
  return new Date(moment.getFullYear(), moment.getMonth(), moment.getDate());
}

/** Сколько календарных дней от «сегодня» до дня срока (отрицательно — в прошлом). */
function daysAhead(dueAt: Date, now: Date): number {
  return Math.round((startOfDay(dueAt).getTime() - startOfDay(now).getTime()) / DAY_MS);
}

/**
 * Срок прошёл: для дня — после полуночи в поясе устройства, для часа — сам
 * момент. Бот при напоминании считает по `due_at` (18:00) — это его правило.
 */
export function isOverdue(task: Task, now: Date): boolean {
  if (!task.dueAt || task.kind !== "task") {
    return false;
  }
  if (task.duePrecision === "time") {
    return task.dueAt.getTime() < now.getTime();
  }
  return daysAhead(task.dueAt, now) < 0;
}

/** В какую группу списка попадает задача. */
export function groupOf(task: Task, now: Date): GroupKey {
  if (task.kind !== "task") {
    return "ideas";
  }
  if (!task.dueAt) {
    return "none";
  }
  if (isOverdue(task, now)) {
    return "overdue";
  }
  if (sameDay(task.dueAt, now)) {
    return "today";
  }
  return daysAhead(task.dueAt, now) <= 7 ? "week" : "later";
}

/** Разложить по группам в фиксированном порядке; пустые группы не возвращаются. */
export function groupTasks(tasks: Task[], now: Date): TaskGroup[] {
  const buckets = new Map<GroupKey, Task[]>();
  for (const task of tasks) {
    const key = groupOf(task, now);
    const bucket = buckets.get(key);
    if (bucket) {
      bucket.push(task);
    } else {
      buckets.set(key, [task]);
    }
  }
  return GROUP_ORDER.flatMap((key) => {
    const bucket = buckets.get(key);
    return bucket ? [{ key, title: GROUP_TITLES[key], tasks: bucket }] : [];
  });
}

/* ------------------------------------------------------------------ база */

/**
 * Прочитать активные задачи, новые сверху.
 *
 * Спрашивается на одну задачу больше, чем показывается: пришла лишняя —
 * значит, показаны не все, и экран об этом говорит.
 */
export async function loadActiveTasks(db: Db): Promise<TasksResult> {
  const result = await query(db, (client: SupabaseClient) =>
    client
      .from("tasks")
      .select(COLUMNS)
      .eq("status", "active")
      .order("created_at", { ascending: false })
      .limit(TASKS_SHOWN + 1),
  );
  if (!result.ok) {
    return failed("Не получилось прочитать задачи", result);
  }

  const tasks: Task[] = [];
  for (const row of result.data ?? []) {
    const task = parseTask(row);
    if (task === null) {
      return {
        ok: false,
        message: "Не получилось прочитать задачи. База вернула задачу без названия.",
      };
    }
    tasks.push(task);
  }
  return { ok: true, tasks: tasks.slice(0, TASKS_SHOWN), more: tasks.length > TASKS_SHOWN };
}

/**
 * Докачать карточку: исходное сообщение и напоминания. Сама задача уже
 * в руках — из списка. Оба запроса идут под RLS (`techspec/03-schema.md` §3.6).
 */
export async function loadTaskDetails(db: Db, task: Task): Promise<DetailsResult> {
  const messageId = task.sourceMessageId;
  const [messageResult, remindersResult] = await Promise.all([
    messageId
      ? query(db, (client: SupabaseClient) =>
          client
            .from("messages")
            .select("text, received_at, kind, duration_seconds")
            .eq("id", messageId)
            .limit(1),
        )
      : Promise.resolve({ ok: true as const, data: null }),
    query(db, (client: SupabaseClient) =>
      client
        .from("reminders")
        .select("id, stage, fire_at, sent_at")
        .eq("task_id", task.id)
        .order("fire_at", { ascending: true }),
    ),
  ]);

  if (!messageResult.ok) {
    return failed("Не получилось прочитать карточку", messageResult);
  }
  if (!remindersResult.ok) {
    return failed("Не получилось прочитать карточку", remindersResult);
  }

  // Сообщения может не быть: задача без источника или строка ушла — это
  // не отказ, карточка просто без цитаты.
  return {
    ok: true,
    details: {
      message: parseSourceMessage(messageResult.data?.[0]),
      reminders: (remindersResult.data ?? []).flatMap((row) => parseReminder(row) ?? []),
    },
  };
}

/**
 * «Сохранить»: `edit_task` под токеном (`techspec/03-schema.md` §3.6).
 * База возвращает задачу после правки — она и показывается, а не черновик:
 * «сохранено» говорится о том, что записала база (инвариант 4). Пустой
 * ответ — задачу закрыли или удалили в чате, ничего не записано.
 */
export async function editTask(db: Db, taskId: string, changes: TaskChanges): Promise<EditResult> {
  const result = await query(db, (client: SupabaseClient) =>
    client.rpc("edit_task", { task_id: taskId, changes }),
  );
  if (!result.ok) {
    return failed("Не получилось сохранить задачу", result);
  }
  // Функция возвращает строку таблицы; пустая строка приходит объектом из null.
  const row: unknown = Array.isArray(result.data) ? result.data[0] : result.data;
  const task = parseTask(row);
  if (task === null) {
    return {
      ok: false,
      message: "Задача не найдена — возможно, её закрыли или удалили в чате. Обновите список.",
    };
  }
  return { ok: true, task };
}

/** «Сделано»: `complete_task` под токеном; `null` — задачи у владельца нет. */
export async function completeTask(db: Db, taskId: string): Promise<ActionResult> {
  const result = await query(db, (client: SupabaseClient) =>
    client.rpc("complete_task", { task_id: taskId }),
  );
  if (!result.ok) {
    return failed("Не получилось закрыть задачу", result);
  }
  if (result.data === null) {
    return {
      ok: false,
      message: "Задача не найдена — возможно, её уже закрыли в чате. Обновите список.",
    };
  }
  return { ok: true };
}

/** «Удалить»: обычный delete под RLS; база вернула строку — значит удалено. */
export async function removeTask(db: Db, taskId: string): Promise<ActionResult> {
  const result = await query(db, (client: SupabaseClient) =>
    client.from("tasks").delete().eq("id", taskId).select("id"),
  );
  if (!result.ok) {
    return failed("Не получилось удалить задачу", result);
  }
  if ((result.data ?? []).length === 0) {
    return {
      ok: false,
      message: "Задача не найдена — возможно, её уже удалили. Обновите список.",
    };
  }
  return { ok: true };
}

/* ------------------------------------------------------ правка задачи */

/**
 * Вопрос бота, который стоит показать в карточке: задан не больше суток
 * назад. Бот слушает ответ реплаем только сутки (§10.4) — позже вопрос
 * не висит на экране, но колонка остаётся, и «Сохранить» её снимет.
 */
export function questionOf(task: Task, now: Date): OpenQuestion | null {
  const question = task.openQuestion;
  if (!question?.askedAt) {
    return null;
  }
  return now.getTime() - question.askedAt.getTime() <= DAY_MS ? question : null;
}

/**
 * Черновик формы правки — строки полей ввода, как их держит браузер.
 * День и час раздельно: день без часа — срок «днём» (18:00 ставит база).
 */
export interface TaskDraft {
  title: string;
  /** «2026-10-05» или пусто. */
  day: string;
  /** «12:00» или пусто — тогда срок днём. */
  time: string;
  /** Флажок «Без срока»: поля дня и часа не читаются. */
  noDue: boolean;
  kind: TaskKind;
  priority: Priority;
  promise: PromiseSide | "none";
  /** Люди через запятую. */
  people: string;
}

/** Изменённые поля для `edit_task` — ровно те ключи, что понимает база. */
export interface TaskChanges {
  title?: string;
  /** Момент со смещением — срок с часом; `null` — срок снят. */
  due_at?: string | null;
  /** «2026-10-05» — срок днём, 18:00 этого дня в поясе владельца. */
  due_date?: string;
  kind?: TaskKind;
  priority?: Priority;
  promise?: PromiseSide | null;
  people?: string[];
}

export type EditResult = { ok: true; task: Task } | { ok: false; message: string };

/** Форма открывается с тем, что сейчас записано. Срок днём — поле часа пустое. */
export function draftOf(task: Task): TaskDraft {
  return {
    title: task.title,
    day: dateInputValue(task.dueAt),
    time: task.duePrecision === "time" ? timeInputValue(task.dueAt) : "",
    noDue: task.dueAt === null,
    kind: task.kind,
    priority: task.priority,
    promise: task.promise ?? "none",
    people: task.people.join(", "),
  };
}

/** «Кузнецов,  Анна , ,» → ["Кузнецов", "Анна"]. */
function peopleOf(text: string): string[] {
  return text
    .split(",")
    .map((name) => name.trim())
    .filter((name) => name !== "");
}

/** День из поля — годная дата или `null`. Час для проверки не важен. */
function dayOf(draft: TaskDraft): Date | null {
  return momentFromInputs(draft.day, "00:00");
}

/**
 * Что мешает сохранить — одной фразой, или `null`. Пустой день при снятом
 * «Без срока» — ошибка, а не молчаливое снятие срока.
 */
export function validateDraft(draft: TaskDraft): string | null {
  if (draft.title.trim() === "") {
    return "Напишите, что сделать.";
  }
  if (!draft.noDue && dayOf(draft) === null) {
    return "Выберите день или отметьте «Без срока».";
  }
  return null;
}

function sameList(a: string[], b: string[]): boolean {
  return a.length === b.length && a.every((item, i) => item === b[i]);
}

/** Срок из черновика — ключ `edit_task`, если он отличается от записанного. */
function dueChange(task: Task, draft: TaskDraft): Pick<TaskChanges, "due_at" | "due_date"> {
  if (draft.noDue) {
    return task.dueAt === null ? {} : { due_at: null };
  }
  const moment = draft.time === "" ? null : momentFromInputs(draft.day, draft.time);
  if (moment) {
    const same = task.duePrecision === "time" && task.dueAt?.getTime() === moment.getTime();
    return same ? {} : { due_at: isoWithOffset(moment) };
  }
  const same =
    task.dueAt !== null && task.duePrecision !== "time" && dateInputValue(task.dueAt) === draft.day;
  return same ? {} : { due_date: draft.day };
}

/**
 * Только изменённые поля: база получает ровно правку, а не всю форму.
 * Черновик должен пройти `validateDraft`. Суть — без пробелов по краям;
 * люди — списком без пустых имён; «нет» у обещания — `null`.
 */
export function taskChanges(task: Task, draft: TaskDraft): TaskChanges {
  const changes: TaskChanges = {};
  const title = draft.title.trim();
  if (title !== task.title) {
    changes.title = title;
  }
  Object.assign(changes, dueChange(task, draft));
  if (draft.kind !== task.kind) {
    changes.kind = draft.kind;
  }
  if (draft.priority !== task.priority) {
    changes.priority = draft.priority;
  }
  const promise = draft.promise === "none" ? null : draft.promise;
  if (promise !== task.promise) {
    changes.promise = promise;
  }
  const people = peopleOf(draft.people);
  if (!sameList(people, task.people)) {
    changes.people = people;
  }
  return changes;
}

/**
 * Нужен ли запрос. Без изменений он нужен только задаче с пометкой или
 * вопросом: «Сохранить» подтверждает разбор и снимает их (§11.2).
 */
export function needsSaving(task: Task, changes: TaskChanges): boolean {
  return Object.keys(changes).length > 0 || task.needsReview || task.openQuestion !== null;
}

/**
 * Подсказка под сроком — по тому же правилу, что считает база (§6.1):
 * у дня — 09:00 и 18:00, у часа — за час и в срок; что уже прошло, не
 * называется. Часы — устройства, как во всём приложении (`format.ts`).
 */
export function dueHint(draft: TaskDraft, now: Date): string {
  if (draft.noDue) {
    return "Без срока напоминать не буду. Час можно указать, когда выбран день.";
  }
  if (draft.kind !== "task") {
    return "Об идеях и желаниях не напоминаю.";
  }
  const day = dayOf(draft);
  if (!day) {
    return "Час можно указать, когда выбран день.";
  }
  const withTime = draft.time === "" ? null : momentFromInputs(draft.day, draft.time);
  const due = withTime ?? momentFromInputs(draft.day, "18:00");
  if (!due || due.getTime() <= now.getTime()) {
    return "Срок уже прошёл — напоминаний по нему не будет.";
  }
  if (withTime) {
    const before = withTime.getTime() - 60 * 60 * 1000;
    return before > now.getTime() ? "Напомню за час и в срок." : "Напомню в срок.";
  }
  const morning = momentFromInputs(draft.day, "09:00");
  return morning && morning.getTime() > now.getTime()
    ? "Без часа — напомню в 09:00 и в 18:00 этого дня."
    : "Без часа — напомню в 18:00 этого дня.";
}
