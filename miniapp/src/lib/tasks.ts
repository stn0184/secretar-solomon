/**
 * Задачи владельца: список, карточка и два действия.
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

import { sameDay } from "./format.ts";
import { type Db, type DbFailure, query } from "./supabase.ts";

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
}

/** Исходное сообщение задачи — целиком, как его читал помощник. */
export interface SourceMessage {
  text: string;
  receivedAt: Date;
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
export type ActionResult = { ok: true } | { ok: false; message: string };

/* ----------------------------------------------------------------- разбор */

const COLUMNS =
  "id, title, kind, due_at, due_precision, priority, promise, people, needs_review, source_message_id, created_at";

function oneOf<T extends string>(value: unknown, allowed: readonly T[], fallback: T): T {
  return typeof value === "string" && (allowed as readonly string[]).includes(value)
    ? (value as T)
    : fallback;
}

function dateOrNull(value: unknown): Date | null {
  if (typeof value !== "string") {
    return null;
  }
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

/** Строка `tasks` → задача. Не годится (нет id или названия) — `null`. */
export function parseTask(row: unknown): Task | null {
  if (typeof row !== "object" || row === null) {
    return null;
  }
  const r = row as Record<string, unknown>;
  if (typeof r.id !== "string" || typeof r.title !== "string") {
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
  };
}

function parseMessage(row: unknown): SourceMessage | null {
  if (typeof row !== "object" || row === null) {
    return null;
  }
  const r = row as Record<string, unknown>;
  const receivedAt = dateOrNull(r.received_at);
  if (typeof r.text !== "string" || !receivedAt) {
    return null;
  }
  return { text: r.text, receivedAt };
}

function parseReminder(row: unknown): Reminder | null {
  if (typeof row !== "object" || row === null) {
    return null;
  }
  const r = row as Record<string, unknown>;
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

function failed(prefix: string, failure: DbFailure): { ok: false; message: string } {
  return { ok: false, message: `${prefix}. ${failure.message}` };
}

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
          client.from("messages").select("text, received_at").eq("id", messageId).limit(1),
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
      message: parseMessage(messageResult.data?.[0]),
      reminders: (remindersResult.data ?? []).flatMap((row) => parseReminder(row) ?? []),
    },
  };
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
