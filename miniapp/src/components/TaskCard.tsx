import { useEffect, useState } from "react";

import { formatDay, formatDue, formatMoment, sameDay } from "../lib/format.ts";
import { repeatWords } from "../lib/repeat.ts";
import type { Db } from "../lib/supabase.ts";
import {
  type Task,
  type TaskDetails,
  completeTask,
  loadTaskDetails,
  questionOf,
  removeTask,
} from "../lib/tasks.ts";
import { confirmDelete } from "../lib/telegram.ts";
import { type ActionKind, Actions } from "./Actions.tsx";
import { PriorityChip, PromiseChip, ReviewChip } from "./Chip.tsx";
import { ErrorNote } from "./ErrorNote.tsx";
import { Field } from "./Field.tsx";
import { Header } from "./Header.tsx";
import { Reminders } from "./Reminders.tsx";
import { Skeleton } from "./Skeleton.tsx";
import { SourceMessage } from "./SourceMessage.tsx";
import { TaskEdit } from "./TaskEdit.tsx";

type DetailsState =
  | { kind: "loading" }
  | { kind: "ready"; details: TaskDetails }
  | { kind: "failed"; message: string };

const KIND_WORD: Record<Task["kind"], string> = {
  task: "Задача",
  idea: "Идея",
  wish: "Желание",
};

function PencilIcon() {
  return (
    <svg
      viewBox="0 0 20 20"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M12.8 3.7l3.5 3.5L7.5 16H4v-3.5z" />
      <path d="M11 5.5l3.5 3.5" />
    </svg>
  );
}

/**
 * Карточка задачи: поля как у бота в подтверждении, строка «Изменить»,
 * исходное сообщение целиком, напоминания и два действия. Задача приходит
 * из списка, сообщение и напоминания докачиваются здесь.
 *
 * После подтверждённого действия `onGone` — список убирает задачу и
 * возвращается; отказ базы остаётся текстом под кнопками. Повторяющаяся
 * задача после «Сделано» не уходит: база перевела её на следующий раз,
 * и `onAdvanced` кладёт в список её новую строку.
 *
 * `editing` — на месте карточки форма правки (`TaskEdit`). Карточка
 * остаётся владельцем докачанного: форма берёт у неё исходное сообщение,
 * а после «Сохранить» приходит новая задача — и напоминания читаются
 * заново, их пересчитала база.
 */
export function TaskCard({
  db,
  task,
  now,
  editing,
  onBack,
  onGone,
  onAdvanced,
  onEdit,
  onSaved,
  onDirty,
}: {
  db: Db;
  task: Task;
  now: Date;
  editing: boolean;
  onBack: () => void;
  onGone: (taskId: string) => void;
  onAdvanced: (task: Task) => void;
  onEdit: () => void;
  onSaved: (task: Task) => void;
  onDirty: (dirty: boolean) => void;
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

  /**
   * «Сделано». У повторяющейся с запросом уходит раз, который видно на
   * экране: задача уже на другом — база второй раз её не переводит.
   */
  async function done() {
    setBusy("done");
    setActionError(null);
    const result = await completeTask(db, task.id, task.repeat ? task.occurrenceAt : null);
    setBusy(null);
    if (!result.ok) {
      setActionError(result.message);
    } else if (result.next) {
      onAdvanced(result.next);
    } else {
      onGone(task.id);
    }
  }

  async function onDelete() {
    if (await confirmDelete(task.title, task.repeat !== null)) {
      await run("delete", () => removeTask(db, task.id));
    }
  }

  function reloadDetails() {
    setDetails({ kind: "loading" });
    setReloadKey((k) => k + 1);
  }

  /** База записала правку: старые напоминания больше не правда — до ответа «загрузка». */
  function saved(next: Task) {
    setDetails({ kind: "loading" });
    onSaved(next);
  }

  if (editing) {
    return (
      <TaskEdit
        db={db}
        task={task}
        message={details.kind === "ready" ? details.details.message : undefined}
        now={now}
        onBack={onBack}
        onSaved={saved}
        onDirty={onDirty}
      />
    );
  }

  const dueToday = task.dueAt !== null && sameDay(task.dueAt, now);
  const question = questionOf(task, now);

  return (
    <main className="screen">
      <Header title={task.title} onBack={onBack} />

      <div className="card">
        <div className="kv">
          <Field label="Срок" note={dueToday && task.dueAt ? formatDay(task.dueAt) : undefined}>
            {task.dueAt ? formatDue(task.dueAt, task.duePrecision, now) : "не назван"}
          </Field>
          {task.repeat ? (
            <Field label="Повтор" note="«Сделано» переведёт задачу на следующий раз">
              {repeatWords(task.repeat)}
            </Field>
          ) : null}
          {task.kind !== "task" ? <Field label="Вид">{KIND_WORD[task.kind]}</Field> : null}
          {task.priority === "high" ? (
            <Field label="Приоритет">
              <PriorityChip task={task} compact={false} />
            </Field>
          ) : null}
          {task.priority === "low" ? <Field label="Приоритет">низкий</Field> : null}
          {task.promise ? (
            <Field label="Обещание">
              <PromiseChip task={task} compact={false} />
            </Field>
          ) : null}
          {task.people.length > 0 ? <Field label="Люди">{task.people.join(", ")}</Field> : null}
          {task.needsReview || question ? (
            <Field label="Разбор">
              <ReviewChip task={task} />
              {question?.askedAt ? (
                <span className="kv__q">
                  Бот спросил {formatMoment(question.askedAt, now)}: «{question.text}»
                </span>
              ) : null}
            </Field>
          ) : null}
        </div>
        <button type="button" className="kv__edit" onClick={onEdit}>
          <PencilIcon />
          Изменить
        </button>
      </div>

      {details.kind === "loading" ? <Skeleton rows={2} label="Загружаем карточку" /> : null}
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

      <Actions
        busy={busy}
        error={actionError}
        primary={{
          kind: "done",
          label: "Сделано",
          busyLabel: task.repeat ? "Отмечаем…" : "Закрываем…",
          onClick: () => void done(),
        }}
        onDelete={() => void onDelete()}
      />
    </main>
  );
}
