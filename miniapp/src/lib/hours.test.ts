/**
 * Часы по сферам в приложении (`techspec/32-redesign.md` §32.3): разбор
 * ответов `sphere_hours` и `day_spans`, нить «Сегодня», строки «Недели»,
 * полосы и карточки сфер — чистые функции; вызовы базы — с подменённым
 * клиентом. Считает часы база; здесь — только раскладка того, что она
 * отдала, по экрану.
 *
 * «Сегодня» — четверг, 8 октября 2026, сейчас 16:40, как на артбордах
 * `prototype/archive/000-redesign`. Сферы и люди выдуманные: VoiceFin, РЕЙВА,
 * Игорь, Олег.
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import type { SupabaseClient } from "@supabase/supabase-js";

import type { Fact } from "./facts.ts";
import {
  type DayTask,
  type Span,
  type SphereMinutes,
  type ThreadEntry,
  aheadMinutes,
  capsuleHeight,
  cardMeta,
  clockWords,
  dayThread,
  eventMeta,
  hoursParts,
  isTodayEmpty,
  loadDaySpans,
  loadSphereHours,
  loadToday,
  loadWeek,
  noHourTasks,
  parseSpans,
  parseSphereMinutes,
  partsWords,
  sphereBars,
  sphereCards,
  spheresHoursEyebrow,
  todaySub,
  totalMinutes,
  unspheredMinutes,
  weekDays,
  weekRows,
} from "./hours.ts";
import type { Session } from "./session.ts";
import type { Sphere } from "./spheres.ts";
import type { Db } from "./supabase.ts";
import type { Task } from "./tasks.ts";

// Четверг, 8 октября 2026, 16:40 — по часам устройства.
const NOW = new Date(2026, 9, 8, 16, 40);

function at(hour: number, minute = 0, day = 8): Date {
  return new Date(2026, 9, day, hour, minute);
}

const VOICEFIN: Sphere = { id: "vf", name: "VoiceFin", place: 1 };
const REIVA: Sphere = { id: "rv", name: "РЕЙВА", place: 2 };
const FAMILY: Sphere = { id: "fam", name: "Семья", place: 3 };
const SPORT: Sphere = { id: "sp", name: "Спорт", place: 4 };
const SPHERES = [VOICEFIN, REIVA, FAMILY, SPORT];

function task(overrides: Partial<Task> = {}): Task {
  return {
    id: "t",
    title: "Дело",
    kind: "task",
    dueAt: null,
    duePrecision: null,
    priority: "normal",
    promise: null,
    people: [],
    needsReview: false,
    sourceMessageId: null,
    createdAt: new Date(2026, 9, 1, 9, 0),
    openQuestion: null,
    repeat: null,
    occurrenceAt: null,
    sphereId: null,
    duration: null,
    ...overrides,
  };
}

function meeting(id: string, title: string, start: Date, duration: number | null, sphereId: string | null, done = false): DayTask {
  return { task: task({ id, title, dueAt: start, duePrecision: "time", duration, sphereId }), done };
}

function chat(start: Date, end: Date, sphereId: string | null): Span {
  return { kind: "chat", start, end, sphereId, taskId: null };
}

function meetingSpan(start: Date, end: Date, sphereId: string | null, taskId = "m"): Span {
  return { kind: "meeting", start, end, sphereId, taskId };
}

function minutes(sphereId: string | null, name: string | null, meetings: number, chatMinutes: number): SphereMinutes {
  return { sphereId, name, meetings, chat: chatMinutes };
}

/** День артборда 01: встречи, звонок, переписка, ужин впереди и лампочка без часа. */
const DAY_TASKS: DayTask[] = [
  meeting("train", "Тренировка", at(7, 30), 60, "sp", true),
  meeting("mom", "Позвонить маме", at(9), null, "fam", true),
  meeting("plan", "Планёрка с отделом продаж", at(10), 60, "rv"),
  meeting("igor", "Созвон с Игорем", at(14), 120, "vf"),
  meeting("dinner", "Ужин с родителями", at(19), 90, "fam"),
  { task: task({ id: "bulb", title: "Купить лампочку", dueAt: at(18), duePrecision: "day" }), done: false },
];
const DAY_CHATS: Span[] = [chat(at(11, 15), at(11, 50), "rv"), chat(at(12, 30), at(12, 50), null), chat(at(16, 10), at(16, 25), "vf")];

/** Нить коротко: событие — название, промежуток — «gap N», линия — «now». */
function brief(entries: readonly ThreadEntry[]): string[] {
  return entries.map((entry) => {
    if (entry.type === "gap") {
      return `gap ${entry.minutes}`;
    }
    if (entry.type === "now") {
      return "now";
    }
    return `${entry.event.title} ${entry.event.state}`;
  });
}

function near(actual: number, expected: number, label: string): void {
  assert.ok(Math.abs(actual - expected) < 0.01, `${label}: ${actual} вместо ${expected}`);
}

describe("parseSphereMinutes", () => {
  it("строки базы — минуты сфер, «без сферы» с пустыми id и названием", () => {
    assert.deepEqual(
      parseSphereMinutes([
        { sphere_id: "vf", name: "VoiceFin", meetings: 120, chat: 10 },
        { sphere_id: null, name: null, meetings: 0, chat: 25 },
      ]),
      [minutes("vf", "VoiceFin", 120, 10), minutes(null, null, 0, 25)],
    );
  });

  it("строка без минут или не строка — отказ всего ответа", () => {
    assert.equal(parseSphereMinutes([{ sphere_id: "vf", name: "VoiceFin", meetings: "час", chat: 0 }]), null);
    assert.equal(parseSphereMinutes([{ sphere_id: "vf", name: "VoiceFin", meetings: 10 }]), null);
    assert.equal(parseSphereMinutes(["строка"]), null);
  });
});

describe("parseSpans", () => {
  it("встречи и сессии переписки — время и сфера, без текста", () => {
    assert.deepEqual(
      parseSpans([
        { kind: "meeting", starts_at: "2026-10-08T09:00:00Z", ends_at: "2026-10-08T11:00:00Z", sphere_id: "vf", task_id: "igor" },
        { kind: "chat", starts_at: "2026-10-08T11:10:00Z", ends_at: "2026-10-08T11:25:00Z", sphere_id: null, task_id: null },
      ]),
      [
        meetingSpan(new Date("2026-10-08T09:00:00Z"), new Date("2026-10-08T11:00:00Z"), "vf", "igor"),
        chat(new Date("2026-10-08T11:10:00Z"), new Date("2026-10-08T11:25:00Z"), null),
      ],
    );
  });

  it("незнакомый вид или дата — отказ всего ответа", () => {
    assert.equal(parseSpans([{ kind: "call", starts_at: "2026-10-08T09:00:00Z", ends_at: "2026-10-08T10:00:00Z" }]), null);
    assert.equal(parseSpans([{ kind: "chat", starts_at: "вчера", ends_at: "2026-10-08T10:00:00Z" }]), null);
  });
});

describe("цифры часов", () => {
  it("итог — сумма встреч и переписки всех сфер", () => {
    assert.equal(totalMinutes([minutes("vf", "VoiceFin", 120, 15), minutes(null, null, 0, 20)]), 155);
    assert.equal(totalMinutes([]), 0);
  });

  it("крупная цифра: нулевая часть не пишется, как у бота", () => {
    assert.deepEqual(hoursParts(310), { hours: 5, minutes: 10 });
    assert.deepEqual(hoursParts(45), { hours: null, minutes: 45 });
    assert.deepEqual(hoursParts(180), { hours: 3, minutes: null });
  });

  it("часы дня на «Неделе» — «5:25», минуты двумя разрядами", () => {
    assert.equal(clockWords(325), "5:25");
    assert.equal(clockWords(605), "10:05");
    assert.equal(clockWords(0), "0:00");
  });

  it("разбивка — только ненулевые части", () => {
    assert.equal(partsWords(510, 55), "встречи 8 ч 30 мин · переписка 55 мин");
    assert.equal(partsWords(180, 0), "встречи 3 ч");
    assert.equal(partsWords(0, 85), "переписка 1 ч 25 мин");
  });
});

describe("weekDays", () => {
  it("семь полуночей с понедельника", () => {
    const days = weekDays(NOW);
    assert.equal(days.length, 7);
    assert.deepEqual(days[0], new Date(2026, 9, 5));
    assert.deepEqual(days[6], new Date(2026, 9, 11));
  });

  it("в воскресенье неделя та же, с прошлого понедельника", () => {
    assert.deepEqual(weekDays(new Date(2026, 9, 11, 23, 0))[0], new Date(2026, 9, 5));
  });
});

describe("«Сегодня»", () => {
  it("под цифрой — «примерно» и сколько встреч ещё впереди", () => {
    assert.deepEqual(todaySub(310, 90), { text: "примерно — из встреч и переписки", ahead: "1 ч 30 мин" });
    assert.deepEqual(todaySub(310, 0), { text: "примерно — из встреч и переписки", ahead: null });
    assert.deepEqual(todaySub(0, 60), { text: "Часов пока нет", ahead: "1 ч" });
  });

  it("впереди — будущие части встреч до конца дня, пересечения один раз", () => {
    const spans = [
      meetingSpan(at(16), at(17, 10), "vf"),
      meetingSpan(at(19), at(20, 30), "fam"),
      meetingSpan(at(20), at(21), "vf"),
      meetingSpan(at(23, 30), at(1, 0, 9), "rv"),
      chat(at(16, 30), at(16, 50), "vf"),
      meetingSpan(at(9), at(10), "rv"),
    ];
    assert.equal(aheadMinutes(spans, NOW, at(0, 0, 9)), 30 + 120 + 30);
    assert.equal(aheadMinutes([], NOW, at(0, 0, 9)), 0);
  });

  it("капсула растёт с длительностью, как на артборде: точка — 30, от 40 до 120", () => {
    assert.equal(capsuleHeight(null), 30);
    assert.equal(capsuleHeight(60), 50);
    assert.equal(capsuleHeight(120), 69);
    assert.equal(capsuleHeight(35), 42);
    assert.equal(capsuleHeight(15), 40);
    assert.equal(capsuleHeight(600), 120);
  });

  it("нить дня артборда: события по времени, «Свободно» больше получаса, «сейчас» перед будущим", () => {
    const thread = dayThread(DAY_TASKS, DAY_CHATS, NOW);
    assert.deepEqual(brief(thread), [
      "Тренировка done",
      "Позвонить маме done",
      "gap 60",
      "Планёрка с отделом продаж past",
      "Переписка past",
      "gap 40",
      "Переписка past",
      "gap 70",
      "Созвон с Игорем past",
      "Переписка past",
      "now",
      "gap 140",
      "Ужин с родителями future",
    ]);
  });

  it("встречи — с длительностью, звонок без конца — точкой, переписка — без имени собеседника", () => {
    const thread = dayThread(DAY_TASKS, DAY_CHATS, NOW);
    const events = thread.flatMap((entry) => (entry.type === "event" ? [entry.event] : []));
    assert.deepEqual(
      events.map((event) => [event.kind, event.minutes]),
      [
        ["meeting", 60],
        ["point", null],
        ["meeting", 60],
        ["chat", 35],
        ["chat", 20],
        ["meeting", 120],
        ["chat", 15],
        ["meeting", 90],
      ],
    );
    assert.deepEqual(
      events.map((event) => eventMeta(event, SPHERES)),
      [
        "07:30–08:30 · 1 ч · Спорт",
        "09:00 · без длительности · Семья",
        "10:00–11:00 · 1 ч · РЕЙВА",
        "11:15–11:50 · 35 мин · РЕЙВА",
        "12:30–12:50 · 20 мин · Без сферы",
        "14:00–16:00 · 2 ч · VoiceFin",
        "16:10–16:25 · 15 мин · VoiceFin",
        "19:00–20:30 · 1 ч 30 мин · Семья · впереди",
      ],
    );
  });

  it("идущая встреча — до «сейчас», промежуток после неё — от её конца", () => {
    const thread = dayThread(
      [meeting("igor", "Созвон с Игорем", at(16), 60, "vf"), meeting("dinner", "Ужин", at(19), 60, null)],
      [],
      NOW,
    );
    assert.deepEqual(brief(thread), ["Созвон с Игорем past", "now", "gap 120", "Ужин future"]);
  });

  it("всё прошло — «сейчас» в конце, перед ней свободное время", () => {
    const thread = dayThread([meeting("plan", "Планёрка", at(10), 60, "rv")], [], NOW);
    assert.deepEqual(brief(thread), ["Планёрка past", "gap 340", "now"]);
  });

  it("переписка через полночь режется днём; без событий нити нет", () => {
    const thread = dayThread([], [chat(at(23, 50, 7), at(0, 20), null)], NOW);
    const [first] = thread;
    assert.ok(first?.type === "event");
    assert.deepEqual([first.event.start, first.event.minutes], [at(0), 20]);
    assert.deepEqual(dayThread([], [], NOW), []);
  });

  it("дела без часа — открытые на сегодня: день или часть дня", () => {
    const morning = task({ id: "pills", title: "Таблетки", dueAt: at(8), duePrecision: "morning", sphereId: "fam" });
    const tasks: DayTask[] = [
      ...DAY_TASKS,
      { task: morning, done: false },
      { task: task({ id: "done", title: "Сдано", dueAt: at(18), duePrecision: "day" }), done: true },
    ];
    assert.deepEqual(
      noHourTasks(tasks).map((t) => t.id),
      ["pills", "bulb"],
    );
  });

  it("пусто — ни часов, ни переписки, ни дел на сегодня", () => {
    const day = at(0);
    assert.equal(isTodayEmpty({ day, rows: [], spans: [], tasks: [] }), true);
    assert.equal(isTodayEmpty({ day, rows: [], spans: [], tasks: [DAY_TASKS[5]!] }), false);
    assert.equal(isTodayEmpty({ day, rows: [], spans: [DAY_CHATS[0]!], tasks: [] }), false);
    assert.equal(isTodayEmpty({ day, rows: [minutes(null, null, 0, 2)], spans: [], tasks: [] }), false);
    // Сделанное дело без часа на экране не видно — день пуст.
    assert.equal(isTodayEmpty({ day, rows: [], spans: [], tasks: [{ ...DAY_TASKS[5]!, done: true }] }), true);
  });
});

describe("«Неделя»", () => {
  const days = weekDays(NOW);
  const spans: Span[] = [
    meetingSpan(at(7, 30, 5), at(8, 30, 5), "sp"),
    chat(at(17, 20, 5), at(17, 30, 5), "rv"),
    chat(at(22, 30, 6), at(22, 40, 6), null),
    meetingSpan(at(21, 0, 7), at(1, 0, 8), "fam"),
    meetingSpan(at(19, 0, 8), at(20, 30, 8), "fam"),
    meetingSpan(at(11, 0, 9), at(12, 0, 9), "vf"),
  ];
  const rows = weekRows(days, spans, [325, 370, 350, 310, null, null, null], NOW);

  it("строка на каждый день: день недели, дата, часы цифрами, у будущих — «впереди»", () => {
    assert.deepEqual(
      rows.map((row) => [row.weekday, row.date, row.today, row.future, row.minutes]),
      [
        ["Пн", "5 окт", false, false, 325],
        ["Вт", "6 окт", false, false, 370],
        ["Ср", "7 окт", false, false, 350],
        ["Чт", "8 окт", true, false, 310],
        ["Пт", "9 окт", false, true, null],
        ["Сб", "10 окт", false, true, null],
        ["Вс", "11 окт", false, true, null],
      ],
    );
  });

  it("полосы — по оси 7–22: встреча и переписка, вне оси не видны", () => {
    const monday = rows[0]!.bars;
    assert.equal(monday.length, 2);
    near(monday[0]!.left, 3.333, "left 07:30");
    near(monday[0]!.width, 6.667, "width 1 ч");
    assert.deepEqual([monday[0]!.kind, monday[0]!.sphereId, monday[0]!.future], ["meeting", "sp", false]);
    near(monday[1]!.left, 68.889, "left 17:20");
    assert.equal(monday[1]!.kind, "chat");
    assert.deepEqual(rows[1]!.bars, []);
  });

  it("встреча через полночь — в каждом своём дне, по краю оси", () => {
    const wednesday = rows[2]!.bars;
    assert.equal(wednesday.length, 1);
    near(wednesday[0]!.left, 93.333, "left 21:00");
    near(wednesday[0]!.width, 6.667, "до 22:00");
    assert.equal(rows[3]!.bars.some((bar) => bar.sphereId === "fam" && bar.left === 0), false);
  });

  it("сегодня — линия «сейчас», будущие встречи — контуром", () => {
    near(rows[3]!.now ?? -1, 64.444, "now 16:40");
    assert.equal(rows[2]!.now, null);
    assert.deepEqual(
      rows[3]!.bars.map((bar) => bar.future),
      [true],
    );
    assert.deepEqual(
      rows[4]!.bars.map((bar) => bar.future),
      [true],
    );
  });

  it("сферы полосами — относительно наибольшей, в порядке базы", () => {
    const bars = sphereBars([minutes("vf", "VoiceFin", 510, 55), minutes("rv", "РЕЙВА", 300, 45), minutes(null, null, 0, 85)]);
    assert.deepEqual(
      bars.map((bar) => [bar.name, bar.minutes]),
      [
        ["VoiceFin", 565],
        ["РЕЙВА", 345],
        ["Без сферы", 85],
      ],
    );
    assert.equal(bars[0]!.width, 100);
    near(bars[1]!.width, 61.062, "РЕЙВА");
  });
});

describe("«Сферы»", () => {
  const week = [minutes("vf", "VoiceFin", 510, 55), minutes("fam", "Семья", 180, 0), minutes(null, null, 0, 85)];
  const tasks = [
    task({ id: "1", sphereId: "vf" }),
    task({ id: "2", sphereId: "vf" }),
    task({ id: "3", sphereId: "vf" }),
    task({ id: "4", sphereId: "fam" }),
    task({ id: "5" }),
  ];
  function fact(id: string, text: string, sphereId: string | null): Fact {
    return { id, category: "work", text, status: "fact", createdAt: new Date(2026, 9, 1), source: null, sphereId };
  }
  const facts = [
    fact("a", "Продаём подписку бухгалтерам", "vf"),
    fact("b", "Игорь — партнёр, отвечает за продукт", "vf"),
    fact("c", "Показы клиентам — по пятницам", "vf"),
    fact("d", "Маше 8 лет", "fam"),
    fact("e", "Люблю кофе без сахара", null),
  ];

  it("карточка на каждую сферу по местам: часы недели, полоса, дела, первая запись и «ещё N»", () => {
    const cards = sphereCards(SPHERES, week, tasks, facts);
    assert.deepEqual(
      cards.map((card) => [card.sphere.name, card.minutes, card.open, card.fact, card.more]),
      [
        ["VoiceFin", 565, 3, "Продаём подписку бухгалтерам", 2],
        ["РЕЙВА", 0, 0, null, 0],
        ["Семья", 180, 1, "Маше 8 лет", 0],
        ["Спорт", 0, 0, null, 0],
      ],
    );
    assert.equal(cards[0]!.width, 100);
    near(cards[2]!.width ?? -1, 31.858, "Семья");
    assert.equal(cards[1]!.width, 0);
  });

  it("подпись: дела и разбивка; без часов и дел — словами, а не нулями", () => {
    const [voicefin, reiva, family] = sphereCards(SPHERES, week, tasks, facts);
    assert.equal(cardMeta(voicefin!), "3 открытых дела · встречи 8 ч 30 мин · переписка 55 мин");
    assert.equal(cardMeta(family!), "1 открытое дело · встречи 3 ч");
    assert.equal(cardMeta(reiva!), "открытых дел нет · часов за неделю нет");
  });

  it("часы не прочитались — карточки без часов и полос, записи не прочитаны — без записи", () => {
    const [voicefin] = sphereCards(SPHERES, null, tasks, null);
    assert.deepEqual([voicefin!.minutes, voicefin!.width, voicefin!.fact, voicefin!.more], [null, null, null, 0]);
    assert.equal(cardMeta(voicefin!), "3 открытых дела");
  });

  it("у всех сфер ноль — полос нет", () => {
    const cards = sphereCards(SPHERES, [minutes(null, null, 0, 85)], tasks, facts);
    assert.ok(cards.every((card) => card.width === null));
  });

  it("часы «без сферы» за неделю — у карточки «Без сферы и о себе»", () => {
    assert.equal(unspheredMinutes(week), 85);
    assert.equal(unspheredMinutes([minutes("vf", "VoiceFin", 10, 0)]), 0);
    assert.equal(unspheredMinutes(null), null);
  });

  it("надзаголовок — сколько сфер из 12 и что часы недельные", () => {
    assert.equal(spheresHoursEyebrow(4), "4 из 12 · часы за неделю");
  });
});

/* -------------------------------------------------------------- база */

type Response = { data: unknown; error: { message: string; code?: string } | null; status: number };

/**
 * Клиент, который записывает цепочку каждого запроса и отвечает по ней: так
 * видно, какие функции и таблицы спрошены и с чем.
 */
function fakeDb(respond: (calls: unknown[][]) => Response): { db: Db; log: unknown[][][] } {
  const log: unknown[][][] = [];
  function chain(calls: unknown[][]): unknown {
    return new Proxy(() => {}, {
      get(_target, prop) {
        if (prop === "then") {
          return (resolve: (value: Response) => unknown) => Promise.resolve(respond(calls)).then(resolve);
        }
        return (...args: unknown[]) => {
          calls.push([prop, ...args]);
          return chain(calls);
        };
      },
    });
  }
  function start(first: unknown[]): unknown {
    const calls = [first];
    log.push(calls);
    return chain(calls);
  }
  const client = {
    from: (table: string) => start(["from", table]),
    rpc: (fn: string, params: unknown) => start(["rpc", fn, params]),
  } as unknown as SupabaseClient;
  const session = {
    env: { url: "https://example.supabase.co", anonKey: "anon" },
    token: () => "token",
    expired: () => false,
    refresh: () => Promise.resolve({ ok: false, message: "не нужен" }),
  } as unknown as Session;
  return { db: { client, session }, log };
}

const ok = (data: unknown): Response => ({ data, error: null, status: 200 });

describe("loadSphereHours и loadDaySpans", () => {
  it("часы окна — функцией базы, владелец — из токена, параметром не передаётся", async () => {
    const { db, log } = fakeDb(() => ok([{ sphere_id: "vf", name: "VoiceFin", meetings: 60, chat: 0 }]));
    assert.deepEqual(await loadSphereHours(db, at(0), at(0, 0, 9)), {
      ok: true,
      rows: [minutes("vf", "VoiceFin", 60, 0)],
    });
    assert.deepEqual(log, [
      [["rpc", "sphere_hours", { window_start: at(0).toISOString(), window_end: at(0, 0, 9).toISOString() }]],
    ]);
  });

  it("отрезки дня — функцией базы", async () => {
    const { db, log } = fakeDb(() => ok([]));
    assert.deepEqual(await loadDaySpans(db, at(0), at(0, 0, 9)), { ok: true, spans: [] });
    assert.deepEqual(log, [[["rpc", "day_spans", { span_start: at(0).toISOString(), span_end: at(0, 0, 9).toISOString() }]]]);
  });

  it("отказ базы и негодный ответ — текстом", async () => {
    const refused = fakeDb(() => ({ data: null, error: { message: "boom" }, status: 400 }));
    assert.deepEqual(await loadSphereHours(refused.db, at(0), NOW), {
      ok: false,
      message: "Не получилось прочитать часы. База ответила отказом. Попробуйте ещё раз.",
    });
    const broken = fakeDb(() => ok([{ kind: "call" }]));
    assert.deepEqual(await loadDaySpans(broken.db, at(0), NOW), {
      ok: false,
      message: "Не получилось прочитать день. База вернула отрезок без времени.",
    });
  });
});

describe("loadToday и loadWeek", () => {
  it("день: часы и отрезки от полуночи до полуночи и дела дня — открытые и сделанные", async () => {
    const { db, log } = fakeDb((calls) => {
      const [first] = calls;
      if (first?.[0] === "from") {
        return ok([
          {
            id: "igor",
            title: "Созвон с Игорем",
            due_at: "2026-10-08T09:00:00Z",
            due_precision: "time",
            duration: 120,
            sphere_id: "vf",
            status: "done",
          },
        ]);
      }
      return ok([]);
    });
    const result = await loadToday(db, NOW);
    assert.ok(result.ok);
    assert.deepEqual(result.today.day, at(0));
    assert.deepEqual(
      result.today.tasks.map((t) => [t.task.id, t.done]),
      [["igor", true]],
    );
    const rpc = log.map((calls) => calls[0]);
    assert.deepEqual(rpc.slice(0, 2), [
      ["rpc", "sphere_hours", { window_start: at(0).toISOString(), window_end: at(0, 0, 9).toISOString() }],
      ["rpc", "day_spans", { span_start: at(0).toISOString(), span_end: at(0, 0, 9).toISOString() }],
    ]);
    const tasks = log[2]!;
    assert.deepEqual(tasks[0], ["from", "tasks"]);
    assert.ok(tasks.some((call) => call[0] === "gte" && call[1] === "due_at" && call[2] === at(0).toISOString()));
    assert.ok(tasks.some((call) => call[0] === "lt" && call[1] === "due_at" && call[2] === at(0, 0, 9).toISOString()));
    assert.ok(tasks.some((call) => call[0] === "in" && call[1] === "status" && JSON.stringify(call[2]) === '["active","done"]'));
  });

  it("неделя: итог недели, часы каждого прошедшего дня и сегодня, отрезки недели", async () => {
    const { db, log } = fakeDb((calls) => {
      const [first] = calls;
      if (first?.[1] === "sphere_hours") {
        return ok([{ sphere_id: "vf", name: "VoiceFin", meetings: 60, chat: 5 }]);
      }
      return ok([]);
    });
    const result = await loadWeek(db, NOW);
    assert.ok(result.ok);
    assert.deepEqual(result.week.dayMinutes, [65, 65, 65, 65, null, null, null]);
    assert.deepEqual(result.week.rows, [minutes("vf", "VoiceFin", 60, 5)]);
    const hours = log.map((calls) => calls[0]).filter((call) => call?.[1] === "sphere_hours");
    assert.equal(hours.length, 1 + 4);
    assert.deepEqual(hours[0], [
      "rpc",
      "sphere_hours",
      { window_start: new Date(2026, 9, 5).toISOString(), window_end: new Date(2026, 9, 12).toISOString() },
    ]);
    assert.deepEqual(hours[4], [
      "rpc",
      "sphere_hours",
      { window_start: at(0).toISOString(), window_end: at(0, 0, 9).toISOString() },
    ]);
  });

  it("отказ любого чтения — текст на весь экран", async () => {
    const { db } = fakeDb((calls) =>
      calls[0]?.[1] === "day_spans" ? { data: null, error: { message: "boom" }, status: 0 } : ok([]),
    );
    assert.deepEqual(await loadWeek(db, NOW), {
      ok: false,
      message: "Не получилось прочитать день. Проверьте связь и обновите.",
    });
  });
});
