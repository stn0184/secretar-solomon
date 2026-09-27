/**
 * Даты словами — те же слова, что у бота (`bot/src/solomon/texts.py`).
 *
 * Даты собираются конструктором в местном времени, и форматируются тоже
 * по местному: тест не зависит от пояса машины, на которой идёт.
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  countActive,
  countOverdue,
  formatDay,
  formatDue,
  formatMoment,
  formatRecorded,
  formatTime,
  plural,
} from "./format.ts";

// Среда, 30 сентября 2026, 12:00 — «сегодня» на экранах прототипа.
const now = new Date(2026, 8, 30, 12, 0);

describe("formatDay / formatTime", () => {
  it("называет день недели и число, как бот", () => {
    assert.equal(formatDay(new Date(2026, 8, 25)), "пятница, 25 сентября");
    assert.equal(formatDay(new Date(2026, 9, 3)), "суббота, 3 октября");
  });

  it("время — две цифры часа и минут", () => {
    assert.equal(formatTime(new Date(2026, 8, 25, 9, 5)), "09:05");
    assert.equal(formatTime(new Date(2026, 8, 25, 18, 0)), "18:00");
  });
});

describe("formatDue", () => {
  it("назван день — только день, час 18:00 не произносится", () => {
    assert.equal(formatDue(new Date(2026, 9, 2, 18, 0), "day", now), "пятница, 2 октября");
  });

  it("назван час — день и час", () => {
    assert.equal(
      formatDue(new Date(2026, 8, 28, 10, 0), "time", now),
      "понедельник, 28 сентября, 10:00",
    );
  });

  it("сегодняшний срок называется «сегодня»", () => {
    assert.equal(formatDue(new Date(2026, 8, 30, 15, 0), "time", now), "сегодня, 15:00");
    assert.equal(formatDue(new Date(2026, 8, 30, 18, 0), "day", now), "сегодня");
  });

  it("точность не задана — как день", () => {
    assert.equal(formatDue(new Date(2026, 9, 15, 18, 0), null, now), "четверг, 15 октября");
  });
});

describe("formatMoment", () => {
  it("момент всегда с часом: сообщение и напоминание", () => {
    assert.equal(
      formatMoment(new Date(2026, 8, 28, 10, 2), now),
      "понедельник, 28 сентября, 10:02",
    );
    assert.equal(formatMoment(new Date(2026, 8, 30, 15, 0), now), "сегодня, 15:00");
  });
});

describe("formatRecorded", () => {
  it("задача без срока — когда записана", () => {
    assert.equal(formatRecorded(new Date(2026, 8, 24, 9, 0)), "записано 24 сентября");
  });
});

describe("plural", () => {
  it("склоняет по русским правилам", () => {
    const word = (n: number) => plural(n, "задача", "задачи", "задач");
    assert.equal(word(1), "задача");
    assert.equal(word(2), "задачи");
    assert.equal(word(4), "задачи");
    assert.equal(word(5), "задач");
    assert.equal(word(11), "задач");
    assert.equal(word(12), "задач");
    assert.equal(word(21), "задача");
    assert.equal(word(22), "задачи");
    assert.equal(word(100), "задач");
  });

  it("подпись списка — сколько активных и сколько просрочено", () => {
    assert.equal(countActive(1), "1 активная");
    assert.equal(countActive(3), "3 активные");
    assert.equal(countActive(7), "7 активных");
    assert.equal(countOverdue(1), "одна просрочена");
    assert.equal(countOverdue(2), "2 просрочены");
    assert.equal(countOverdue(5), "5 просрочено");
  });
});
