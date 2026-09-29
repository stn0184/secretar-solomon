import { formatDate, momentFromInputs } from "../lib/format.ts";
import {
  type RepeatChoice,
  type RepeatDraft,
  chooseEvery,
  intervalOf,
  stepWords,
} from "../lib/repeat.ts";
import { type Task, type TaskDraft, repeatOf, repeatOpen, repeatSummary } from "../lib/tasks.ts";
import { Choice } from "./Choice.tsx";

const EVERY = [
  ["none", "Нет"],
  ["day", "Дни"],
  ["week", "Недели"],
  ["month", "Месяцы"],
  ["year", "Годы"],
] as const satisfies readonly (readonly [RepeatChoice, string])[];

const WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"];

/** Дни недели флажками: отметить можно несколько, отмеченные — цветом действия. */
function Days({
  on,
  disabled,
  onChange,
}: {
  on: number[];
  disabled: boolean;
  onChange: (days: number[]) => void;
}) {
  return (
    <div className="days" role="group" aria-label="Дни недели">
      {WEEKDAYS.map((word, i) => {
        const day = i + 1;
        return (
          <label className="days__opt" key={word}>
            <input
              type="checkbox"
              checked={on.includes(day)}
              disabled={disabled}
              onChange={(e) =>
                onChange(
                  e.target.checked
                    ? [...on, day].sort((a, b) => a - b)
                    : on.filter((one) => one !== day),
                )
              }
            />
            <span>{word}</span>
          </label>
        );
      })}
    </div>
  );
}

/**
 * Ряд «Повтор» формы правки — сразу под сроком (прототип 011, §13.6):
 * вид повтора, шаг числом между словами, дни недели или число месяца и
 * строка «Получается» теми же словами, что у бота. Без дня, у идеи и
 * желания выбор заперт на «Нет».
 *
 * Сам ряд дату не двигает: новый выбор уходит наверх (`onChange`), и
 * форма подгоняет под него срок (`applyRepeat`).
 */
export function RepeatRow({
  task,
  draft,
  busy,
  onChange,
}: {
  task: Task;
  draft: TaskDraft;
  busy: boolean;
  onChange: (repeat: RepeatDraft) => void;
}) {
  const open = repeatOpen(draft);
  const choice = repeatOf(draft);
  // Полдень: переход на летнее время не сдвинет день.
  const day = open ? momentFromInputs(draft.day, "12:00") : null;
  const summary = repeatSummary(task, draft);
  const every = choice.every;
  const [before, after] = every === "none" ? ["", ""] : stepWords(every, intervalOf(choice.interval));

  return (
    <div className="form__row">
      <span className="form__k">Повтор</span>
      <div className="rep__every">
        <Choice
          label="Повтор"
          options={EVERY}
          value={every}
          disabled={busy || day === null}
          onChange={(next) => {
            if (day !== null) {
              onChange(chooseEvery(choice, next, day));
            }
          }}
        />
      </div>

      {day === null ? (
        <p className="form__hint">
          {draft.kind === "task"
            ? "Повтор можно выбрать, когда выбран день."
            : "У идей и желаний повтора нет."}
        </p>
      ) : null}

      {day !== null && every !== "none" ? (
        <div className="rep__step">
          <span>{before}</span>
          <input
            type="number"
            className="input input--step"
            aria-label="Шаг"
            inputMode="numeric"
            min={1}
            max={99}
            value={choice.interval}
            disabled={busy}
            onChange={(e) => onChange({ ...choice, interval: e.target.value })}
          />
          <span>{after}</span>
        </div>
      ) : null}

      {day !== null && every === "week" ? (
        <>
          <Days
            on={choice.weekdays}
            disabled={busy}
            onChange={(weekdays) => onChange({ ...choice, weekdays })}
          />
          {choice.weekdays.length === 0 ? (
            <p className="form__hint">Отметьте хотя бы один день недели.</p>
          ) : null}
        </>
      ) : null}

      {day !== null && every === "month" ? (
        <Choice
          label="День месяца"
          options={[
            ["date", `${day.getDate()}-го`],
            ["last", "В последний день"],
          ]}
          value={choice.last ? "last" : "date"}
          disabled={busy}
          onChange={(value) => onChange({ ...choice, last: value === "last" })}
        />
      ) : null}

      {day !== null && every === "year" ? (
        <p className="form__hint">День и месяц — из срока: {formatDate(day)}.</p>
      ) : null}

      {summary ? (
        <p className="rep__words">
          Получается: <b>{summary.words}</b>.
          {summary.changed ? " Срок выше станет первым разом." : null}
        </p>
      ) : null}
    </div>
  );
}
