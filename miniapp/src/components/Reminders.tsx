import { formatMoment } from "../lib/format.ts";
import type { Reminder } from "../lib/tasks.ts";

const STAGE_LABEL: Record<Reminder["stage"], string> = {
  before: "заранее",
  due: "в срок",
};

/**
 * Напоминания задачи: отправленные — зелёной точкой, ждущие — пустой.
 * Пустой список объясняется: без срока напоминать не о чем.
 */
export function Reminders({
  reminders,
  hasDue,
  now,
}: {
  reminders: Reminder[];
  hasDue: boolean;
  now: Date;
}) {
  return (
    <section className="sec">
      <h2 className="sec__h">
        <span>Напоминания</span>
      </h2>
      {reminders.length === 0 ? (
        <p className="note">
          {hasDue ? "Напоминаний нет." : "Напоминаний нет: у задачи нет срока."}
        </p>
      ) : (
        <div className="card">
          <ul className="rem">
            {reminders.map((reminder) => (
              <li className="rem__item" key={reminder.id}>
                <span
                  className={reminder.sentAt ? "rem__dot" : "rem__dot rem__dot--wait"}
                  aria-hidden="true"
                />
                <span className="rem__when">
                  {formatMoment(reminder.fireAt, now)}{" "}
                  <span className="rem__stage">{STAGE_LABEL[reminder.stage]}</span>
                </span>
                <span className={reminder.sentAt ? "rem__state rem__state--sent" : "rem__state"}>
                  {reminder.sentAt ? "отправлено" : "ждёт"}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}
