/**
 * Даты словами — те же слова, что у бота (`bot/src/solomon/texts.py`).
 *
 * Даты собираются конструктором в местном времени, и форматируются тоже
 * по местному: тест не зависит от пояса машины, на которой идёт.
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  countOpen,
  countOverdue,
  dateInputValue,
  formatDay,
  formatDue,
  formatMoment,
  formatRecorded,
  formatTime,
  formatTimeRange,
  formatWeekRange,
  hoursWords,
  isPart,
  isoWithOffset,
  momentFromInputs,
  PART_WORDS,
  partLabel,
  plural,
  timeInputValue,
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

  it("часть дня — словом, без часа её начала (§21.4)", () => {
    assert.equal(formatDue(new Date(2026, 8, 30, 8, 0), "morning", now), "сегодня утром");
    assert.equal(formatDue(new Date(2026, 8, 30, 12, 0), "afternoon", now), "сегодня днём");
    assert.equal(formatDue(new Date(2026, 8, 30, 18, 0), "evening", now), "сегодня вечером");
    assert.equal(
      formatDue(new Date(2026, 9, 9, 8, 0), "morning", now),
      "пятница, 9 октября, утром",
    );
  });
});

describe("часть дня", () => {
  it("слова частей — те же, что у бота", () => {
    assert.deepEqual(PART_WORDS, { morning: "утром", afternoon: "днём", evening: "вечером" });
    assert.equal(partLabel("morning"), "Утром");
    assert.equal(partLabel("afternoon"), "Днём");
    assert.equal(partLabel("evening"), "Вечером");
  });

  it("частью считаются только три значения", () => {
    assert.equal(isPart("morning"), true);
    assert.equal(isPart("afternoon"), true);
    assert.equal(isPart("evening"), true);
    assert.equal(isPart("day"), false);
    assert.equal(isPart("time"), false);
    assert.equal(isPart(null), false);
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

  it("надзаголовок «Дел» — сколько открытых и сколько просрочено", () => {
    assert.equal(countOpen(1), "1 открытое");
    assert.equal(countOpen(3), "3 открытых");
    assert.equal(countOpen(11), "11 открытых");
    assert.equal(countOpen(21), "21 открытое");
    assert.equal(countOverdue(1), "одно просрочено");
    assert.equal(countOverdue(2), "2 просрочены");
    assert.equal(countOverdue(5), "5 просрочено");
    assert.equal(countOverdue(21), "21 просрочено");
  });
});

describe("встреча с концом (§31.1)", () => {
  it("срок с часом и длительностью — «14:00–16:00», как у бота", () => {
    const at = new Date(2026, 8, 30, 14, 0);
    assert.equal(formatDue(at, "time", now, 120), "сегодня, 14:00–16:00");
    assert.equal(
      formatDue(new Date(2026, 9, 5, 9, 30), "time", now, 45),
      "понедельник, 5 октября, 09:30–10:15",
    );
  });

  it("без длительности и у срока без часа конца нет", () => {
    assert.equal(formatDue(new Date(2026, 8, 30, 14, 0), "time", now, null), "сегодня, 14:00");
    assert.equal(formatDue(new Date(2026, 9, 2, 18, 0), "day", now, 60), "пятница, 2 октября");
  });

  it("время крупно в карточке — начало и конец или одно начало", () => {
    assert.equal(formatTimeRange(new Date(2026, 8, 30, 14, 0), 120), "14:00–16:00");
    assert.equal(formatTimeRange(new Date(2026, 8, 30, 23, 30), 60), "23:30–00:30");
    assert.equal(formatTimeRange(new Date(2026, 8, 30, 14, 0), null), "14:00");
  });

  it("длительность словами — как в ответе бота: «2 ч 10 мин», «45 мин», «3 ч»", () => {
    assert.equal(hoursWords(130), "2 ч 10 мин");
    assert.equal(hoursWords(45), "45 мин");
    assert.equal(hoursWords(180), "3 ч");
    assert.equal(hoursWords(60), "1 ч");
  });
});

describe("formatWeekRange", () => {
  it("неделя с понедельника по воскресенье, месяц один раз", () => {
    // Четверг, 8 октября 2026 — неделя 5–11 октября.
    assert.equal(formatWeekRange(new Date(2026, 9, 8, 16, 40)), "5 – 11 октября");
    assert.equal(formatWeekRange(new Date(2026, 9, 5, 0, 1)), "5 – 11 октября");
    assert.equal(formatWeekRange(new Date(2026, 9, 11, 23, 59)), "5 – 11 октября");
  });

  it("неделя на стыке месяцев — оба месяца", () => {
    assert.equal(formatWeekRange(now), "28 сентября – 4 октября");
  });
});

describe("поля ввода срока (§11.3)", () => {
  it("день и час — цифрами для input date/time, по часам устройства", () => {
    const at = new Date(2026, 9, 5, 7, 5);
    assert.equal(dateInputValue(at), "2026-10-05");
    assert.equal(timeInputValue(at), "07:05");
  });

  it("срока нет — поля пустые", () => {
    assert.equal(dateInputValue(null), "");
    assert.equal(timeInputValue(null), "");
  });

  it("из полей — момент в поясе устройства", () => {
    assert.equal(
      momentFromInputs("2026-10-05", "12:00")?.getTime(),
      new Date(2026, 9, 5, 12, 0).getTime(),
    );
    assert.equal(
      momentFromInputs("2026-01-31", "00:00")?.getTime(),
      new Date(2026, 0, 31, 0, 0).getTime(),
    );
  });

  it("пустое и негодное — не момент", () => {
    const bad: [string, string][] = [
      ["", "12:00"],
      ["2026-10-05", ""],
      ["2026-02-30", "12:00"],
      ["2026-13-01", "12:00"],
      ["2026-10-05", "24:00"],
      ["2026-10-05", "12:60"],
      ["05.10.2026", "12:00"],
      ["2026-10-05", "12"],
    ];
    for (const [day, time] of bad) {
      assert.equal(momentFromInputs(day, time), null, `${day} ${time}`);
    }
  });

  it("момент со смещением: те же цифры часа и тот же миг", () => {
    const at = new Date(2026, 9, 5, 12, 0);
    const iso = isoWithOffset(at);
    assert.match(iso, /^2026-10-05T12:00:00[+-]\d{2}:\d{2}$/);
    assert.equal(new Date(iso).getTime(), at.getTime());
  });

  it("смещение берётся на сам день, а не на сегодня", () => {
    for (const at of [new Date(2026, 0, 15, 9, 30), new Date(2026, 6, 15, 23, 45)]) {
      assert.equal(new Date(isoWithOffset(at)).getTime(), at.getTime());
    }
  });
});
