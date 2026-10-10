import { formatMoment } from "../lib/format.ts";
import type { Reminder } from "../lib/tasks.ts";

const STAGE_LABEL: Record<Reminder["stage"], string> = {
  before: "заранее",
  due: "в срок",
};

/**
 * Напоминания дела — строкой среди полей карточки (артборд 05): когда
 * постучится, какая ступень и отправлено ли. Пустой список объясняется: без
 * срока напоминать не о чем. `null` — ещё читаются: серая плашка на месте
 * строк, без спиннера (`design.md` §2).
 */
export function Reminders({
  reminders,
  hasDue,
  now,
}: {
  reminders: Reminder[] | null;
  hasDue: boolean;
  now: Date;
}) {
  return (
    <div className="kv__row">
      <span className="kv__k">Напоминания</span>
      {reminders === null ? (
        <span className="kv__wait" aria-busy="true" aria-label="Загружаем напоминания" />
      ) : reminders.length === 0 ? (
        <span className="kv__v kv__v--muted">
          {hasDue ? "Напоминаний нет." : "Напоминаний нет: у дела нет срока."}
        </span>
      ) : (
        <ul className="rem">
          {reminders.map((reminder) => (
            <li className="rem__item" key={reminder.id}>
              <span className="rem__when">
                {formatMoment(reminder.fireAt, now)} — {STAGE_LABEL[reminder.stage]}
              </span>
              <span className={reminder.sentAt ? "rem__state rem__state--sent" : "rem__state"}>
                {reminder.sentAt ? "отправлено" : "ждёт"}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
