/**
 * Повтор задачи в приложении (`techspec/13-repeat.md` §13.6–13.7): правило
 * словами и коротко, разбор строки базы, выбор в форме и подгонка даты под
 * правило. Чистые функции — на Node.
 *
 * Слова одни для бота и приложения: общие примеры — тот же список, что
 * `WORDS` в `bot/tests/test_repeat.py`.
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  NO_REPEAT,
  type Repeat,
  type RepeatDraft,
  chooseEvery,
  fitDay,
  intervalOf,
  isoWeekday,
  movedNote,
  occurrenceSeconds,
  parseRepeat,
  repeatDraftOf,
  repeatJson,
  repeatShort,
  repeatWords,
  ruleOf,
  sameChoice,
  stepWords,
} from "./repeat.ts";

/** Правило как у бота в `canonical()`: по понедельникам, шаг 1, без часа. */
function rule(overrides: Partial<Repeat> = {}): Repeat {
  return {
    every: "week",
    interval: 1,
    weekdays: [1],
    monthDay: null,
    month: null,
    time: null,
    ...overrides,
  };
}

function daily(interval = 1): Repeat {
  return rule({ every: "day", interval, weekdays: null });
}

function monthly(monthDay: number, interval = 1): Repeat {
  return rule({ every: "month", interval, weekdays: null, monthDay });
}

function yearly(monthDay: number, month: number, interval = 1): Repeat {
  return rule({ every: "year", interval, weekdays: null, monthDay, month });
}

function draft(overrides: Partial<RepeatDraft> = {}): RepeatDraft {
  return { ...NO_REPEAT, ...overrides };
}

// Понедельник, 5 октября 2026, без часа — в базе 18:00.
const MONDAY = new Date(2026, 9, 5, 18, 0);
const TUESDAY = new Date(2026, 9, 6, 18, 0);

/* ---------------------------------------------------------------- слова */

// Общие примеры бота и приложения (§13.7).
const WORDS: [Repeat, string][] = [
  [daily(), "каждый день"],
  [daily(2), "через день"],
  [daily(3), "каждые 3 дня"],
  [rule(), "каждый понедельник"],
  [rule({ weekdays: [3] }), "каждую среду"],
  [rule({ weekdays: [1, 2, 3, 4, 5] }), "по будням"],
  [rule({ weekdays: [6, 7] }), "по выходным"],
  [rule({ weekdays: [1, 5] }), "по понедельникам и пятницам"],
  [rule({ interval: 2, weekdays: [2] }), "каждые 2 недели по вторникам"],
  [monthly(10), "каждый месяц 10-го"],
  [monthly(-1), "в последний день месяца"],
  [monthly(5, 3), "каждые 3 месяца 5-го"],
  [yearly(5, 3), "каждый год 5 марта"],
];

describe("repeatWords", () => {
  for (const [value, expected] of WORDS) {
    it(`общий пример: ${expected}`, () => {
      assert.equal(repeatWords(value), expected);
    });
  }

  it("склонения и час — как у бота", () => {
    assert.equal(repeatWords(daily(5)), "каждые 5 дней");
    assert.equal(repeatWords(daily(21)), "каждый 21 день");
    assert.equal(repeatWords(daily(11)), "каждые 11 дней");
    assert.equal(repeatWords(rule({ weekdays: [7] })), "каждое воскресенье");
    assert.equal(repeatWords(rule({ weekdays: [5] })), "каждую пятницу");
    assert.equal(repeatWords(rule({ weekdays: [4] })), "каждый четверг");
    assert.equal(repeatWords(rule({ weekdays: [1, 3, 5] })), "по понедельникам, средам и пятницам");
    assert.equal(repeatWords(rule({ weekdays: [1, 2, 3, 4, 5, 6, 7] })), "каждый день");
    assert.equal(repeatWords(rule({ interval: 2, weekdays: [1, 2, 3, 4, 5] })), "каждые 2 недели по будням");
    assert.equal(repeatWords(rule({ interval: 5, weekdays: [6, 7] })), "каждые 5 недель по выходным");
    assert.equal(repeatWords(rule({ interval: 2, weekdays: [3] })), "каждые 2 недели по средам");
    assert.equal(repeatWords(monthly(-1, 3)), "каждые 3 месяца в последний день");
    assert.equal(repeatWords(yearly(29, 2, 2)), "каждые 2 года 29 февраля");
    assert.equal(repeatWords(yearly(1, 1, 5)), "каждые 5 лет 1 января");
    // Час в словах не звучит — он в сроке.
    assert.equal(repeatWords(rule({ time: "09:00" })), "каждый понедельник");
  });
});

describe("repeatShort", () => {
  it("короткие формы из прототипа", () => {
    assert.equal(repeatShort(rule()), "каждый пн");
    assert.equal(repeatShort(rule({ weekdays: [1, 5] })), "пн и пт");
    assert.equal(repeatShort(rule({ weekdays: [1, 2, 3, 4, 5] })), "по будням");
    assert.equal(repeatShort(rule({ weekdays: [6, 7] })), "по выходным");
    assert.equal(repeatShort(daily()), "каждый день");
    assert.equal(repeatShort(daily(2)), "через день");
    assert.equal(repeatShort(daily(3)), "каждые 3 дня");
    assert.equal(repeatShort(monthly(10)), "10-го");
    assert.equal(repeatShort(monthly(-1)), "в последний день");
    assert.equal(repeatShort(yearly(14, 10)), "каждый год");
  });

  it("род дня и список дней", () => {
    assert.equal(repeatShort(rule({ weekdays: [3] })), "каждую ср");
    assert.equal(repeatShort(rule({ weekdays: [7] })), "каждое вс");
    assert.equal(repeatShort(rule({ weekdays: [1, 3, 5] })), "пн, ср и пт");
    assert.equal(repeatShort(rule({ weekdays: [1, 2, 3, 4, 5, 6, 7] })), "каждый день");
  });

  it("шаг больше одного — словами целиком: короче не сказать без потери", () => {
    assert.equal(repeatShort(rule({ interval: 2, weekdays: [2] })), "каждые 2 недели по вторникам");
    assert.equal(repeatShort(monthly(5, 3)), "каждые 3 месяца 5-го");
    assert.equal(repeatShort(yearly(5, 3, 2)), "каждые 2 года");
  });
});

/* --------------------------------------------------------------- разбор */

describe("parseRepeat", () => {
  it("строка базы → правило в camelCase; дни по порядку", () => {
    assert.deepEqual(
      parseRepeat({ every: "week", interval: 2, weekdays: [5, 1], month_day: null, month: null, time: "09:00" }),
      rule({ interval: 2, weekdays: [1, 5], time: "09:00" }),
    );
    assert.deepEqual(
      parseRepeat({ every: "month", interval: 1, weekdays: null, month_day: -1, month: null, time: null }),
      monthly(-1),
    );
    assert.deepEqual(
      parseRepeat({ every: "year", interval: 1, month_day: 5, month: 3 }),
      yearly(5, 3),
    );
  });

  it("разовая и негодное правило — null", () => {
    for (const value of [
      null,
      undefined,
      "week",
      [],
      { every: "fortnight", interval: 1 },
      { every: "day", interval: 0 },
      { every: "day", interval: 100 },
      { every: "day", interval: 1.5 },
      { every: "week", interval: 1, weekdays: [] },
      { every: "week", interval: 1, weekdays: [8] },
      { every: "month", interval: 1, month_day: 32 },
      { every: "month", interval: 1 },
      { every: "year", interval: 1, month_day: 5, month: 13 },
      { every: "year", interval: 1, month_day: -1, month: 3 },
    ]) {
      assert.equal(parseRepeat(value), null, JSON.stringify(value));
    }
  });
});

describe("repeatJson", () => {
  it("ключи `edit_task` без часа: час база берёт из срока (§13.2)", () => {
    assert.deepEqual(repeatJson(rule({ weekdays: [1, 5], time: "09:00" })), {
      every: "week",
      interval: 1,
      weekdays: [1, 5],
      month_day: null,
      month: null,
    });
    assert.deepEqual(repeatJson(yearly(5, 3)), {
      every: "year",
      interval: 1,
      weekdays: null,
      month_day: 5,
      month: 3,
    });
  });
});

/* ---------------------------------------------------------------- форма */

describe("stepWords", () => {
  it("слова вокруг шага в нужном падеже", () => {
    assert.deepEqual(stepWords("week", 1), ["Каждую", "неделю"]);
    assert.deepEqual(stepWords("week", 2), ["Каждые", "недели"]);
    assert.deepEqual(stepWords("week", 5), ["Каждые", "недель"]);
    assert.deepEqual(stepWords("week", 21), ["Каждую", "неделю"]);
    assert.deepEqual(stepWords("day", 2), ["Каждые", "дня"]);
    assert.deepEqual(stepWords("day", 11), ["Каждые", "дней"]);
    assert.deepEqual(stepWords("month", 1), ["Каждый", "месяц"]);
    assert.deepEqual(stepWords("month", 3), ["Каждые", "месяца"]);
    assert.deepEqual(stepWords("year", 1), ["Каждый", "год"]);
    assert.deepEqual(stepWords("year", 5), ["Каждые", "лет"]);
  });

  it("шаг не вписан — формы для одного", () => {
    assert.deepEqual(stepWords("week", null), ["Каждую", "неделю"]);
  });
});

describe("intervalOf", () => {
  it("целое от 1 до 99; иначе null", () => {
    assert.equal(intervalOf("1"), 1);
    assert.equal(intervalOf(" 3 "), 3);
    assert.equal(intervalOf("99"), 99);
    for (const text of ["", "0", "100", "2.5", "-1", "два", "1e1"]) {
      assert.equal(intervalOf(text), null, text);
    }
  });
});

describe("isoWeekday", () => {
  it("понедельник — 1, воскресенье — 7", () => {
    assert.equal(isoWeekday(MONDAY), 1);
    assert.equal(isoWeekday(new Date(2026, 9, 11)), 7);
  });
});

describe("repeatDraftOf и sameChoice", () => {
  it("правило → выбор формы", () => {
    assert.deepEqual(repeatDraftOf(null), NO_REPEAT);
    assert.deepEqual(repeatDraftOf(rule({ interval: 2, weekdays: [2] })), draft({
      every: "week",
      interval: "2",
      weekdays: [2],
    }));
    assert.deepEqual(repeatDraftOf(monthly(-1)), draft({ every: "month", last: true }));
    assert.deepEqual(repeatDraftOf(monthly(10)), draft({ every: "month" }));
    assert.deepEqual(repeatDraftOf(yearly(5, 3)), draft({ every: "year" }));
    assert.deepEqual(repeatDraftOf(daily(3)), draft({ every: "day", interval: "3" }));
  });

  it("сравниваются только поля выбранного правила", () => {
    assert.ok(sameChoice(NO_REPEAT, draft({ weekdays: [1], interval: "7" })));
    assert.ok(sameChoice(draft({ every: "week", weekdays: [5, 1] }), draft({ every: "week", weekdays: [1, 5] })));
    assert.ok(sameChoice(draft({ every: "day", interval: "02" }), draft({ every: "day", interval: "2" })));
    assert.ok(sameChoice(draft({ every: "year", weekdays: [3] }), draft({ every: "year" })));
    assert.ok(!sameChoice(draft({ every: "week", weekdays: [1] }), draft({ every: "week", weekdays: [2] })));
    assert.ok(!sameChoice(draft({ every: "month" }), draft({ every: "month", last: true })));
    assert.ok(!sameChoice(draft({ every: "month" }), draft({ every: "month", interval: "3" })));
    assert.ok(!sameChoice(NO_REPEAT, draft({ every: "day" })));
  });
});

describe("chooseEvery", () => {
  it("недели — день из даты, шаг заново с одного", () => {
    assert.deepEqual(chooseEvery(draft({ interval: "3" }), "week", TUESDAY), draft({
      every: "week",
      weekdays: [2],
    }));
  });

  it("месяцы — число из даты, не последний день", () => {
    assert.deepEqual(chooseEvery(draft({ every: "week", weekdays: [1] }), "month", MONDAY), draft({
      every: "month",
    }));
  });

  it("то же правило ещё раз — выбор не меняется", () => {
    const current = draft({ every: "week", interval: "2", weekdays: [1, 5] });
    assert.equal(chooseEvery(current, "week", TUESDAY), current);
  });

  it("«Нет» — пустой выбор", () => {
    assert.deepEqual(chooseEvery(draft({ every: "week", weekdays: [1] }), "none", MONDAY), NO_REPEAT);
  });
});

describe("ruleOf", () => {
  it("недели — отмеченные дни по порядку", () => {
    assert.deepEqual(ruleOf(draft({ every: "week", interval: "2", weekdays: [5, 1] }), MONDAY), rule({
      interval: 2,
      weekdays: [1, 5],
    }));
  });

  it("месяцы — число из даты или последний день; годы — день и месяц из даты", () => {
    assert.deepEqual(ruleOf(draft({ every: "month", interval: "3" }), MONDAY), monthly(5, 3));
    assert.deepEqual(ruleOf(draft({ every: "month", last: true }), new Date(2026, 9, 31)), monthly(-1));
    assert.deepEqual(ruleOf(draft({ every: "year" }), MONDAY), yearly(5, 10));
    assert.deepEqual(ruleOf(draft({ every: "day", interval: "2" }), MONDAY), daily(2));
  });

  it("«Нет», неделя без дня и негодный шаг — правила нет", () => {
    assert.equal(ruleOf(NO_REPEAT, MONDAY), null);
    assert.equal(ruleOf(draft({ every: "week", weekdays: [] }), MONDAY), null);
    assert.equal(ruleOf(draft({ every: "day", interval: "0" }), MONDAY), null);
  });
});

describe("fitDay", () => {
  it("день не по правилу — ближайший подходящий не раньше него, час тот же", () => {
    const fitted = fitDay(draft({ every: "week", weekdays: [2] }), MONDAY);
    assert.equal(fitted.getTime(), TUESDAY.getTime());
    // Суббота, 10 октября, по будням — понедельник, 12-е.
    const saturday = new Date(2026, 9, 10, 9, 30);
    assert.equal(
      fitDay(draft({ every: "week", weekdays: [1, 2, 3, 4, 5] }), saturday).getTime(),
      new Date(2026, 9, 12, 9, 30).getTime(),
    );
  });

  it("день по правилу — тот же", () => {
    assert.equal(fitDay(draft({ every: "week", weekdays: [1, 5] }), MONDAY), MONDAY);
    assert.equal(fitDay(draft({ every: "month" }), MONDAY), MONDAY);
    assert.equal(fitDay(draft({ every: "year" }), MONDAY), MONDAY);
    assert.equal(fitDay(draft({ every: "day" }), MONDAY), MONDAY);
    assert.equal(fitDay(NO_REPEAT, MONDAY), MONDAY);
  });

  it("последний день месяца — конец того же месяца", () => {
    assert.equal(
      fitDay(draft({ every: "month", last: true }), MONDAY).getTime(),
      new Date(2026, 9, 31, 18, 0).getTime(),
    );
    assert.equal(
      fitDay(draft({ every: "month", last: true }), new Date(2027, 1, 3, 18, 0)).getTime(),
      new Date(2027, 1, 28, 18, 0).getTime(),
    );
    const last = new Date(2026, 9, 31, 18, 0);
    assert.equal(fitDay(draft({ every: "month", last: true }), last), last);
  });

  it("неделя без дня — дата не двигается", () => {
    assert.equal(fitDay(draft({ every: "week", weekdays: [] }), MONDAY), MONDAY);
  });
});

describe("movedNote", () => {
  it("дни недели: куда передвинута и почему", () => {
    assert.equal(
      movedNote(draft({ every: "week", weekdays: [2] }), MONDAY, TUESDAY),
      "Дата передвинута на вторник, 6 октября — в понедельник повтор не попадает.",
    );
    assert.equal(
      movedNote(draft({ every: "week", weekdays: [4] }), TUESDAY, new Date(2026, 9, 8, 18, 0)),
      "Дата передвинута на четверг, 8 октября — во вторник повтор не попадает.",
    );
  });

  it("последний день месяца", () => {
    assert.equal(
      movedNote(draft({ every: "month", last: true }), MONDAY, new Date(2026, 9, 31, 18, 0)),
      "Дата передвинута на 31 октября — последний день месяца.",
    );
  });

  it("не двигалась — подсказки нет", () => {
    assert.equal(movedNote(draft({ every: "week", weekdays: [1] }), MONDAY, MONDAY), null);
  });
});

describe("occurrenceSeconds", () => {
  it("раз в секундах Unix, доли отбрасываются — как в базе", () => {
    assert.equal(occurrenceSeconds(new Date("2026-10-05T13:00:00.999Z")), 1791205200);
    assert.equal(occurrenceSeconds(new Date("2026-10-05T13:00:00Z")), 1791205200);
  });
});
