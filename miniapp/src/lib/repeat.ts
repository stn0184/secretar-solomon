/**
 * Повтор задачи (`techspec/13-repeat.md` §13.2, §13.6–13.7): правило из
 * строки базы, слова для карточки и коротко для списка, выбор повтора в
 * форме правки и подгонка даты под правило.
 *
 * Слова — те же, что у бота (`bot/src/solomon/texts.py::repeat_words`):
 * правило пишется в каждой части своё, части друг друга не импортируют, а
 * примеры в тестах общие. Следующий раз здесь не считается — его считает
 * база (`repeat_next`) и отдаёт после «Сделано».
 *
 * Чистый модуль: без React, DOM и сети — проверяется тестами на Node.
 */

import { MONTHS, formatDate, formatDay, sameDay } from "./format.ts";
import { recordOf } from "./parse.ts";

export type Every = "day" | "week" | "month" | "year";

/** «В последний день месяца» — число −1 у `month`. */
export const LAST_DAY = -1;

const MAX_INTERVAL = 99;

/** Правило повтора, как его хранит база (§13.2), в виде приложения. */
export interface Repeat {
  every: Every;
  /** Шаг, 1–99: «через день» — 2. */
  interval: number;
  /** Дни недели 1–7 (понедельник — 1) по порядку — только у `week`. */
  weekdays: number[] | null;
  /** Число 1–31 у `month` и `year`; −1 — последний день, только у `month`. */
  monthDay: number | null;
  /** Месяц 1–12 — только у `year`. */
  month: number | null;
  /** Час серии «09:00»; `null` — повтор днём. Ставит база из срока. */
  time: string | null;
}

/** Ключи правила для `edit_task` — без часа: его база берёт из срока (§13.2). */
export interface RepeatChange {
  every: Every;
  interval: number;
  weekdays: number[] | null;
  month_day: number | null;
  month: number | null;
}

/* --------------------------------------------------------------- разбор */

function integer(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) ? value : null;
}

function inRange(value: number | null, low: number, high: number): value is number {
  return value !== null && value >= low && value <= high;
}

function weekdaysOf(value: unknown): number[] | null {
  if (!Array.isArray(value) || value.length === 0) {
    return null;
  }
  const days = value.map(integer);
  if (!days.every((day) => inRange(day, 1, 7))) {
    return null;
  }
  return [...new Set(days as number[])].sort((a, b) => a - b);
}

/**
 * `tasks.repeat` → правило. У разовой задачи и у правила не по форме —
 * `null`: задача показывается разовой, а не ломает список.
 */
export function parseRepeat(value: unknown): Repeat | null {
  const r = recordOf(value);
  if (!r || Array.isArray(value)) {
    return null;
  }
  const every = r.every;
  const interval = integer(r.interval);
  if (
    (every !== "day" && every !== "week" && every !== "month" && every !== "year") ||
    !inRange(interval, 1, MAX_INTERVAL)
  ) {
    return null;
  }
  const time = typeof r.time === "string" ? r.time : null;
  const base: Repeat = { every, interval, weekdays: null, monthDay: null, month: null, time };
  if (every === "day") {
    return base;
  }
  if (every === "week") {
    const weekdays = weekdaysOf(r.weekdays);
    return weekdays ? { ...base, weekdays } : null;
  }
  const monthDay = integer(r.month_day);
  if (every === "month") {
    return inRange(monthDay, 1, 31) || monthDay === LAST_DAY ? { ...base, monthDay } : null;
  }
  const month = integer(r.month);
  return inRange(monthDay, 1, 31) && inRange(month, 1, 12) ? { ...base, monthDay, month } : null;
}

/** Правило → ключ `repeat` для `edit_task`. */
export function repeatJson(rule: Repeat): RepeatChange {
  return {
    every: rule.every,
    interval: rule.interval,
    weekdays: rule.weekdays,
    month_day: rule.monthDay,
    month: rule.month,
  };
}

/* ---------------------------------------------------------------- слова */

// «Каждый понедельник», «каждую среду», «каждое воскресенье» — род дня.
const EVERY_WEEKDAY = ["каждый", "каждый", "каждую", "каждый", "каждую", "каждую", "каждое"];
const WEEKDAYS_ACCUSATIVE = [
  "понедельник",
  "вторник",
  "среду",
  "четверг",
  "пятницу",
  "субботу",
  "воскресенье",
];
const WEEKDAYS_DATIVE_PLURAL = [
  "понедельникам",
  "вторникам",
  "средам",
  "четвергам",
  "пятницам",
  "субботам",
  "воскресеньям",
];
const WEEKDAYS_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"];
const WORKDAYS = [1, 2, 3, 4, 5];
const WEEKEND = [6, 7];
const ALL_WEEK = [1, 2, 3, 4, 5, 6, 7];

// Единица шага: «каждый» при 1 и при 21, 31…; формы для 1, 2–4 и 5–20.
const UNITS: Record<Every, [string, string, string, string]> = {
  day: ["каждый", "день", "дня", "дней"],
  week: ["каждую", "неделю", "недели", "недель"],
  month: ["каждый", "месяц", "месяца", "месяцев"],
  year: ["каждый", "год", "года", "лет"],
};

/** Какая форма единицы у шага: одна, несколько (2–4) или много. */
function formOf(interval: number): 1 | 2 | 3 {
  const tens = interval % 100;
  const ones = interval % 10;
  if (ones === 1 && tens !== 11) {
    return 1;
  }
  if (ones >= 2 && ones <= 4 && !(tens >= 12 && tens <= 14)) {
    return 2;
  }
  return 3;
}

/** Шаг правила: «каждую неделю», «каждые 2 недели», «каждые 5 недель», «каждую 21 неделю». */
function everyWords(unit: Every, interval: number): string {
  const [every, one, few, many] = UNITS[unit];
  if (interval === 1) {
    return `${every} ${one}`;
  }
  const form = formOf(interval);
  if (form === 1) {
    return `${every} ${interval} ${one}`;
  }
  return `каждые ${interval} ${form === 2 ? few : many}`;
}

/** «а», «а и б», «а, б и в». */
function listed(words: string[]): string {
  const last = words.at(-1) ?? "";
  return words.length > 1 ? `${words.slice(0, -1).join(", ")} и ${last}` : last;
}

/** Слово дня недели 1–7 из списка «понедельник…воскресенье». */
function dayWord(words: readonly string[], day: number): string {
  return words[day - 1] ?? "";
}

function sameDays(a: number[], b: number[]): boolean {
  return a.length === b.length && a.every((day, i) => day === b[i]);
}

/** Дни недели после шага: «по будням», «по выходным», «по средам и пятницам». */
function weekdaysWords(days: number[]): string {
  if (sameDays(days, WORKDAYS)) {
    return "по будням";
  }
  if (sameDays(days, WEEKEND)) {
    return "по выходным";
  }
  return `по ${listed(days.map((day) => dayWord(WEEKDAYS_DATIVE_PLURAL, day)))}`;
}

function sortedDays(rule: Repeat): number[] {
  return [...(rule.weekdays ?? [])].sort((a, b) => a - b);
}

/**
 * Правило словами без часа — час уже в сроке (§13.7): «каждый
 * понедельник», «каждые 3 месяца 5-го». Так же говорит бот.
 */
export function repeatWords(rule: Repeat): string {
  const interval = rule.interval;
  if (rule.every === "day") {
    return interval === 2 ? "через день" : everyWords("day", interval);
  }
  if (rule.every === "week") {
    const days = sortedDays(rule);
    if (sameDays(days, ALL_WEEK)) {
      return interval === 1 ? "каждый день" : `${everyWords("week", interval)}, каждый день`;
    }
    if (interval === 1) {
      const [day] = days;
      if (days.length === 1 && day !== undefined) {
        return `${dayWord(EVERY_WEEKDAY, day)} ${dayWord(WEEKDAYS_ACCUSATIVE, day)}`;
      }
      return weekdaysWords(days);
    }
    return `${everyWords("week", interval)} ${weekdaysWords(days)}`;
  }
  const monthDay = rule.monthDay ?? 1;
  if (rule.every === "month") {
    if (monthDay === LAST_DAY) {
      return interval === 1
        ? "в последний день месяца"
        : `${everyWords("month", interval)} в последний день`;
    }
    return `${everyWords("month", interval)} ${monthDay}-го`;
  }
  return `${everyWords("year", interval)} ${monthDay} ${MONTHS[(rule.month ?? 1) - 1]}`;
}

/**
 * Правило коротко — для строки списка (прототип 011): «каждый пн», «пн и
 * пт», «по будням», «10-го», «в последний день», «каждый год». С шагом
 * больше одного короче не сказать без потери смысла — словами целиком.
 */
export function repeatShort(rule: Repeat): string {
  if (rule.interval !== 1) {
    return rule.every === "year" ? everyWords("year", rule.interval) : repeatWords(rule);
  }
  if (rule.every === "week") {
    const days = sortedDays(rule);
    const [day] = days;
    if (days.length === 1 && day !== undefined) {
      return `${dayWord(EVERY_WEEKDAY, day)} ${dayWord(WEEKDAYS_SHORT, day)}`;
    }
    if (sameDays(days, ALL_WEEK) || sameDays(days, WORKDAYS) || sameDays(days, WEEKEND)) {
      return repeatWords(rule);
    }
    return listed(days.map((one) => dayWord(WEEKDAYS_SHORT, one)));
  }
  if (rule.every === "month") {
    return rule.monthDay === LAST_DAY ? "в последний день" : `${rule.monthDay ?? 1}-го`;
  }
  if (rule.every === "year") {
    return "каждый год";
  }
  return repeatWords(rule);
}

/* ---------------------------------------------------------------- форма */

/** Выбор в ряду «Повтор» формы правки. */
export type RepeatChoice = "none" | Every;

/**
 * Выбор повтора, как его держит форма. Число месяца и день с месяцем у
 * лет в выбор не входят — они берутся из даты срока (§13.6).
 */
export interface RepeatDraft {
  every: RepeatChoice;
  /** Шаг как в поле ввода: «2». Негодный — ошибка формы. */
  interval: string;
  /** Отмеченные дни недели 1–7 — у недель. */
  weekdays: number[];
  /** У месяцев: «В последний день» вместо числа из даты. */
  last: boolean;
}

export const NO_REPEAT: RepeatDraft = { every: "none", interval: "1", weekdays: [], last: false };

/** Форма открывается с правилом задачи. */
export function repeatDraftOf(rule: Repeat | null): RepeatDraft {
  if (rule === null) {
    return NO_REPEAT;
  }
  return {
    every: rule.every,
    interval: String(rule.interval),
    weekdays: rule.every === "week" ? sortedDays(rule) : [],
    last: rule.every === "month" && rule.monthDay === LAST_DAY,
  };
}

/** Шаг из поля: целое 1–99, иначе `null`. */
export function intervalOf(text: string): number | null {
  const trimmed = text.trim();
  if (!/^\d{1,2}$/.test(trimmed)) {
    return null;
  }
  const value = Number(trimmed);
  return value >= 1 && value <= MAX_INTERVAL ? value : null;
}

/**
 * Тот же ли выбор: сравниваются только поля выбранного правила — дни,
 * отмеченные у недель до перехода на месяцы, выбора не меняют.
 */
export function sameChoice(a: RepeatDraft, b: RepeatDraft): boolean {
  if (a.every !== b.every) {
    return false;
  }
  if (a.every === "none") {
    return true;
  }
  if (intervalOf(a.interval) !== intervalOf(b.interval)) {
    return false;
  }
  if (a.every === "week") {
    const days = (d: number[]) => [...new Set(d)].sort((x, y) => x - y);
    return sameDays(days(a.weekdays), days(b.weekdays));
  }
  return a.every !== "month" || a.last === b.last;
}

/** День недели по-русски: понедельник — 1, воскресенье — 7. */
export function isoWeekday(day: Date): number {
  return ((day.getDay() + 6) % 7) + 1;
}

/**
 * Выбрали вид повтора: шаг — снова один, день недели — из даты (§13.6).
 * Число месяца берётся из даты само. Тот же вид — выбор как был.
 */
export function chooseEvery(current: RepeatDraft, every: RepeatChoice, day: Date): RepeatDraft {
  if (every === current.every) {
    return current;
  }
  if (every === "none") {
    return NO_REPEAT;
  }
  return {
    every,
    interval: "1",
    weekdays: every === "week" ? [isoWeekday(day)] : [],
    last: false,
  };
}

/**
 * Выбор и дата срока → правило. «Нет», неделя без дня и негодный шаг —
 * `null`: сохранять нечего или нельзя.
 */
export function ruleOf(draft: RepeatDraft, day: Date): Repeat | null {
  const interval = intervalOf(draft.interval);
  if (draft.every === "none" || interval === null) {
    return null;
  }
  const base: Repeat = {
    every: draft.every,
    interval,
    weekdays: null,
    monthDay: null,
    month: null,
    time: null,
  };
  switch (draft.every) {
    case "day":
      return base;
    case "week": {
      const weekdays = [...new Set(draft.weekdays)].sort((a, b) => a - b);
      return weekdays.length > 0 ? { ...base, weekdays } : null;
    }
    case "month":
      return { ...base, monthDay: draft.last ? LAST_DAY : day.getDate() };
    case "year":
      return { ...base, monthDay: day.getDate(), month: day.getMonth() + 1 };
  }
}

/**
 * Дата под правило: отмеченные дни или «в последний день месяца» её не
 * включают — ближайший подходящий день не раньше неё, час тот же. Как
 * «теперь по вторникам» в чате (§13.5).
 */
export function fitDay(draft: RepeatDraft, day: Date): Date {
  if (draft.every === "week" && draft.weekdays.length > 0) {
    for (let shift = 0; shift < 7; shift += 1) {
      const candidate = new Date(day);
      candidate.setDate(day.getDate() + shift);
      if (draft.weekdays.includes(isoWeekday(candidate))) {
        return shift === 0 ? day : candidate;
      }
    }
  }
  if (draft.every === "month" && draft.last) {
    const last = new Date(day);
    last.setDate(1);
    last.setMonth(day.getMonth() + 1);
    last.setDate(0);
    return last.getDate() === day.getDate() ? day : last;
  }
  return day;
}

// «В понедельник», но «во вторник».
const IN_WEEKDAY = ["в", "во", "в", "в", "в", "в", "в"];

/**
 * Первая строка подсказки под сроком, когда дату передвинуло правило:
 * куда и почему. Не двигалась — `null`.
 */
export function movedNote(draft: RepeatDraft, from: Date, to: Date): string | null {
  if (sameDay(from, to)) {
    return null;
  }
  if (draft.every === "month") {
    return `Дата передвинута на ${formatDate(to)} — последний день месяца.`;
  }
  const day = isoWeekday(from) - 1;
  return (
    `Дата передвинута на ${formatDay(to)} — ` +
    `${IN_WEEKDAY[day]} ${WEEKDAYS_ACCUSATIVE[day]} повтор не попадает.`
  );
}

/**
 * Слова вокруг поля шага в нужном падеже: «Каждую [1] неделю», «Каждые
 * [2] недели». Шаг не вписан — формы для одного.
 */
export function stepWords(every: Every, interval: number | null): [string, string] {
  const [each, one, few, many] = UNITS[every];
  const form = formOf(interval ?? 1);
  if (form === 1) {
    return [capitalized(each), one];
  }
  return ["Каждые", form === 2 ? few : many];
}

function capitalized(word: string): string {
  return word.charAt(0).toUpperCase() + word.slice(1);
}

/** Раз в секундах Unix, доли отброшены — так его сверяет база (§13.3). */
export function occurrenceSeconds(moment: Date): number {
  return Math.floor(moment.getTime() / 1000);
}
