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

/** Дни недели коротко — «пт» в строке дела (этап 036). */
export const WEEKDAYS_SHORT = ["вс", "пн", "вт", "ср", "чт", "пт", "сб"] as const;

/** «25 сентября» — число без дня недели, когда важна сама дата. */
export function formatDate(moment: Date): string {
  return `${moment.getDate()} ${MONTHS[moment.getMonth()]}`;
}

/** «26 окт», «5 мая» — день и месяц коротко: строка дела, строки «Недели». */
export function formatShortDate(moment: Date): string {
  return `${moment.getDate()} ${MONTHS[moment.getMonth()]!.slice(0, 3)}`;
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

/** Точность срока (`techspec/21-part-of-day.md` §21.2): день, час или часть дня. */
export type DuePrecision = "day" | "time" | "morning" | "afternoon" | "evening";
export type PartOfDay = "morning" | "afternoon" | "evening";

/**
 * Слова частей дня — те же, что у бота (`texts.PART_WORDS`). Часов частей
 * у приложения нет: начало части лежит в записанном сроке (§21.4).
 */
export const PART_WORDS: Record<PartOfDay, string> = {
  morning: "утром",
  afternoon: "днём",
  evening: "вечером",
};

/** Часть дня — одна из трёх; день, час и пустая точность — нет. */
export function isPart(precision: DuePrecision | null): precision is PartOfDay {
  return precision === "morning" || precision === "afternoon" || precision === "evening";
}

/** Часть в начале фразы: «Утром», «Днём», «Вечером». */
export function partLabel(part: PartOfDay): string {
  const word = PART_WORDS[part];
  return word.charAt(0).toUpperCase() + word.slice(1);
}

/**
 * Время встречи: «14:00–16:00» — начало и конец по длительности (§31.1),
 * без длительности — одно начало. Так же, как у бота (`texts.format_due`).
 */
export function formatTimeRange(dueAt: Date, duration: number | null): string {
  if (!duration) {
    return formatTime(dueAt);
  }
  const end = new Date(dueAt.getTime() + duration * 60 * 1000);
  return `${formatTime(dueAt)}–${formatTime(end)}`;
}

/** «2 ч 10 мин», «45 мин», «3 ч» — те же слова, что в ответе бота о часах (§31.2). */
export function hoursWords(minutes: number): string {
  const whole = Math.floor(minutes / 60);
  const rest = minutes % 60;
  if (whole && rest) {
    return `${whole} ч ${rest} мин`;
  }
  return whole ? `${whole} ч` : `${rest} мин`;
}

/**
 * Срок словами: день, а со временем — и час, у части дня — её слово.
 *
 * Час показывается только когда человек его назвал: у срока «в пятницу»
 * в базе стоит 18:00 (`techspec/03-schema.md` §3.3), у «утром» — 08:00
 * (§21.2), и произносить его значило бы приписать человеку то, чего он не
 * говорил: «сегодня утром», «пятница, 9 октября, утром». Пустая точность
 * читается как день. У встречи с длительностью — и конец: «14:00–16:00».
 */
export function formatDue(
  dueAt: Date,
  precision: DuePrecision | null,
  now: Date,
  duration: number | null = null,
): string {
  const today = sameDay(dueAt, now);
  const day = today ? "сегодня" : formatDay(dueAt);
  if (precision === "time") {
    return `${day}, ${formatTimeRange(dueAt, duration)}`;
  }
  if (isPart(precision)) {
    return `${day}${today ? " " : ", "}${PART_WORDS[precision]}`;
  }
  return day;
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

/** «11 открытых», «1 открытое» — надзаголовок «Дел». */
export function countOpen(n: number): string {
  return `${n} ${plural(n, "открытое", "открытых", "открытых")}`;
}

/** «одно просрочено», «2 просрочены», «5 просрочено» — о делах. */
export function countOverdue(n: number): string {
  if (n === 1) {
    return "одно просрочено";
  }
  return `${n} ${plural(n, "просрочено", "просрочены", "просрочено")}`;
}

/** Понедельник недели, в которую попадает день, — в полночь по часам устройства. */
export function weekStart(moment: Date): Date {
  const shift = (moment.getDay() + 6) % 7;
  return new Date(moment.getFullYear(), moment.getMonth(), moment.getDate() - shift);
}

/** «5 – 11 октября», на стыке — «28 сентября – 4 октября»: неделя с понедельника. */
export function formatWeekRange(now: Date): string {
  const monday = weekStart(now);
  const sunday = new Date(monday.getFullYear(), monday.getMonth(), monday.getDate() + 6);
  return monday.getMonth() === sunday.getMonth()
    ? `${monday.getDate()} – ${formatDate(sunday)}`
    : `${formatDate(monday)} – ${formatDate(sunday)}`;
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
