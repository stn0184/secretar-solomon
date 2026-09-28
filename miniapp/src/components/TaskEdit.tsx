import { useEffect, useState } from "react";

import { formatMoment } from "../lib/format.ts";
import type { Db } from "../lib/supabase.ts";
import {
  type Priority,
  type PromiseSide,
  type SourceMessage as Source,
  type Task,
  type TaskDraft,
  type TaskKind,
  draftOf,
  dueHint,
  editTask,
  needsSaving,
  questionOf,
  taskChanges,
  validateDraft,
} from "../lib/tasks.ts";
import { Actions } from "./Actions.tsx";
import { Choice } from "./Choice.tsx";
import { ReviewChip } from "./Chip.tsx";
import { Header } from "./Header.tsx";
import { SourceMessage } from "./SourceMessage.tsx";

const KINDS = [
  ["task", "Задача"],
  ["idea", "Идея"],
  ["wish", "Желание"],
] as const satisfies readonly (readonly [TaskKind, string])[];

const PRIORITIES = [
  ["low", "Низкая"],
  ["normal", "Обычная"],
  ["high", "Высокая"],
] as const satisfies readonly (readonly [Priority, string])[];

const PROMISES = [
  ["none", "Нет"],
  ["mine", "Я обещал"],
  ["to_me", "Мне обещали"],
] as const satisfies readonly (readonly [PromiseSide | "none", string])[];

/**
 * Форма правки на месте карточки (`techspec/11-edit.md` §11.2): поля
 * стопкой, внизу «Сохранить» и «Отмена». Напоминаний в форме нет — их
 * пересчитает база; исходное сообщение внизу — для сверки.
 *
 * Уходит в базу только изменённое (`taskChanges`); ответ базы — новая
 * задача для карточки и списка (`onSaved`). Отказ — текстом под
 * кнопками, введённое остаётся. Есть ли несохранённое, форма сообщает
 * наверх (`onDirty`): «назад» решает, спрашивать ли «Бросить правку?».
 *
 * `message` — исходное сообщение из карточки; `undefined` — карточка
 * его ещё не докачала, и блока нет.
 */
export function TaskEdit({
  db,
  task,
  message,
  now,
  onBack,
  onSaved,
  onDirty,
}: {
  db: Db;
  task: Task;
  message: Source | null | undefined;
  now: Date;
  onBack: () => void;
  onSaved: (task: Task) => void;
  onDirty: (dirty: boolean) => void;
}) {
  const [draft, setDraft] = useState<TaskDraft>(() => draftOf(task));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const dirty = Object.keys(taskChanges(task, draft)).length > 0;
  useEffect(() => {
    onDirty(dirty);
  }, [dirty, onDirty]);

  function update(patch: Partial<TaskDraft>) {
    setError(null);
    setDraft((current) => ({ ...current, ...patch }));
  }

  async function save() {
    const problem = validateDraft(draft);
    if (problem) {
      setError(problem);
      return;
    }
    const changes = taskChanges(task, draft);
    // Нечего менять и снимать нечего — запроса нет, форма просто закрывается.
    if (!needsSaving(task, changes)) {
      onBack();
      return;
    }
    setBusy(true);
    setError(null);
    const result = await editTask(db, task.id, changes);
    setBusy(false);
    if (result.ok) {
      onSaved(result.task);
    } else {
      setError(result.message);
    }
  }

  const question = questionOf(task, now);
  const dayMissing = draft.day === "";
  const hasTime = !draft.noDue && !dayMissing && draft.time !== "";

  return (
    <main className="screen">
      <Header title="Правка задачи" onBack={onBack} backLabel="Задача" />

      {task.needsReview || question ? (
        <div className="review">
          <div className="review__top">
            <ReviewChip task={task} />
            {question?.askedAt ? <span>{formatMoment(question.askedAt, now)}</span> : null}
          </div>
          {question ? <p className="review__q">Бот спросил: «{question.text}»</p> : null}
          <p className="review__note">
            «Сохранить» снимет пометку и вопрос — даже если ничего не менять.
          </p>
        </div>
      ) : null}

      <div className="card form">
        <label className="form__row">
          <span className="form__k">Суть</span>
          <textarea
            className="input input--area"
            rows={2}
            value={draft.title}
            disabled={busy}
            // Суть — одна строка: перенос из вставки становится пробелом.
            onChange={(e) => update({ title: e.target.value.replace(/\s*\n\s*/g, " ") })}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
              }
            }}
          />
        </label>

        <div className="form__row">
          <span className="form__k">Срок</span>
          <div className="form__due">
            <input
              type="date"
              className="input"
              aria-label="День"
              value={draft.day}
              disabled={busy || draft.noDue}
              onChange={(e) => update({ day: e.target.value })}
            />
            <input
              type="time"
              className="input"
              aria-label="Час"
              value={dayMissing ? "" : draft.time}
              disabled={busy || draft.noDue || dayMissing}
              onChange={(e) => update({ time: e.target.value })}
            />
          </div>
          <div className="form__opts">
            <label className="check">
              <input
                type="checkbox"
                checked={draft.noDue}
                disabled={busy}
                onChange={(e) => update({ noDue: e.target.checked })}
              />
              <span>Без срока</span>
            </label>
            {/* Колесо iPhone ставит час от одного касания, а стереть его
                своими средствами почти нельзя: час снимается здесь. */}
            {hasTime ? (
              <button
                type="button"
                className="form__clear"
                disabled={busy}
                onClick={() => update({ time: "" })}
              >
                Убрать час
              </button>
            ) : null}
          </div>
          <p className="form__hint">{dueHint(draft, now)}</p>
        </div>

        <div className="form__row">
          <span className="form__k">Вид</span>
          <Choice
            label="Вид"
            options={KINDS}
            value={draft.kind}
            disabled={busy}
            onChange={(kind) => update({ kind })}
          />
        </div>

        <div className="form__row">
          <span className="form__k">Срочность</span>
          <Choice
            label="Срочность"
            options={PRIORITIES}
            value={draft.priority}
            disabled={busy}
            onChange={(priority) => update({ priority })}
          />
        </div>

        <div className="form__row">
          <span className="form__k">Обещание</span>
          <Choice
            label="Обещание"
            options={PROMISES}
            value={draft.promise}
            disabled={busy}
            onChange={(promise) => update({ promise })}
          />
        </div>

        <label className="form__row">
          <span className="form__k">Люди</span>
          <input
            type="text"
            className="input"
            value={draft.people}
            disabled={busy}
            placeholder="Например: Кузнецов, Анна"
            onChange={(e) => update({ people: e.target.value })}
          />
          <span className="form__hint">Через запятую.</span>
        </label>
      </div>

      {message !== undefined ? <SourceMessage message={message} now={now} /> : null}

      <Actions
        busy={busy ? "save" : null}
        error={error}
        primary={{
          kind: "save",
          label: "Сохранить",
          busyLabel: "Сохраняем…",
          onClick: () => void save(),
        }}
        secondary={{ label: "Отмена", onClick: onBack }}
      />
    </main>
  );
}
