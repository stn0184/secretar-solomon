import { useEffect, useState } from "react";

import { formatDay, formatDue, sameDay } from "../lib/format.ts";
import type { Db } from "../lib/supabase.ts";
import {
  type Task,
  type TaskDetails,
  completeTask,
  loadTaskDetails,
  removeTask,
} from "../lib/tasks.ts";
import { confirmDelete } from "../lib/telegram.ts";
import { type ActionKind, Actions } from "./Actions.tsx";
import { ErrorNote } from "./ErrorNote.tsx";
import { Field } from "./Field.tsx";
import { Header } from "./Header.tsx";
import { Reminders } from "./Reminders.tsx";
import { SourceMessage } from "./SourceMessage.tsx";
import { taskChips } from "./TaskRow.tsx";

type DetailsState =
  | { kind: "loading" }
  | { kind: "ready"; details: TaskDetails }
  | { kind: "failed"; message: string };

const KIND_WORD: Record<Task["kind"], string> = {
  task: "Задача",
  idea: "Идея",
  wish: "Желание",
};

/**
 * Карточка задачи: поля как у бота в подтверждении, исходное сообщение
 * целиком, напоминания и два действия. Задача приходит из списка, сообщение
 * и напоминания докачиваются здесь.
 *
 * После подтверждённого действия `onGone` — список убирает задачу и
 * возвращается; отказ базы остаётся текстом под кнопками.
 */
export function TaskCard({
  db,
  task,
  now,
  onBack,
  onGone,
}: {
  db: Db;
  task: Task;
  now: Date;
  onBack: () => void;
  onGone: (taskId: string) => void;
}) {
  const [details, setDetails] = useState<DetailsState>({ kind: "loading" });
  const [reloadKey, setReloadKey] = useState(0);
  const [busy, setBusy] = useState<ActionKind | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  // Сообщение и напоминания докачиваются при открытии и по «Обновить»;
  // «загрузка» ставится в обработчике, не в эффекте.
  useEffect(() => {
    let cancelled = false;
    void loadTaskDetails(db, task).then((result) => {
      if (cancelled) {
        return;
      }
      setDetails(
        result.ok
          ? { kind: "ready", details: result.details }
          : { kind: "failed", message: result.message },
      );
    });
    return () => {
      cancelled = true;
    };
  }, [db, task, reloadKey]);

  async function run(kind: ActionKind, action: () => Promise<{ ok: true } | { ok: false; message: string }>) {
    setBusy(kind);
    setActionError(null);
    const result = await action();
    setBusy(null);
    if (result.ok) {
      onGone(task.id);
    } else {
      setActionError(result.message);
    }
  }

  async function onDelete() {
    if (await confirmDelete(task.title)) {
      await run("delete", () => removeTask(db, task.id));
    }
  }

  function reloadDetails() {
    setDetails({ kind: "loading" });
    setReloadKey((k) => k + 1);
  }

  const chips = taskChips(task, false);
  const dueToday = task.dueAt !== null && sameDay(task.dueAt, now);

  return (
    <main className="screen">
      <Header title={task.title} onBack={onBack} />

      <div className="card">
        <div className="kv">
          <Field label="Срок" note={dueToday && task.dueAt ? formatDay(task.dueAt) : undefined}>
            {task.dueAt ? formatDue(task.dueAt, task.duePrecision, now) : "не назван"}
          </Field>
          {task.kind !== "task" ? <Field label="Вид">{KIND_WORD[task.kind]}</Field> : null}
          {task.priority === "high" ? <Field label="Приоритет">{chips[0]}</Field> : null}
          {task.priority === "low" ? <Field label="Приоритет">низкий</Field> : null}
          {task.promise ? (
            <Field label="Обещание">{chips.find((c) => c.key === "mine" || c.key === "theirs")}</Field>
          ) : null}
          {task.people.length > 0 ? <Field label="Люди">{task.people.join(", ")}</Field> : null}
          {task.needsReview ? (
            <Field label="Разбор">{chips.find((c) => c.key === "review")}</Field>
          ) : null}
        </div>
      </div>

      {details.kind === "loading" ? (
        <div className="card skeleton" aria-busy="true" aria-label="Загружаем карточку">
          <div className="skeleton__row" />
          <div className="skeleton__row" />
        </div>
      ) : null}
      {details.kind === "failed" ? (
        <ErrorNote message={details.message} onRetry={reloadDetails} />
      ) : null}
      {details.kind === "ready" ? (
        <>
          <SourceMessage message={details.details.message} now={now} />
          <Reminders
            reminders={details.details.reminders}
            hasDue={task.dueAt !== null}
            now={now}
          />
        </>
      ) : null}

      <p className="note">Изменить текст или срок пока можно только через бота.</p>

      <Actions
        busy={busy}
        error={actionError}
        onDone={() => void run("done", () => completeTask(db, task.id))}
        onDelete={() => void onDelete()}
      />
    </main>
  );
}
