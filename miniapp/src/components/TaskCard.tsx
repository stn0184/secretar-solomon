import { type CSSProperties, useEffect, useState } from "react";

import { formatMoment } from "../lib/format.ts";
import { repeatWords } from "../lib/repeat.ts";
import { type Sphere, sphereColor, sphereName, sphereOf } from "../lib/spheres.ts";
import type { Db } from "../lib/supabase.ts";
import {
  type Task,
  type TaskDetails,
  cardWhen,
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

/**
 * Карточка дела (`techspec/32-redesign.md` §32.2, артборд 05): шапка — сфера,
 * суть, время крупно (у встречи — «14:00–16:00», §31.1), ниже день и
 * длительность; поля стопкой — разбор, сфера, повтор, вид, срочность,
 * обещание, люди, напоминания; исходное сообщение цитатой; внизу «Сделано ·
 * Изменить · Удалить». Дело приходит из списка, сообщение и напоминания
 * докачиваются здесь. Время в карточке не анимируется (`design.md` §1).
 *
 * После подтверждённого действия `onGone` — список убирает дело и
 * возвращается; отказ базы остаётся текстом под кнопками. Повторяющееся
 * дело после «Сделано» не уходит: база перевела его на следующий раз, и
 * `onAdvanced` кладёт в список его новую строку.
 *
 * `editing` — на месте карточки форма правки (`TaskEdit`). Карточка остаётся
 * владельцем докачанного: форма берёт у неё исходное сообщение, а после
 * «Сохранить» приходит новое дело — и напоминания читаются заново, их
 * пересчитала база. `spheres` — живые сферы владельца для строки «Сфера» и
 * выбора в форме; `null` — не прочитались.
 */
export function TaskCard({
  db,
  task,
  spheres,
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
  spheres: readonly Sphere[] | null;
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
   * «Сделано». У повторяющегося с запросом уходит раз, который видно на
   * экране: дело уже на другом — база второй раз его не переводит.
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
        spheres={spheres}
        message={details.kind === "ready" ? details.details.message : undefined}
        now={now}
        onBack={onBack}
        onSaved={saved}
        onDirty={onDirty}
      />
    );
  }

  const sphere = sphereOf(task.sphereId, spheres ?? []);
  const when = cardWhen(task, now);
  const question = questionOf(task, now);

  return (
    <main className="screen">
      <Header onBack={onBack} backLabel="Дела" />

      <section className="ch card" style={{ "--c": sphereColor(sphere) } as CSSProperties}>
        <span className="ch__s">
          <i aria-hidden="true" />
          {sphereName(sphere)}
        </span>
        <h1 className="ch__t">{task.title}</h1>
        {when.time ? <span className="ch__time">{when.time}</span> : null}
        <span className={when.late ? "ch__d ch__d--late" : "ch__d"}>{when.day}</span>
      </section>

      <div className="kv card">
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
        <Field label="Сфера">{sphereName(sphere)}</Field>
        {task.repeat ? (
          <Field label="Повтор" note="«Сделано» переведёт дело на следующий раз">
            {repeatWords(task.repeat)}
          </Field>
        ) : null}
        {task.kind !== "task" ? <Field label="Вид">{KIND_WORD[task.kind]}</Field> : null}
        {task.priority === "high" ? (
          <Field label="Приоритет">
            <PriorityChip task={task} />
          </Field>
        ) : null}
        {task.priority === "low" ? <Field label="Приоритет">низкий</Field> : null}
        {task.promise ? (
          <Field label="Обещание">
            <PromiseChip task={task} compact={false} />
          </Field>
        ) : null}
        {task.people.length > 0 ? <Field label="Люди">{task.people.join(", ")}</Field> : null}
        {details.kind !== "failed" ? (
          <Reminders
            reminders={details.kind === "ready" ? details.details.reminders : null}
            hasDue={task.dueAt !== null}
            now={now}
          />
        ) : null}
      </div>

      {details.kind === "loading" ? (
        <div className="src src--wait" aria-busy="true" aria-label="Загружаем сообщение" />
      ) : null}
      {details.kind === "failed" ? (
        <ErrorNote message={details.message} onRetry={reloadDetails} />
      ) : null}
      {details.kind === "ready" ? <SourceMessage message={details.details.message} now={now} /> : null}

      <Actions
        busy={busy}
        error={actionError}
        primary={{
          kind: "done",
          label: "Сделано",
          busyLabel: task.repeat ? "Отмечаем…" : "Закрываем…",
          onClick: () => void done(),
        }}
        extra={{ label: "Изменить", onClick: onEdit }}
        onDelete={() => void onDelete()}
      />
    </main>
  );
}
