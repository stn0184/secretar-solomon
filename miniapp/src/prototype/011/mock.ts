/**
 * Моки прототипа 011 (повторяющиеся задачи). Только для dev-страницы
 * `/prototype/011`: не из базы, не из сидов и тестов. Имена, тексты и даты
 * выдуманы. Уходят вместе с этапом (`prototype/011-repeat/README.md`).
 *
 * Правило повтора здесь — готовые строки: словами (как у бота, §13.7) и
 * коротко для списка. В реализации их считает `lib/repeat.ts` из правила
 * `repeat` задачи; строки ниже — предложение для его тестов.
 */

import type { Reminder, SourceMessage, Task } from "../../lib/tasks.ts";

/** «Сейчас» на всех экранах — вторник, 29 сентября 2026, 10:40. */
export const NOW = new Date(2026, 8, 29, 10, 40);

/** Правило, как его видит экран: словами — в карточке, коротко — в списке. */
export interface RepeatView {
  words: string;
  short: string;
}

/** Задача прототипа — задача приложения плюс повтор (`null` — разовая). */
export type ProtoTask = Task & { rep: RepeatView | null };

function task(
  id: string,
  title: string,
  dueAt: Date | null,
  extra: Partial<ProtoTask> = {},
): ProtoTask {
  return {
    id: `proto-011-${id}`,
    title,
    kind: "task",
    dueAt,
    duePrecision: dueAt ? "day" : null,
    priority: "normal",
    promise: null,
    people: [],
    needsReview: false,
    sourceMessageId: null,
    createdAt: new Date(2026, 8, 21, 9, 0),
    openQuestion: null,
    repeat: null,
    occurrenceAt: null,
    rep: null,
    ...extra,
  };
}

/* ── Список: повторяющиеся вперемешку с разовыми ── */

/** Раз прошёл без отметки: ждёт до начала следующего (четверг, 00:00). */
export const WATER = task("water", "Полить цветы в переговорной", new Date(2026, 8, 28, 18, 0), {
  rep: { words: "каждые 3 дня", short: "каждые 3 дня" },
});

export const STANDUP = task("standup", "Созвон с командой", new Date(2026, 8, 29, 11, 30), {
  duePrecision: "time",
  rep: { words: "по будням", short: "по будням" },
  sourceMessageId: "proto-011-msg-standup",
});

/** Тот же созвон после «Сделано»: база перевела его на следующий будний день. */
export const STANDUP_NEXT: ProtoTask = { ...STANDUP, dueAt: new Date(2026, 8, 30, 11, 30) };

const SERVICE = task("service", "Позвонить в сервис насчёт кондиционера", new Date(2026, 8, 29, 15, 0), {
  duePrecision: "time",
  priority: "high",
});

const METERS = task("meters", "Передать показания счётчиков", new Date(2026, 8, 30, 18, 0), {
  rep: { words: "в последний день месяца", short: "в последний день" },
});

export const REPORT = task("report", "Отправить отчёт Гончаровой", new Date(2026, 9, 5, 18, 0), {
  promise: "mine",
  people: ["Гончарова"],
  rep: { words: "каждый понедельник", short: "каждый пн" },
  sourceMessageId: "proto-011-msg-report",
});

const DRY = task("dry", "Забрать костюм из химчистки", new Date(2026, 9, 2, 18, 0));

const RENT = task("rent", "Заплатить за квартиру", new Date(2026, 9, 10, 18, 0), {
  rep: { words: "каждый месяц 10-го", short: "10-го" },
});

const BIRTHDAY = task("birthday", "Поздравить Веру Павловну с днём рождения", new Date(2026, 9, 14, 18, 0), {
  rep: { words: "каждый год 14 октября", short: "каждый год" },
});

const IDEA = task("idea", "Записаться на курс по фотографии", null, {
  kind: "idea",
  createdAt: new Date(2026, 8, 24, 20, 15),
});

/** Порядок — как отдаёт база (новые сверху); группы раскладывает `groupTasks`. */
export const LIST: ProtoTask[] = [WATER, STANDUP, SERVICE, METERS, REPORT, DRY, RENT, BIRTHDAY, IDEA];

/** Список после «Сделано» в карточке созвона: задача та же, срок — от базы. */
export const LIST_AFTER_DONE: ProtoTask[] = LIST.map((t) => (t.id === STANDUP.id ? STANDUP_NEXT : t));

/* ── Карточка созвона ── */

export const STANDUP_SOURCE: SourceMessage = {
  text: "По будням в полдвенадцатого созвон с командой, напоминай.",
  receivedAt: new Date(2026, 8, 21, 9, 0),
  kind: "voice",
  durationSeconds: 5,
};

export const STANDUP_REMINDERS: Reminder[] = [
  { id: "proto-011-r1", stage: "before", fireAt: new Date(2026, 8, 29, 10, 30), sentAt: new Date(2026, 8, 29, 10, 30) },
  { id: "proto-011-r2", stage: "due", fireAt: new Date(2026, 8, 29, 11, 30), sentAt: null },
];

/* ── Карточка и форма отчёта ── */

export const REPORT_SOURCE: SourceMessage = {
  text: "Каждый понедельник отправлять Гончаровой отчёт по продажам за неделю, я ей обещал.",
  receivedAt: new Date(2026, 8, 21, 9, 0),
  kind: "text",
  durationSeconds: null,
};

export const REPORT_REMINDERS: Reminder[] = [
  { id: "proto-011-r3", stage: "before", fireAt: new Date(2026, 9, 5, 9, 0), sentAt: null },
  { id: "proto-011-r4", stage: "due", fireAt: new Date(2026, 9, 5, 18, 0), sentAt: null },
];

/* ── Пустое: разовая задача без срока — повтор выбрать нельзя ── */

export const BARE = task("bare", "Разобрать коробки на балконе", null, {
  createdAt: new Date(2026, 8, 27, 19, 5),
  sourceMessageId: "proto-011-msg-bare",
});

export const BARE_SOURCE: SourceMessage = {
  text: "Разобрать коробки на балконе.",
  receivedAt: new Date(2026, 8, 27, 19, 5),
  kind: "text",
  durationSeconds: null,
};
