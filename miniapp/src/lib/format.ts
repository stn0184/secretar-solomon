/**
 * Даты словами — в поясе устройства и теми же словами, что у бота
 * (`bot/src/solomon/texts.py`): «пятница, 25 сентября», «сегодня, 18:00».
 * Человек читает одно и то же в чате и в приложении, и разных слов для
 * одного срока быть не должно.
 *
 * Пояс — устройства, а не владельца из `.env`: приложение живёт в
 * телефоне, и «сегодня» здесь — то, что показывают его часы. Бот при
 * напоминании считает по своему правилу (`techspec/06-reminders.md`).
 *
 * Чистый модуль: без React, DOM и сети — проверяется тестами на Node.
 */

export const WEEKDAYS = [
  "воскресенье",
  "понедельник",
  "вторник",
  "среда",
  "четверг",
  "пятница",
  "суббота",
] as const;

export const MONTHS = [
  "января",
  "февраля",
  "марта",
  "апреля",
  "мая",
  "июня",
  "июля",
  "августа",
  "сентября",
  "октября",
  "ноября",
  "декабря",
] as const;

/** «25 сентября» — число без дня недели, когда важна сама дата. */
export function formatDate(moment: Date): string {
  return `${moment.getDate()} ${MONTHS[moment.getMonth()]}`;
}

/** «пятница, 25 сентября» — как человек называет день вслух. */
export function formatDay(moment: Date): string {
  return `${WEEKDAYS[moment.getDay()]}, ${formatDate(moment)}`;
}

/** «18:00». */
export function formatTime(moment: Date): string {
  const hh = String(moment.getHours()).padStart(2, "0");
  const mm = String(moment.getMinutes()).padStart(2, "0");
  return `${hh}:${mm}`;
}

/** Один и тот же календарный день в поясе устройства. */
export function sameDay(a: Date, b: Date): boolean {
  return (
    a.getFullYear() === b.getFullYear() &&
    a.getMonth() === b.getMonth() &&
    a.getDate() === b.getDate()
  );
}

/** «сегодня» для текущего дня, иначе день словами. */
function dayWord(moment: Date, now: Date): string {
  return sameDay(moment, now) ? "сегодня" : formatDay(moment);
}

/**
 * Срок словами: день, а со временем — и час.
 *
 * Час показывается только когда человек его назвал: у срока «в пятницу»
 * в базе стоит 18:00 (`techspec/03-schema.md` §3.3), и произносить его
 * значило бы приписать человеку то, чего он не говорил. Пустая точность
 * читается как день.
 */
export function formatDue(dueAt: Date, precision: "day" | "time" | null, now: Date): string {
  const day = dayWord(dueAt, now);
  return precision === "time" ? `${day}, ${formatTime(dueAt)}` : day;
}

/**
 * Момент, у которого час есть всегда: когда пришло сообщение, когда
 * стучится напоминание. «сегодня, 15:00» / «вторник, 29 сентября, 15:00».
 */
export function formatMoment(at: Date, now: Date): string {
  return `${dayWord(at, now)}, ${formatTime(at)}`;
}

/** Задача без срока: когда записана — «записано 24 сентября». */
export function formatRecorded(createdAt: Date): string {
  return `записано ${formatDate(createdAt)}`;
}

/** Форма слова по русским правилам: 1 задача, 2 задачи, 5 задач, 21 задача. */
export function plural(n: number, one: string, few: string, many: string): string {
  const abs = Math.abs(n) % 100;
  const last = abs % 10;
  if (abs >= 11 && abs <= 19) {
    return many;
  }
  if (last === 1) {
    return one;
  }
  if (last >= 2 && last <= 4) {
    return few;
  }
  return many;
}

/** «7 активных» — подпись под заголовком списка. */
export function countActive(n: number): string {
  return `${n} ${plural(n, "активная", "активные", "активных")}`;
}

/** «одна просрочена», «2 просрочены», «5 просрочено». */
export function countOverdue(n: number): string {
  if (n === 1) {
    return "одна просрочена";
  }
  return `${n} ${plural(n, "просрочена", "просрочены", "просрочено")}`;
}

/* ------------------------------------------------------ поля ввода срока */

function pad(n: number): string {
  return String(n).padStart(2, "0");
}

/** «2026-10-05» — значение для `<input type="date">`; срока нет — пусто. */
export function dateInputValue(at: Date | null): string {
  return at ? `${at.getFullYear()}-${pad(at.getMonth() + 1)}-${pad(at.getDate())}` : "";
}

/** «07:05» — значение для `<input type="time">`; срока нет — пусто. */
export function timeInputValue(at: Date | null): string {
  return at ? formatTime(at) : "";
}

/**
 * Поля «день» и «час» → момент в поясе устройства. Пустое или негодное
 * (30 февраля, 24:00, не те цифры) — `null`: дата не угадывается.
 */
export function momentFromInputs(day: string, time: string): Date | null {
  const d = /^(\d{4})-(\d{2})-(\d{2})$/.exec(day);
  const t = /^(\d{2}):(\d{2})$/.exec(time);
  if (!d || !t) {
    return null;
  }
  const year = Number(d[1]);
  const month = Number(d[2]);
  const date = Number(d[3]);
  const hours = Number(t[1]);
  const minutes = Number(t[2]);
  if (hours > 23 || minutes > 59) {
    return null;
  }
  const at = new Date(year, month - 1, date, hours, minutes);
  // Date сам переносит 30 февраля на 2 марта — такой день не годится.
  if (at.getFullYear() !== year || at.getMonth() !== month - 1 || at.getDate() !== date) {
    return null;
  }
  return at;
}

/**
 * «2026-10-05T12:00:00+03:00» — момент с цифрами часа, которые видел
 * человек, и смещением устройства на этот день. База читает его как тот
 * же миг, а смещение сохраняет смысл «в 12 по моим часам».
 */
export function isoWithOffset(at: Date): string {
  const offset = -at.getTimezoneOffset();
  const sign = offset >= 0 ? "+" : "-";
  const abs = Math.abs(offset);
  return (
    `${dateInputValue(at)}T${formatTime(at)}:${pad(at.getSeconds())}` +
    `${sign}${pad(Math.floor(abs / 60))}:${pad(abs % 60)}`
  );
}
