/**
 * Часы по сферам в приложении (`techspec/32-redesign.md` §32.3,
 * `techspec/31-hours.md`): «Сегодня», «Неделя» и часы на «Сферах».
 *
 * Считает база — функция `sphere_hours`, одна на бота и приложение
 * (`techspec/03-schema.md` §3.18): встречи по длительности, сессии переписки,
 * пересечение внутри сферы — один раз, не прошедшее не считается. Отрезки дня
 * — `day_spans`: встречи и сессии переписки со сферой, без текста сообщений.
 * Здесь часов не досчитывают — только раскладывают ответ базы по экрану:
 * нить дня, строки недели, полосы и карточки сфер.
 *
 * Владельца приложение не передаёт — база берёт его из токена. Окна — по
 * часам устройства, как и все даты приложения (`format.ts`): «сегодня» — то,
 * что показывает телефон.
 *
 * Раскладка — чистые функции, проверяются на Node; чтение — через `query`,
 * отказ — текстом (`supabase.ts`).
 */

import type { SupabaseClient } from "@supabase/supabase-js";

import type { Fact } from "./facts.ts";
import { formatShortDate, formatTime, hoursWords, plural, sameDay, weekStart } from "./format.ts";
import { dateOrNull, recordOf } from "./parse.ts";
import { type Sphere, sphereName, sphereOf, spheresEyebrow } from "./spheres.ts";
import { type Db, failed, query } from "./supabase.ts";
import { type DayTask, type Task, loadDayTasks } from "./tasks.ts";

export type { DayTask } from "./tasks.ts";

/** Минуты сферы за окно; `sphereId` и `name` пусты — «без сферы». */
export interface SphereMinutes {
  sphereId: string | null;
  name: string | null;
  meetings: number;
  chat: number;
}

export type SpanKind = "meeting" | "chat";

/** Отрезок дня: встреча (её дело — `taskId`) или сессия переписки. Текста нет. */
export interface Span {
  kind: SpanKind;
  start: Date;
  end: Date;
  sphereId: string | null;
  taskId: string | null;
}

const MINUTE_MS = 60 * 1000;

/* ----------------------------------------------------------------- разбор */

function count(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : null;
}

function idOrNull(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

/** Строки `sphere_hours` → минуты сфер. Строка без минут — отказ всего ответа. */
export function parseSphereMinutes(rows: readonly unknown[]): SphereMinutes[] | null {
  const found: SphereMinutes[] = [];
  for (const row of rows) {
    const r = recordOf(row);
    const meetings = count(r?.meetings);
    const chat = count(r?.chat);
    if (!r || meetings === null || chat === null) {
      return null;
    }
    found.push({ sphereId: idOrNull(r.sphere_id), name: idOrNull(r.name), meetings, chat });
  }
  return found;
}

/** Строки `day_spans` → отрезки. Незнакомый вид или дата — отказ всего ответа. */
export function parseSpans(rows: readonly unknown[]): Span[] | null {
  const found: Span[] = [];
  for (const row of rows) {
    const r = recordOf(row);
    const start = dateOrNull(r?.starts_at);
    const end = dateOrNull(r?.ends_at);
    if (!r || (r.kind !== "meeting" && r.kind !== "chat") || !start || !end) {
      return null;
    }
    found.push({ kind: r.kind, start, end, sphereId: idOrNull(r.sphere_id), taskId: idOrNull(r.task_id) });
  }
  return found;
}

/* ----------------------------------------------------------------- цифры */

/** Полночь дня по часам устройства. */
export function startOfDay(moment: Date): Date {
  return new Date(moment.getFullYear(), moment.getMonth(), moment.getDate());
}

/** Полночь через `n` дней — по календарю, а не по 24 часам. */
export function addDays(day: Date, n: number): Date {
  return new Date(day.getFullYear(), day.getMonth(), day.getDate() + n);
}

/** Семь полуночей недели — с понедельника. */
export function weekDays(now: Date): Date[] {
  const monday = weekStart(now);
  return Array.from({ length: 7 }, (_, i) => addDays(monday, i));
}

/** Итог окна — сумма встреч и переписки всех сфер. */
export function totalMinutes(rows: readonly SphereMinutes[]): number {
  return rows.reduce((sum, row) => sum + row.meetings + row.chat, 0);
}

/**
 * Крупная цифра «5 ч 10 мин» по частям: нулевая часть не пишется — «45 мин»,
 * «3 ч», как в ответе бота (`hoursWords`).
 */
export function hoursParts(minutes: number): { hours: number | null; minutes: number | null } {
  const whole = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return { hours: whole > 0 ? whole : null, minutes: rest > 0 || whole === 0 ? rest : null };
}

/** Часы дня на «Неделе»: «5:25» — минуты двумя разрядами. */
export function clockWords(minutes: number): string {
  return `${Math.floor(minutes / 60)}:${String(minutes % 60).padStart(2, "0")}`;
}

/** «встречи 8 ч 30 мин · переписка 55 мин» — только ненулевые части. */
export function partsWords(meetings: number, chat: number): string {
  return [meetings > 0 ? `встречи ${hoursWords(meetings)}` : "", chat > 0 ? `переписка ${hoursWords(chat)}` : ""]
    .filter(Boolean)
    .join(" · ");
}

/** Длина объединения отрезков в минутах: пересечения — один раз. */
function unionMinutes(parts: readonly (readonly [number, number])[]): number {
  const sorted = [...parts].sort((a, b) => a[0] - b[0]);
  let total = 0;
  let piece: [number, number] | null = null;
  for (const [start, end] of sorted) {
    if (piece === null || start > piece[1]) {
      total += piece === null ? 0 : piece[1] - piece[0];
      piece = [start, end];
    } else {
      piece[1] = Math.max(piece[1], end);
    }
  }
  total += piece === null ? 0 : piece[1] - piece[0];
  return Math.round(total / MINUTE_MS);
}

/* --------------------------------------------------------------- «Сегодня» */

/**
 * Сколько встреч ещё впереди сегодня — их будущие части до конца дня,
 * пересечения один раз. Это план, а не часы: в итог дня не входит.
 */
export function aheadMinutes(spans: readonly Span[], now: Date, dayEnd: Date): number {
  const parts = spans.flatMap((span): [number, number][] => {
    const start = Math.max(span.start.getTime(), now.getTime());
    const end = Math.min(span.end.getTime(), dayEnd.getTime());
    return span.kind === "meeting" && start < end ? [[start, end]] : [];
  });
  return unionMinutes(parts);
}

/** Строка под крупной цифрой; часов нет — «Часов пока нет», а не ноль. */
export function todaySub(total: number, ahead: number): { text: string; ahead: string | null } {
  return {
    text: total > 0 ? "примерно — из встреч и переписки" : "Часов пока нет",
    ahead: ahead > 0 ? hoursWords(ahead) : null,
  };
}

/**
 * Высота капсулы нити по длительности — формула артборда 01: дело с часом
 * без длительности — точка 30, встреча — от 40 до 120.
 */
export function capsuleHeight(minutes: number | null): number {
  if (minutes === null) {
    return 30;
  }
  return Math.min(120, Math.max(40, Math.round(40 + (minutes - 30) * 0.32)));
}

/** Событие нити: встреча, дело с часом без длительности («точка») или переписка. */
export interface ThreadEvent {
  key: string;
  kind: "meeting" | "point" | "chat";
  start: Date;
  /** У точки — тот же момент, что начало. */
  end: Date;
  /** Минуты события; у точки — `null`. */
  minutes: number | null;
  sphereId: string | null;
  title: string;
  /** `done` — сделано (✓), `future` — ещё не началось, `past` — началось или прошло. */
  state: "done" | "past" | "future";
}

export type ThreadEntry =
  | { type: "event"; event: ThreadEvent }
  | { type: "gap"; key: string; minutes: number }
  | { type: "now"; at: Date };

/** «Свободно N» — только промежутки длиннее этого (на артборде 40 минут подписаны, 30 — нет). */
export const FREE_GAP_MINUTES = 30;

const KIND_ORDER: Record<ThreadEvent["kind"], number> = { meeting: 0, point: 1, chat: 2 };

function minutesBetween(from: Date, to: Date): number {
  return Math.round((to.getTime() - from.getTime()) / MINUTE_MS);
}

function later(a: Date, b: Date): Date {
  return a.getTime() >= b.getTime() ? a : b;
}

/**
 * Нить дня (артборд 01): встречи и дела с часом из дел дня, переписка — из
 * отрезков дня, обрезанная днём; по времени. Между событиями — «Свободно N»,
 * если промежуток длиннее получаса; линия «сейчас» — перед первым событием,
 * которое ещё не началось (всё прошло — в конце), промежуток после неё
 * считается от «сейчас». Событий нет — нити нет.
 */
export function dayThread(tasks: readonly DayTask[], spans: readonly Span[], now: Date): ThreadEntry[] {
  const dayStart = startOfDay(now);
  const dayEnd = addDays(dayStart, 1);
  const events: ThreadEvent[] = [];
  for (const { task, done } of tasks) {
    const start = task.dueAt;
    if (task.duePrecision !== "time" || !start || start < dayStart || start >= dayEnd) {
      continue;
    }
    const minutes = task.duration;
    events.push({
      key: task.id,
      kind: minutes ? "meeting" : "point",
      start,
      end: minutes ? new Date(start.getTime() + minutes * MINUTE_MS) : start,
      minutes,
      sphereId: task.sphereId,
      title: task.title,
      state: done ? "done" : start > now ? "future" : "past",
    });
  }
  for (const span of spans) {
    const start = later(span.start, dayStart);
    const end = span.end < dayEnd ? span.end : dayEnd;
    if (span.kind !== "chat" || start >= end) {
      continue;
    }
    events.push({
      key: `chat-${start.getTime()}-${span.sphereId ?? ""}`,
      kind: "chat",
      start,
      end,
      minutes: minutesBetween(start, end),
      sphereId: span.sphereId,
      title: "Переписка",
      state: "past",
    });
  }
  if (events.length === 0) {
    return [];
  }
  events.sort(
    (a, b) =>
      a.start.getTime() - b.start.getTime() || KIND_ORDER[a.kind] - KIND_ORDER[b.kind] || a.title.localeCompare(b.title),
  );

  const entries: ThreadEntry[] = [];
  let cursor: Date | null = null;
  let nowPlaced = false;
  const gap = (from: Date | null, to: Date) => {
    if (from !== null) {
      const minutes = minutesBetween(from, to);
      if (minutes > FREE_GAP_MINUTES) {
        entries.push({ type: "gap", key: `gap-${to.getTime()}`, minutes });
      }
    }
  };
  const placeNow = () => {
    gap(cursor, now);
    entries.push({ type: "now", at: now });
    nowPlaced = true;
    cursor = cursor === null ? now : later(cursor, now);
  };
  for (const event of events) {
    if (!nowPlaced && event.start > now) {
      placeNow();
    }
    gap(cursor, event.start);
    entries.push({ type: "event", event });
    cursor = cursor === null ? event.end : later(cursor, event.end);
  }
  if (!nowPlaced && now >= dayStart && now < dayEnd) {
    placeNow();
  }
  return entries;
}

/**
 * Подпись события: «14:00–16:00 · 2 ч · VoiceFin», у точки — «09:00 · без
 * длительности · Семья», у будущего — «· впереди». У переписки нет имени
 * собеседника и площадки: приложение знает о ней только время и сферу.
 */
export function eventMeta(event: ThreadEvent, spheres: readonly Sphere[]): string {
  const sphere = sphereName(sphereOf(event.sphereId, spheres));
  const when =
    event.minutes === null
      ? `${formatTime(event.start)} · без длительности`
      : `${formatTime(event.start)}–${formatTime(event.end)} · ${hoursWords(event.minutes)}`;
  return `${when} · ${sphere}${event.state === "future" ? " · впереди" : ""}`;
}

/** «Без часа» — открытые дела на сегодня без часа: день или часть дня, по сроку. */
export function noHourTasks(tasks: readonly DayTask[]): Task[] {
  return tasks
    .filter(({ task, done }) => !done && task.dueAt !== null && task.duePrecision !== "time")
    .map(({ task }) => task)
    .sort((a, b) => (a.dueAt?.getTime() ?? 0) - (b.dueAt?.getTime() ?? 0));
}

/** Что прочитано для «Сегодня»: часы дня, отрезки дня и дела дня. */
export interface TodayData {
  day: Date;
  rows: SphereMinutes[];
  spans: Span[];
  tasks: DayTask[];
}

/**
 * Показывать нечего — одобренное пустое (`design.md` §2): ни часов, ни
 * переписки, ни видимых дел на сегодня (сделанное дело без часа не видно).
 */
export function isTodayEmpty(today: TodayData): boolean {
  const dayEnd = addDays(today.day, 1);
  const talked = today.spans.some((span) => span.kind === "chat" && span.end > today.day && span.start < dayEnd);
  const timed = today.tasks.some(({ task }) => task.duePrecision === "time");
  return totalMinutes(today.rows) === 0 && !talked && !timed && noHourTasks(today.tasks).length === 0;
}

/* ---------------------------------------------------------------- «Неделя» */

const WEEKDAYS_SHORT = ["Вс", "Пн", "Вт", "Ср", "Чт", "Пт", "Сб"] as const;

/** Ось строки дня — с 7 до 22 часов (`design.md` §3), в минутах от полуночи. */
const AXIS_FROM = 7 * 60;
const AXIS_TO = 22 * 60;

/** Подписи оси и их место в процентах ширины. */
export const AXIS_MARKS: readonly { hour: number; left: number }[] = [8, 11, 14, 17, 20].map((hour) => ({
  hour,
  left: axisPercent(hour * 60),
}));

function axisPercent(minuteOfDay: number): number {
  return ((minuteOfDay - AXIS_FROM) / (AXIS_TO - AXIS_FROM)) * 100;
}

function clampPercent(value: number): number {
  return Math.min(100, Math.max(0, value));
}

export interface WeekBar {
  left: number;
  width: number;
  sphereId: string | null;
  kind: SpanKind;
  /** Встреча ещё не началась — контуром. */
  future: boolean;
}

export interface WeekRow {
  day: Date;
  weekday: string;
  date: string;
  today: boolean;
  future: boolean;
  bars: WeekBar[];
  /** Часы дня; у будущего дня — `null` («впереди»). */
  minutes: number | null;
  /** Линия «сейчас» в процентах — только у сегодня. */
  now: number | null;
}

/**
 * Строки «Недели» (артборд 02): день — полосы его отрезков по оси 7–22
 * (вне оси не видны, через полночь — в каждом своём дне), часы дня из базы,
 * у будущих — «впереди», у сегодня — линия «сейчас».
 */
export function weekRows(
  days: readonly Date[],
  spans: readonly Span[],
  dayMinutes: readonly (number | null)[],
  now: Date,
): WeekRow[] {
  const today = startOfDay(now);
  return days.map((day, index) => {
    const next = addDays(day, 1);
    const bars = spans.flatMap((span): WeekBar[] => {
      const start = Math.max(span.start.getTime(), day.getTime());
      const end = Math.min(span.end.getTime(), next.getTime());
      if (start >= end) {
        return [];
      }
      const left = clampPercent(axisPercent((start - day.getTime()) / MINUTE_MS));
      const right = clampPercent(axisPercent((end - day.getTime()) / MINUTE_MS));
      if (right <= left) {
        return [];
      }
      return [
        {
          left,
          width: right - left,
          sphereId: span.sphereId,
          kind: span.kind,
          future: span.kind === "meeting" && span.start >= now,
        },
      ];
    });
    bars.sort((a, b) => a.left - b.left);
    const isToday = sameDay(day, now);
    return {
      day,
      weekday: WEEKDAYS_SHORT[day.getDay()]!,
      date: formatShortDate(day),
      today: isToday,
      future: day > today,
      bars,
      minutes: dayMinutes[index] ?? null,
      now: isToday ? clampPercent(axisPercent((now.getTime() - day.getTime()) / MINUTE_MS)) : null,
    };
  });
}

export interface SphereBar {
  sphereId: string | null;
  name: string;
  minutes: number;
  meetings: number;
  chat: number;
  /** Длина полосы — доля от наибольшей сферы, в процентах. */
  width: number;
}

/** «По сферам» — полосы относительно наибольшей, в порядке базы. */
export function sphereBars(rows: readonly SphereMinutes[]): SphereBar[] {
  const max = Math.max(0, ...rows.map((row) => row.meetings + row.chat));
  return rows.map((row) => {
    const minutes = row.meetings + row.chat;
    return {
      sphereId: row.sphereId,
      name: row.name ?? "Без сферы",
      minutes,
      meetings: row.meetings,
      chat: row.chat,
      width: max > 0 ? (minutes / max) * 100 : 0,
    };
  });
}

/** Что прочитано для «Недели» и «Сфер»: часы недели, отрезки, часы по дням. */
export interface WeekData {
  days: Date[];
  rows: SphereMinutes[];
  spans: Span[];
  /** Часы каждого дня; будущие дни — `null`. */
  dayMinutes: (number | null)[];
}

/* ----------------------------------------------------------------- «Сферы» */

export interface SphereCardData {
  sphere: Sphere;
  /** Часы за неделю; не прочитались — `null`. */
  minutes: number | null;
  meetings: number;
  chat: number;
  /** Полоса сравнения в процентах от наибольшей; у всех ноль или часов нет — `null`. */
  width: number | null;
  /** Открытых дел сферы. */
  open: number;
  /** Первая запись «что Соломон знает»; записей нет или не прочитаны — `null`. */
  fact: string | null;
  /** Сколько записей ещё. */
  more: number;
}

/**
 * Карточки «Сфер» (артборд 03) — на каждую живую сферу по местам: часы недели,
 * полоса сравнения, открытые дела, первая запись памяти сферы и «ещё N».
 */
export function sphereCards(
  spheres: readonly Sphere[],
  week: readonly SphereMinutes[] | null,
  tasks: readonly Task[],
  facts: readonly Fact[] | null,
): SphereCardData[] {
  const totals = spheres.map((sphere) => week?.find((row) => row.sphereId === sphere.id) ?? null);
  const max = Math.max(0, ...totals.map((row) => (row ? row.meetings + row.chat : 0)));
  return spheres.map((sphere, index) => {
    const row = totals[index] ?? null;
    const minutes = week === null ? null : row ? row.meetings + row.chat : 0;
    const known = facts?.filter((fact) => fact.sphereId === sphere.id) ?? [];
    return {
      sphere,
      minutes,
      meetings: row?.meetings ?? 0,
      chat: row?.chat ?? 0,
      width: minutes === null || max === 0 ? null : (minutes / max) * 100,
      open: tasks.filter((task) => task.sphereId === sphere.id).length,
      fact: known[0]?.text ?? null,
      more: Math.max(0, known.length - 1),
    };
  });
}

/** «3 открытых дела · встречи 8 ч 30 мин · переписка 55 мин»; нулей не пишем — словами. */
export function cardMeta(card: SphereCardData): string {
  const open = card.open > 0 ? `${card.open} ${plural(card.open, "открытое дело", "открытых дела", "открытых дел")}` : "открытых дел нет";
  if (card.minutes === null) {
    return open;
  }
  return `${open} · ${card.minutes > 0 ? partsWords(card.meetings, card.chat) : "часов за неделю нет"}`;
}

/** Часы «без сферы» за неделю — у карточки «Без сферы и о себе»; не прочитались — `null`. */
export function unspheredMinutes(week: readonly SphereMinutes[] | null): number | null {
  if (week === null) {
    return null;
  }
  const row = week.find((item) => item.sphereId === null);
  return row ? row.meetings + row.chat : 0;
}

/** «4 из 12 · часы за неделю». */
export function spheresHoursEyebrow(count: number): string {
  return `${spheresEyebrow(count)} · часы за неделю`;
}

/* ------------------------------------------------------------------- база */

export type HoursResult = { ok: true; rows: SphereMinutes[] } | { ok: false; message: string };
export type SpansResult = { ok: true; spans: Span[] } | { ok: false; message: string };
export type TodayResult = { ok: true; today: TodayData } | { ok: false; message: string };
export type WeekResult = { ok: true; week: WeekData } | { ok: false; message: string };

/** Часы сфер за `[start, end)` — `sphere_hours`; владелец — из токена. */
export async function loadSphereHours(db: Db, start: Date, end: Date): Promise<HoursResult> {
  const result = await query(db, (client: SupabaseClient) =>
    client.rpc("sphere_hours", { window_start: start.toISOString(), window_end: end.toISOString() }),
  );
  if (!result.ok) {
    return failed("Не получилось прочитать часы", result);
  }
  const rows = parseSphereMinutes(Array.isArray(result.data) ? result.data : []);
  if (rows === null) {
    return { ok: false, message: "Не получилось прочитать часы. База вернула сферу без минут." };
  }
  return { ok: true, rows };
}

/** Отрезки, задевающие `[start, end)`, — `day_spans`; владелец — из токена. */
export async function loadDaySpans(db: Db, start: Date, end: Date): Promise<SpansResult> {
  const result = await query(db, (client: SupabaseClient) =>
    client.rpc("day_spans", { span_start: start.toISOString(), span_end: end.toISOString() }),
  );
  if (!result.ok) {
    return failed("Не получилось прочитать день", result);
  }
  const spans = parseSpans(Array.isArray(result.data) ? result.data : []);
  if (spans === null) {
    return { ok: false, message: "Не получилось прочитать день. База вернула отрезок без времени." };
  }
  return { ok: true, spans };
}

/** «Сегодня»: часы и отрезки дня и дела дня — разом; отказ любого — текст. */
export async function loadToday(db: Db, now: Date): Promise<TodayResult> {
  const day = startOfDay(now);
  const next = addDays(day, 1);
  const [hours, spans, tasks] = await Promise.all([
    loadSphereHours(db, day, next),
    loadDaySpans(db, day, next),
    loadDayTasks(db, day, next),
  ]);
  if (!hours.ok) {
    return hours;
  }
  if (!spans.ok) {
    return spans;
  }
  if (!tasks.ok) {
    return tasks;
  }
  return { ok: true, today: { day, rows: hours.rows, spans: spans.spans, tasks: tasks.tasks } };
}

/**
 * «Неделя» и «Сферы»: часы недели, часы каждого прошедшего дня и сегодня
 * (будущим дням спрашивать нечего), отрезки недели — разом.
 */
export async function loadWeek(db: Db, now: Date): Promise<WeekResult> {
  const days = weekDays(now);
  const monday = days[0]!;
  const end = addDays(monday, 7);
  const today = startOfDay(now);
  const past = days.filter((day) => day <= today);
  const [week, spans, ...perDay] = await Promise.all([
    loadSphereHours(db, monday, end),
    loadDaySpans(db, monday, end),
    ...past.map((day) => loadSphereHours(db, day, addDays(day, 1))),
  ]);
  if (!week.ok) {
    return week;
  }
  if (!spans.ok) {
    return spans;
  }
  const dayMinutes: (number | null)[] = days.map(() => null);
  for (const [index, result] of perDay.entries()) {
    if (!result.ok) {
      return result;
    }
    dayMinutes[index] = totalMinutes(result.rows);
  }
  return { ok: true, week: { days, rows: week.rows, spans: spans.spans, dayMinutes } };
}
