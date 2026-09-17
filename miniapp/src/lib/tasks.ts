/**
 * Активные задачи владельца.
 *
 * Фильтра по владельцу здесь нет намеренно: строки режет сама база правилами
 * доступа (`techspec/04-access.md` §4.2) — она смотрит на клейм `telegram_id`
 * в токене. Приложение чужой id подставить не может, потому что не задаёт его
 * вовсе; без токена запрос не вернёт ни строки.
 *
 * Отказ — это текст на экране, а не исключение (`session.ts`).
 */

import type { SupabaseClient } from "@supabase/supabase-js";

export interface Task {
  id: string;
  title: string;
}

/** Сколько задач показывает экран. Пагинация — этап настоящих экранов. */
export const TASKS_SHOWN = 100;

export type TasksResult =
  | { ok: true; tasks: Task[]; more: boolean }
  | { ok: false; message: string };

function toTask(row: unknown): Task | null {
  if (typeof row !== "object" || row === null) {
    return null;
  }
  const { id, title } = row as { id?: unknown; title?: unknown };
  if (typeof id !== "string" || typeof title !== "string") {
    return null;
  }
  return { id, title };
}

/**
 * Прочитать активные задачи, новые сверху.
 *
 * Спрашивается на одну задачу больше, чем показывается: пришла лишняя —
 * значит, показаны не все, и экран об этом говорит.
 */
export async function loadActiveTasks(client: SupabaseClient): Promise<TasksResult> {
  let rows: unknown;
  try {
    const { data, error } = await client
      .from("tasks")
      .select("id, title")
      .eq("status", "active")
      .order("created_at", { ascending: false })
      .limit(TASKS_SHOWN + 1);
    if (error) {
      return { ok: false, message: `Не получилось прочитать задачи: ${error.message}` };
    }
    rows = data;
  } catch {
    return { ok: false, message: "Не получилось связаться с базой. Проверьте сеть." };
  }

  if (!Array.isArray(rows)) {
    return { ok: false, message: "База ответила непонятно." };
  }

  const tasks: Task[] = [];
  for (const row of rows) {
    const task = toTask(row);
    if (task === null) {
      return { ok: false, message: "База вернула задачу без названия." };
    }
    tasks.push(task);
  }

  return { ok: true, tasks: tasks.slice(0, TASKS_SHOWN), more: tasks.length > TASKS_SHOWN };
}
