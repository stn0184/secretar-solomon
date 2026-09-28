/**
 * Моки прототипа 009 (правка задачи). Только для dev-страницы
 * `/prototype/009`: не из базы, не из сидов и тестов. Имена, тексты и даты
 * выдуманы. Уходят вместе с этапом (`prototype/009-task-edit/README.md`).
 */

import type { Reminder, SourceMessage, Task } from "../../lib/tasks.ts";

/** «Сейчас» на всех экранах — понедельник, 28 сентября 2026, 10:40. */
export const NOW = new Date(2026, 8, 28, 10, 40);

/* ── Задача с пометкой и вопросом бота: бот не понял срок ── */

export const SOURCE: SourceMessage = {
  text: "Надо до пятницы отправить расчёт клиенту, Кузнецов просил, я обещал. Хотя, может, и в понедельник — посмотрим.",
  receivedAt: new Date(2026, 8, 28, 9, 10),
  kind: "voice",
  durationSeconds: 14,
};

/** Вопрос, который бот задал по этой задаче (не старше суток). */
export const QUESTION = {
  text: "Срок — пятница или понедельник?",
  askedAt: new Date(2026, 8, 28, 9, 11),
};

export const TASK_BEFORE: Task = {
  id: "proto-009-a",
  title: "Отправить расчёт клиенту",
  kind: "task",
  // день без часа — в базе 18:00 этого дня
  dueAt: new Date(2026, 9, 2, 18, 0),
  duePrecision: "day",
  priority: "normal",
  promise: "mine",
  people: ["Кузнецов"],
  needsReview: true,
  sourceMessageId: "proto-009-msg-a",
  createdAt: new Date(2026, 8, 28, 9, 10),
};

export const REMINDERS_BEFORE: Reminder[] = [
  { id: "proto-009-r1", stage: "before", fireAt: new Date(2026, 9, 2, 9, 0), sentAt: null },
  { id: "proto-009-r2", stage: "due", fireAt: new Date(2026, 9, 2, 18, 0), sentAt: null },
];

/* ── Та же задача после «Сохранить»: срок перенесён на понедельник, 12:00,
   приоритет поднят; пометка и вопрос сняты, напоминания пересчитала база ── */

export const TASK_AFTER: Task = {
  ...TASK_BEFORE,
  dueAt: new Date(2026, 9, 5, 12, 0),
  duePrecision: "time",
  priority: "high",
  needsReview: false,
};

export const REMINDERS_AFTER: Reminder[] = [
  { id: "proto-009-r3", stage: "before", fireAt: new Date(2026, 9, 5, 11, 0), sentAt: null },
  { id: "proto-009-r4", stage: "due", fireAt: new Date(2026, 9, 5, 12, 0), sentAt: null },
];

/* ── Пустое: задача, у которой, кроме сути, ничего нет ── */

export const BARE_SOURCE: SourceMessage = {
  text: "Позвонить в управляющую компанию про счётчики",
  receivedAt: new Date(2026, 8, 26, 19, 42),
  kind: "text",
  durationSeconds: null,
};

export const TASK_BARE: Task = {
  id: "proto-009-b",
  title: "Позвонить в управляющую компанию про счётчики",
  kind: "task",
  dueAt: null,
  duePrecision: null,
  priority: "normal",
  promise: null,
  people: [],
  needsReview: false,
  sourceMessageId: "proto-009-msg-b",
  createdAt: new Date(2026, 8, 26, 19, 42),
};
