/**
 * Моки прототипа 036 — понятный список дел. Всё выдумано: сферы, люди,
 * дела и часы; к базе, сидам и тестам проекта отношения не имеют.
 *
 * «Сейчас» на всех экранах — вторник, 13 октября 2026, 10:40. Порядок дел
 * внутри группы — как пришло бы из базы (новые сверху), его этап не меняет.
 */

import type { SphereMinutes, Span, TodayData } from "../../lib/hours.ts";
import type { Sphere } from "../../lib/spheres.ts";
import type { DuePrecision, Task, TaskKind } from "../../lib/tasks.ts";

export const NOW = new Date(2026, 9, 13, 10, 40);

export const SPHERES: Sphere[] = [
  { id: "s-vf", name: "VoiceFin", place: 1 },
  { id: "s-rv", name: "РЕЙВА", place: 2 },
  { id: "s-fam", name: "Семья", place: 3 },
];

/** День октября 2026 и час: день без часа в базе стоит на 18:00, утро — на 9:00. */
function at(day: number, hour: number, minute = 0): Date {
  return new Date(2026, 9, day, hour, minute);
}

function task(
  id: string,
  title: string,
  extra: Partial<Task> & { dueAt?: Date | null; duePrecision?: DuePrecision | null; kind?: TaskKind } = {},
): Task {
  return {
    id,
    title,
    kind: "task",
    dueAt: null,
    duePrecision: null,
    priority: "normal",
    promise: null,
    people: [],
    needsReview: false,
    sourceMessageId: null,
    createdAt: at(1, 12),
    openQuestion: null,
    repeat: null,
    occurrenceAt: null,
    sphereId: null,
    duration: null,
    ...extra,
  };
}

/* ── Дела: семнадцать открытых, по одному-два на каждый случай ── */

const OVERDUE_CONTRACT = task("o1", "Отправить Игорю договор на подпись", {
  dueAt: at(9, 18),
  duePrecision: "day",
  priority: "high",
  promise: "mine",
  people: ["Игорь"],
  sphereId: "s-rv",
});

const OVERDUE_DENTIST = task("o2", "Записать Машу к стоматологу", {
  dueAt: at(12, 18),
  duePrecision: "day",
  sphereId: "s-fam",
});

const TODAY_CALL = task("t1", "Созвон с командой РЕЙВА", {
  dueAt: at(13, 15),
  duePrecision: "time",
  duration: 60,
  sphereId: "s-rv",
  repeat: { every: "week", interval: 1, weekdays: [2], monthDay: null, month: null, time: "15:00" },
});

const TODAY_NEWSLETTER = task("t2", "Согласовать текст рассылки", {
  dueAt: at(13, 18),
  duePrecision: "day",
  priority: "high",
  promise: "mine",
  people: ["Олег"],
  sphereId: "s-vf",
});

const TODAY_SERVICE = task("t3", "Позвонить в сервис насчёт машины", {
  dueAt: at(13, 18),
  duePrecision: "day",
  needsReview: true,
});

const TODAY_CAT = task("t4", "Купить корм коту", {
  dueAt: at(13, 9),
  duePrecision: "morning",
  sphereId: "s-fam",
});

const TODAY_PARCEL = task("t5", "Забрать посылку на почте", {
  dueAt: at(13, 18),
  duePrecision: "evening",
});

const TODAY_COFFEE = task("t6", "Встреча с Игорем в кофейне на Ленина", {
  dueAt: at(13, 18, 30),
  duePrecision: "time",
});

const TOMORROW_MOCKUPS = task("m1", "Макеты лендинга", {
  dueAt: at(14, 18),
  duePrecision: "day",
  promise: "to_me",
  people: ["Олег"],
  sphereId: "s-rv",
});

const TOMORROW_ACCOUNTANT = task("m2", "Встреча с бухгалтером", {
  dueAt: at(14, 11, 30),
  duePrecision: "time",
  duration: 60,
  sphereId: "s-vf",
});

const WEEK_SUPPLIER = task("w1", "Ответить поставщику по срокам поставки", {
  dueAt: at(15, 18),
  duePrecision: "day",
  needsReview: true,
  sphereId: "s-rv",
});

const WEEK_DECK = task("w2", "Подготовить презентацию для инвесторов", {
  dueAt: at(16, 18),
  duePrecision: "day",
  priority: "high",
  sphereId: "s-vf",
});

const WEEK_GIFT = task("w3", "Купить подарок Свете на день рождения", {
  dueAt: at(17, 18),
  duePrecision: "day",
  sphereId: "s-fam",
});

const LATER_INSURANCE = task("l1", "Продлить страховку на машину", {
  dueAt: at(26, 18),
  duePrecision: "day",
});

const NONE_LAWYER = task("n1", "Контакты юриста по аренде", {
  promise: "to_me",
  people: ["Марина"],
  sphereId: "s-rv",
});

const NONE_MAIL = task("n2", "Разобрать почту за сентябрь");

const IDEA_EXPORT = task("i1", "Выгрузка отчётов в Excel", {
  kind: "idea",
  sphereId: "s-vf",
});

/** Открытые дела со сферами — как пришли бы из базы. */
export const TASKS: Task[] = [
  TODAY_CALL,
  TODAY_NEWSLETTER,
  TODAY_COFFEE,
  TODAY_SERVICE,
  TODAY_CAT,
  TODAY_PARCEL,
  OVERDUE_CONTRACT,
  OVERDUE_DENTIST,
  TOMORROW_MOCKUPS,
  TOMORROW_ACCOUNTANT,
  WEEK_SUPPLIER,
  WEEK_DECK,
  WEEK_GIFT,
  LATER_INSURANCE,
  NONE_LAWYER,
  NONE_MAIL,
  IDEA_EXPORT,
];

/** Те же дела у владельца, который сфер ещё не завёл. */
export const TASKS_NO_SPHERES: Task[] = TASKS.map((t) => ({ ...t, sphereId: null }));

/* ── «Сегодня»: часы, отрезки и дела дня ── */

const STANDUP = task("d1", "Планёрка VoiceFin", {
  dueAt: at(13, 9, 30),
  duePrecision: "time",
  duration: 45,
  sphereId: "s-vf",
});

const SPANS: Span[] = [
  { kind: "chat", start: at(13, 8, 40), end: at(13, 9, 10), sphereId: "s-vf", taskId: null },
  { kind: "meeting", start: at(13, 9, 30), end: at(13, 10, 15), sphereId: "s-vf", taskId: "d1" },
  { kind: "chat", start: at(13, 10, 15), end: at(13, 10, 35), sphereId: "s-rv", taskId: null },
  { kind: "meeting", start: at(13, 15), end: at(13, 16), sphereId: "s-rv", taskId: "t1" },
];

const ROWS: SphereMinutes[] = [
  { sphereId: "s-vf", name: "VoiceFin", meetings: 45, chat: 30 },
  { sphereId: "s-rv", name: "РЕЙВА", meetings: 0, chat: 20 },
];

export const TODAY: TodayData = {
  day: at(13, 0),
  rows: ROWS,
  spans: SPANS,
  tasks: [
    { task: STANDUP, done: true },
    { task: TODAY_CALL, done: false },
    { task: TODAY_NEWSLETTER, done: false },
    { task: TODAY_COFFEE, done: false },
    { task: TODAY_SERVICE, done: false },
    { task: TODAY_CAT, done: false },
    { task: TODAY_PARCEL, done: false },
  ],
};

/** Тот же день без сфер: часы и отрезки — «без сферы». */
export const TODAY_NO_SPHERES: TodayData = {
  day: TODAY.day,
  rows: [{ sphereId: null, name: null, meetings: 45, chat: 50 }],
  spans: SPANS.map((s) => ({ ...s, sphereId: null })),
  tasks: TODAY.tasks.map(({ task: t, done }) => ({ task: { ...t, sphereId: null }, done })),
};
