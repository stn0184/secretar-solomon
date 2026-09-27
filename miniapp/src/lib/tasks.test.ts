/**
 * Группировка списка по сроку и разбор строки базы — чистые функции.
 *
 * Границы групп — `specs` 005: просрочено — день срока уже кончился (для
 * `day`) или момент прошёл (для `time`); сегодня — до конца дня; на неделе —
 * ближайшие семь дней; позже — дальше. Идеи и желания — своей группой внизу.
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { type Task, groupOf, groupTasks, parseTask } from "./tasks.ts";

// Среда, 30 сентября 2026, 12:00.
const now = new Date(2026, 8, 30, 12, 0);

function task(overrides: Partial<Task> = {}): Task {
  return {
    id: "t",
    title: "Задача",
    kind: "task",
    dueAt: null,
    duePrecision: null,
    priority: "normal",
    promise: null,
    people: [],
    needsReview: false,
    sourceMessageId: null,
    createdAt: new Date(2026, 8, 24, 9, 0),
    ...overrides,
  };
}

describe("groupOf", () => {
  it("идея и желание — внизу, даже со сроком", () => {
    assert.equal(groupOf(task({ kind: "idea", dueAt: new Date(2026, 9, 1), duePrecision: "day" }), now), "ideas");
    assert.equal(groupOf(task({ kind: "wish" }), now), "ideas");
  });

  it("без срока — «без срока»", () => {
    assert.equal(groupOf(task(), now), "none");
  });

  it("просрочено: момент прошёл или день кончился", () => {
    assert.equal(groupOf(task({ dueAt: new Date(2026, 8, 28, 10, 0), duePrecision: "time" }), now), "overdue");
    assert.equal(groupOf(task({ dueAt: new Date(2026, 8, 29, 18, 0), duePrecision: "day" }), now), "overdue");
    assert.equal(groupOf(task({ dueAt: new Date(2026, 8, 30, 9, 0), duePrecision: "time" }), now), "overdue");
  });

  it("сегодня: до конца дня, а для дня — пока не наступила полночь", () => {
    assert.equal(groupOf(task({ dueAt: new Date(2026, 8, 30, 15, 0), duePrecision: "time" }), now), "today");
    assert.equal(groupOf(task({ dueAt: new Date(2026, 8, 30, 18, 0), duePrecision: "day" }), now), "today");
    // 20:00 — 18:00 «того дня» прошли, но день ещё не кончился
    const evening = new Date(2026, 8, 30, 20, 0);
    assert.equal(groupOf(task({ dueAt: new Date(2026, 8, 30, 18, 0), duePrecision: "day" }), evening), "today");
    // пустая точность — как день
    assert.equal(groupOf(task({ dueAt: new Date(2026, 8, 30, 18, 0), duePrecision: null }), evening), "today");
  });

  it("на неделе — ближайшие семь дней, дальше — позже", () => {
    assert.equal(groupOf(task({ dueAt: new Date(2026, 9, 1, 18, 0), duePrecision: "day" }), now), "week");
    assert.equal(groupOf(task({ dueAt: new Date(2026, 9, 7, 18, 0), duePrecision: "day" }), now), "week");
    assert.equal(groupOf(task({ dueAt: new Date(2026, 9, 8, 9, 0), duePrecision: "time" }), now), "later");
  });
});

describe("groupTasks", () => {
  it("порядок групп фиксирован, пустые пропущены, внутри — как пришло", () => {
    const later = task({ id: "later", dueAt: new Date(2026, 9, 15), duePrecision: "day" });
    const overdue = task({ id: "overdue", dueAt: new Date(2026, 8, 28, 10, 0), duePrecision: "time" });
    const wish = task({ id: "wish", kind: "wish" });
    const week2 = task({ id: "week2", dueAt: new Date(2026, 9, 3, 11, 0), duePrecision: "time" });
    const week1 = task({ id: "week1", dueAt: new Date(2026, 9, 2), duePrecision: "day" });

    const groups = groupTasks([later, week2, overdue, wish, week1], now);

    assert.deepEqual(
      groups.map((g) => g.key),
      ["overdue", "week", "later", "ideas"],
    );
    assert.deepEqual(groups.map((g) => g.title), ["Просрочено", "На неделе", "Позже", "Идеи и желания"]);
    assert.deepEqual(groups[1]?.tasks.map((t) => t.id), ["week2", "week1"]);
  });

  it("ни одной задачи — ни одной группы", () => {
    assert.deepEqual(groupTasks([], now), []);
  });
});

describe("parseTask", () => {
  const row = {
    id: "6f1c",
    title: "Отправить расчёт",
    kind: "task",
    status: "active",
    due_at: "2026-09-30T10:00:00+00:00",
    due_precision: "time",
    priority: "high",
    promise: "mine",
    people: ["Кузнецов"],
    needs_review: false,
    source_message_id: "m1",
    created_at: "2026-09-28T05:02:00+00:00",
  };

  it("строка базы становится задачей с датами", () => {
    const parsed = parseTask(row);
    assert.ok(parsed);
    assert.equal(parsed.title, "Отправить расчёт");
    assert.equal(parsed.priority, "high");
    assert.equal(parsed.promise, "mine");
    assert.deepEqual(parsed.people, ["Кузнецов"]);
    assert.equal(parsed.dueAt?.toISOString(), "2026-09-30T10:00:00.000Z");
    assert.equal(parsed.duePrecision, "time");
    assert.equal(parsed.sourceMessageId, "m1");
  });

  it("пустой срок и незнакомые значения не ломают разбор", () => {
    const parsed = parseTask({ ...row, due_at: null, due_precision: null, promise: null, priority: "weird", kind: "other", people: null });
    assert.ok(parsed);
    assert.equal(parsed.dueAt, null);
    assert.equal(parsed.priority, "normal");
    assert.equal(parsed.kind, "task");
    assert.deepEqual(parsed.people, []);
  });

  it("без id или названия строка не годится", () => {
    assert.equal(parseTask({ ...row, title: 5 }), null);
    assert.equal(parseTask(null), null);
    assert.equal(parseTask({ ...row, id: undefined }), null);
  });
});
