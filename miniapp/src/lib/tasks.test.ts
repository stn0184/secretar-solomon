/**
 * Группировка списка по сроку и разбор строки базы — чистые функции.
 *
 * Границы групп — `specs` 005: просрочено — день срока уже кончился (для
 * `day`) или момент прошёл (для `time`); сегодня — до конца дня; на неделе —
 * ближайшие семь дней; позже — дальше. Идеи и желания — своей группой внизу.
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import type { SupabaseClient } from "@supabase/supabase-js";

import type { Session } from "./session.ts";
import type { Db } from "./supabase.ts";
import {
  type Task,
  type TaskDraft,
  draftOf,
  dueHint,
  editTask,
  formatDuration,
  groupOf,
  groupTasks,
  needsSaving,
  parseSourceMessage,
  parseTask,
  questionOf,
  taskChanges,
  validateDraft,
  voiceCaption,
} from "./tasks.ts";

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
    openQuestion: null,
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

describe("formatDuration", () => {
  it("минуты и секунды через двоеточие, секунды двумя цифрами", () => {
    assert.equal(formatDuration(0), "0:00");
    assert.equal(formatDuration(5), "0:05");
    assert.equal(formatDuration(32), "0:32");
    assert.equal(formatDuration(61), "1:01");
    assert.equal(formatDuration(600), "10:00");
  });

  it("длиннее часа — минуты растут, часов нет", () => {
    assert.equal(formatDuration(3725), "62:05");
  });
});

describe("parseSourceMessage", () => {
  const row = { text: "в пятницу отправить расчёт", received_at: "2026-09-28T05:02:00+00:00" };

  it("текст без вида читается как текст без длительности", () => {
    const parsed = parseSourceMessage(row);
    assert.ok(parsed);
    assert.equal(parsed.kind, "text");
    assert.equal(parsed.durationSeconds, null);
  });

  it("голосовое и кружок несут вид и длительность", () => {
    const voice = parseSourceMessage({ ...row, kind: "voice", duration_seconds: 32 });
    assert.equal(voice?.kind, "voice");
    assert.equal(voice?.durationSeconds, 32);
    const note = parseSourceMessage({ ...row, kind: "video_note", duration_seconds: 15 });
    assert.equal(note?.kind, "video_note");
  });

  it("незнакомый вид — текст, негодная длительность — пусто", () => {
    const parsed = parseSourceMessage({ ...row, kind: "photo", duration_seconds: "long" });
    assert.equal(parsed?.kind, "text");
    assert.equal(parsed?.durationSeconds, null);
  });

  it("без текста или даты строка не годится", () => {
    assert.equal(parseSourceMessage({ text: 5, received_at: row.received_at }), null);
    assert.equal(parseSourceMessage({ text: "x" }), null);
    assert.equal(parseSourceMessage(null), null);
  });
});

describe("voiceCaption", () => {
  const at = new Date(2026, 8, 28, 10, 2);

  it("голосовое — «Голосовое · 0:32», кружок — «Кружок · 0:32»", () => {
    assert.equal(voiceCaption({ text: "", receivedAt: at, kind: "voice", durationSeconds: 32 }), "Голосовое · 0:32");
    assert.equal(voiceCaption({ text: "", receivedAt: at, kind: "video_note", durationSeconds: 95 }), "Кружок · 1:35");
  });

  it("у текста подписи нет", () => {
    assert.equal(voiceCaption({ text: "x", receivedAt: at, kind: "text", durationSeconds: null }), null);
  });

  it("без длительности — только слово", () => {
    assert.equal(voiceCaption({ text: "", receivedAt: at, kind: "voice", durationSeconds: null }), "Голосовое");
  });
});

/* ------------------------------------------------ правка задачи (§11) */

describe("parseTask: вопрос бота", () => {
  const row = { id: "6f1c", title: "Отправить расчёт", due_at: null };

  it("вопрос и когда он задан — из колонок задачи", () => {
    const parsed = parseTask({
      ...row,
      open_question: "Срок — пятница или понедельник?",
      question_asked_at: "2026-09-30T04:11:00+00:00",
    });
    assert.equal(parsed?.openQuestion?.text, "Срок — пятница или понедельник?");
    assert.equal(parsed?.openQuestion?.askedAt?.toISOString(), "2026-09-30T04:11:00.000Z");
  });

  it("нет вопроса или он пустой — null", () => {
    assert.equal(parseTask(row)?.openQuestion, null);
    assert.equal(parseTask({ ...row, open_question: "  " })?.openQuestion, null);
  });

  it("вопрос без времени — есть, но без даты", () => {
    const parsed = parseTask({ ...row, open_question: "К какому сроку?", question_asked_at: null });
    assert.deepEqual(parsed?.openQuestion, { text: "К какому сроку?", askedAt: null });
  });
});

describe("questionOf", () => {
  const question = (askedAt: Date | null) => task({ openQuestion: { text: "К какому сроку?", askedAt } });

  it("вопрос не старше суток — показывается", () => {
    assert.equal(questionOf(question(new Date(2026, 8, 30, 9, 11)), now)?.text, "К какому сроку?");
    assert.ok(questionOf(question(new Date(2026, 8, 29, 12, 0)), now), "ровно сутки — ещё свежий");
  });

  it("старше суток, без даты или без вопроса — нет", () => {
    assert.equal(questionOf(question(new Date(2026, 8, 29, 11, 59)), now), null);
    assert.equal(questionOf(question(null), now), null);
    assert.equal(questionOf(task(), now), null);
  });
});

function draft(overrides: Partial<TaskDraft> = {}): TaskDraft {
  return {
    title: "Задача",
    day: "",
    time: "",
    noDue: true,
    kind: "task",
    priority: "normal",
    promise: "none",
    people: "",
    ...overrides,
  };
}

// Пятница, 2 октября: день без часа — в базе 18:00.
const friday = task({
  title: "Отправить расчёт клиенту",
  dueAt: new Date(2026, 9, 2, 18, 0),
  duePrecision: "day",
  promise: "mine",
  people: ["Кузнецов", "Анна"],
});

describe("draftOf", () => {
  it("день без часа — поле часа пустое", () => {
    assert.deepEqual(draftOf(friday), {
      title: "Отправить расчёт клиенту",
      day: "2026-10-02",
      time: "",
      noDue: false,
      kind: "task",
      priority: "normal",
      promise: "mine",
      people: "Кузнецов, Анна",
    });
  });

  it("срок с часом — час в поле", () => {
    const atNoon = task({ dueAt: new Date(2026, 9, 5, 12, 0), duePrecision: "time" });
    assert.equal(draftOf(atNoon).time, "12:00");
    assert.equal(draftOf(atNoon).day, "2026-10-05");
  });

  it("без срока — флажок и пустые поля; без обещания — «нет»", () => {
    assert.deepEqual(draftOf(task()), draft());
  });
});

describe("validateDraft", () => {
  it("пустая суть или одни пробелы — «Напишите, что сделать.»", () => {
    assert.equal(validateDraft(draft({ title: "" })), "Напишите, что сделать.");
    assert.equal(validateDraft(draft({ title: "   " })), "Напишите, что сделать.");
  });

  it("срок нужен, а день не выбран — просьба выбрать", () => {
    assert.equal(
      validateDraft(draft({ noDue: false, day: "" })),
      "Выберите день или отметьте «Без срока».",
    );
    assert.equal(
      validateDraft(draft({ noDue: false, day: "2026-02-30" })),
      "Выберите день или отметьте «Без срока».",
    );
  });

  it("годный черновик — без ошибки", () => {
    assert.equal(validateDraft(draft()), null);
    assert.equal(validateDraft(draft({ noDue: false, day: "2026-10-05" })), null);
    assert.equal(validateDraft(draft({ noDue: false, day: "2026-10-05", time: "12:00" })), null);
  });
});

describe("taskChanges", () => {
  it("ничего не меняли — пусто", () => {
    assert.deepEqual(taskChanges(friday, draftOf(friday)), {});
    assert.deepEqual(taskChanges(task(), draftOf(task())), {});
  });

  it("суть — без пробелов по краям; те же слова с пробелами — не правка", () => {
    const edited = { ...draftOf(friday), title: "  Отправить расчёт Кузнецову " };
    assert.deepEqual(taskChanges(friday, edited), { title: "Отправить расчёт Кузнецову" });
    assert.deepEqual(taskChanges(friday, { ...draftOf(friday), title: " Отправить расчёт клиенту " }), {});
  });

  it("другой день без часа — датой, 18:00 ставит база", () => {
    assert.deepEqual(taskChanges(friday, { ...draftOf(friday), day: "2026-10-05" }), {
      due_date: "2026-10-05",
    });
  });

  it("день с часом — моментом со смещением устройства", () => {
    const changes = taskChanges(friday, { ...draftOf(friday), day: "2026-10-05", time: "12:00" });
    assert.deepEqual(Object.keys(changes), ["due_at"]);
    assert.equal(new Date(changes.due_at as string).getTime(), new Date(2026, 9, 5, 12, 0).getTime());
  });

  it("тот же момент — не правка; час убрали — датой того же дня", () => {
    const atNoon = task({ dueAt: new Date(2026, 9, 5, 12, 0), duePrecision: "time" });
    assert.deepEqual(taskChanges(atNoon, draftOf(atNoon)), {});
    assert.deepEqual(taskChanges(atNoon, { ...draftOf(atNoon), time: "" }), { due_date: "2026-10-05" });
  });

  it("«Без срока» снимает срок; у задачи без срока — не правка", () => {
    assert.deepEqual(taskChanges(friday, { ...draftOf(friday), noDue: true }), { due_at: null });
    assert.deepEqual(taskChanges(task(), draft({ day: "2026-10-05" })), {});
  });

  it("вид, срочность и обещание — своими значениями; «нет» — null", () => {
    assert.deepEqual(
      taskChanges(friday, { ...draftOf(friday), kind: "idea", priority: "high", promise: "none" }),
      { kind: "idea", priority: "high", promise: null },
    );
    assert.deepEqual(taskChanges(task(), draft({ promise: "to_me" })), { promise: "to_me" });
  });

  it("люди — через запятую, без пустых и пробелов", () => {
    assert.deepEqual(taskChanges(friday, { ...draftOf(friday), people: "Кузнецов,  Анна , , Пётр" }), {
      people: ["Кузнецов", "Анна", "Пётр"],
    });
    assert.deepEqual(taskChanges(friday, { ...draftOf(friday), people: " Кузнецов ,Анна" }), {});
    assert.deepEqual(taskChanges(friday, { ...draftOf(friday), people: "" }), { people: [] });
  });
});

describe("needsSaving", () => {
  it("без изменений и без пометки — запроса нет", () => {
    assert.equal(needsSaving(friday, {}), false);
  });

  it("пометка или вопрос любой давности — «Сохранить» снимает их и без изменений", () => {
    assert.equal(needsSaving(task({ needsReview: true }), {}), true);
    assert.equal(needsSaving(task({ openQuestion: { text: "Когда?", askedAt: null } }), {}), true);
  });

  it("есть изменения — запрос", () => {
    assert.equal(needsSaving(friday, { priority: "high" }), true);
  });
});

describe("dueHint", () => {
  it("без срока и без дня", () => {
    assert.equal(dueHint(draft(), now), "Без срока напоминать не буду. Час можно указать, когда выбран день.");
    assert.equal(dueHint(draft({ noDue: false }), now), "Час можно указать, когда выбран день.");
  });

  it("идея и желание — не напоминаю", () => {
    assert.equal(dueHint(draft({ noDue: false, day: "2026-10-05", kind: "idea" }), now), "Об идеях и желаниях не напоминаю.");
    assert.equal(dueHint(draft({ noDue: false, day: "2026-10-05", kind: "wish" }), now), "Об идеях и желаниях не напоминаю.");
  });

  it("день без часа: утром и вечером, а после 09:00 — только вечером", () => {
    assert.equal(dueHint(draft({ noDue: false, day: "2026-10-05" }), now), "Без часа — напомню в 09:00 и в 18:00 этого дня.");
    assert.equal(dueHint(draft({ noDue: false, day: "2026-09-30" }), now), "Без часа — напомню в 18:00 этого дня.");
  });

  it("срок с часом: за час и в срок, а в последний час — только в срок", () => {
    assert.equal(dueHint(draft({ noDue: false, day: "2026-09-30", time: "15:00" }), now), "Напомню за час и в срок.");
    assert.equal(dueHint(draft({ noDue: false, day: "2026-09-30", time: "12:30" }), now), "Напомню в срок.");
  });

  it("срок прошёл — напоминаний не будет", () => {
    const past = "Срок уже прошёл — напоминаний по нему не будет.";
    assert.equal(dueHint(draft({ noDue: false, day: "2026-09-29" }), now), past);
    assert.equal(dueHint(draft({ noDue: false, day: "2026-09-30", time: "11:00" }), now), past);
    assert.equal(dueHint(draft({ noDue: false, day: "2026-09-30" }), new Date(2026, 8, 30, 18, 0)), past);
  });
});

/* -------------------------------------------- editTask на подменённой базе */

interface RpcCall {
  name: string;
  params: unknown;
}

function fakeDb(response: { data: unknown; error: { message: string; code?: string } | null; status?: number }): {
  db: Db;
  calls: RpcCall[];
} {
  const calls: RpcCall[] = [];
  const client = {
    rpc(name: string, params: unknown) {
      calls.push({ name, params });
      return Promise.resolve(response);
    },
  } as unknown as SupabaseClient;
  const session = {
    env: { url: "https://example.supabase.co", anonKey: "anon" },
    token: () => "token",
    expired: () => false,
    refresh: () => Promise.resolve({ ok: false, message: "не нужен" }),
  } as unknown as Session;
  return { db: { client, session }, calls };
}

const SAVED_ROW = {
  id: "6f1c",
  title: "Отправить расчёт клиенту",
  kind: "task",
  due_at: "2026-10-05T07:00:00+00:00",
  due_precision: "time",
  priority: "high",
  promise: "mine",
  people: ["Кузнецов"],
  needs_review: false,
  open_question: null,
  question_asked_at: null,
  source_message_id: "m1",
  created_at: "2026-09-28T05:02:00+00:00",
};

const NOT_FOUND = "Задача не найдена — возможно, её закрыли или удалили в чате. Обновите список.";

describe("editTask", () => {
  it("зовёт edit_task с изменёнными полями и отдаёт строку базы", async () => {
    const { db, calls } = fakeDb({ data: SAVED_ROW, error: null, status: 200 });

    const result = await editTask(db, "6f1c", { priority: "high" });

    assert.deepEqual(calls, [{ name: "edit_task", params: { task_id: "6f1c", changes: { priority: "high" } } }]);
    assert.ok(result.ok);
    assert.equal(result.task.priority, "high");
    assert.equal(result.task.dueAt?.toISOString(), "2026-10-05T07:00:00.000Z");
    assert.equal(result.task.needsReview, false);
  });

  it("строка в списке из одной — та же строка", async () => {
    const { db } = fakeDb({ data: [SAVED_ROW], error: null, status: 200 });
    const result = await editTask(db, "6f1c", {});
    assert.ok(result.ok);
    assert.equal(result.task.id, "6f1c");
  });

  it("пустой ответ — задачу закрыли или удалили: ничего не записано", async () => {
    for (const data of [null, { id: null, title: null }, []]) {
      const { db } = fakeDb({ data, error: null, status: 200 });
      assert.deepEqual(await editTask(db, "6f1c", { title: "x" }), { ok: false, message: NOT_FOUND });
    }
  });

  it("отказ базы — причина по-русски, без английского хвоста", async () => {
    const { db } = fakeDb({
      data: null,
      error: { message: "edit_task: owner has no timezone", code: "P0001" },
      status: 400,
    });
    assert.deepEqual(await editTask(db, "6f1c", { due_date: "2026-10-05" }), {
      ok: false,
      message: "Не получилось сохранить задачу. База ответила отказом. Попробуйте ещё раз.",
    });
  });
});
