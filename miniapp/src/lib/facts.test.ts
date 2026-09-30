/**
 * Память о пользователе — чистые функции: разбор строки базы, группировка
 * по категориям в фиксированном порядке, подзаголовок со счётчиком и
 * подпись источника («с ваших слов» / «из сообщения»).
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  type Fact,
  CATEGORY_ORDER,
  factsSubtitle,
  groupFacts,
  isOwnWords,
  parseFact,
  sourceCaption,
} from "./facts.ts";

function fact(overrides: Partial<Fact> = {}): Fact {
  return {
    id: "f",
    category: "other",
    text: "Запись",
    status: "fact",
    createdAt: new Date(2026, 8, 12, 9, 0),
    source: null,
    ...overrides,
  };
}

describe("parseFact", () => {
  it("строка с вложенным источником — запись с текстом, датой и видом сообщения", () => {
    const parsed = parseFact({
      id: "f1",
      category: "car",
      text: "Машина — Toyota Camry",
      status: "fact",
      created_at: "2026-09-12T09:00:00+05:00",
      source: {
        text: "у меня Toyota Camry",
        received_at: "2026-09-12T08:59:00+05:00",
        kind: "voice",
        duration_seconds: 4,
        analysis_kind: "about_me",
      },
    });

    assert.ok(parsed);
    assert.equal(parsed.category, "car");
    assert.equal(parsed.status, "fact");
    assert.equal(parsed.source?.text, "у меня Toyota Camry");
    assert.equal(parsed.source?.analysisKind, "about_me");
    // Вид и длительность самого сообщения — те же поля, что у источника задачи.
    assert.equal(parsed.source?.kind, "voice");
    assert.equal(parsed.source?.durationSeconds, 4);
    assert.equal(parsed.source?.receivedAt.getTime(), new Date("2026-09-12T08:59:00+05:00").getTime());
  });

  it("источник-снимок несёт прочитанное со снимка", () => {
    const parsed = parseFact({
      id: "f2",
      category: "home",
      text: "Лампы в прихожей — E14",
      status: "guess",
      created_at: "2026-09-30T09:00:00+05:00",
      source: {
        text: "",
        received_at: "2026-09-30T08:59:00+05:00",
        kind: "photo",
        duration_seconds: null,
        photo_text: "Этикетка лампы: 7 Вт, E14",
        analysis_kind: "task",
      },
    });

    assert.equal(parsed?.source?.kind, "photo");
    assert.equal(parsed?.source?.photoText, "Этикетка лампы: 7 Вт, E14");
  });

  it("без источника — запись без цитаты, а не отказ", () => {
    const parsed = parseFact({ id: "f1", category: "home", text: "Живёт в Казани", status: "guess", created_at: "2026-09-02T10:00:00Z", source: null });

    assert.ok(parsed);
    assert.equal(parsed.source, null);
    assert.equal(parsed.status, "guess");
  });

  it("незнакомая категория — «другое», незнакомый статус — предположение", () => {
    const parsed = parseFact({ id: "f1", category: "pets", text: "Кот Барсик", status: "sure", created_at: "2026-09-02T10:00:00Z" });

    assert.ok(parsed);
    assert.equal(parsed.category, "other");
    assert.equal(parsed.status, "guess");
  });

  it("нет id или текста — null", () => {
    assert.equal(parseFact({ id: "f1", category: "car" }), null);
    assert.equal(parseFact({ category: "car", text: "Camry" }), null);
    assert.equal(parseFact(null), null);
  });
});

describe("groupFacts", () => {
  it("порядок категорий фиксирован, пустые пропускаются, порядок внутри сохраняется", () => {
    const groups = groupFacts([
      fact({ id: "w1", category: "work", text: "Работа до 18:00" }),
      fact({ id: "f1", category: "family", text: "Жена — Марина" }),
      fact({ id: "w2", category: "work", text: "По пятницам в офисе" }),
      fact({ id: "c1", category: "car", text: "Машина — Toyota Camry" }),
    ]);

    assert.deepEqual(
      groups.map((g) => [g.category, g.title, g.facts.map((f) => f.id)]),
      [
        ["family", "Семья", ["f1"]],
        ["car", "Машина", ["c1"]],
        ["work", "Работа", ["w1", "w2"]],
      ],
    );
  });

  it("порядок категорий — Семья · Дом · Машина · Работа · Привычки · Предпочтения · Другое", () => {
    assert.deepEqual([...CATEGORY_ORDER], ["family", "home", "car", "work", "habit", "preference", "other"]);
  });

  it("нет записей — нет групп", () => {
    assert.deepEqual(groupFacts([]), []);
  });
});

describe("factsSubtitle", () => {
  it("считает записи и предположения по русским правилам", () => {
    assert.equal(factsSubtitle([fact()]), "1 запись");
    assert.equal(
      factsSubtitle([fact(), fact({ status: "guess" })]),
      "2 записи · 1 предположение",
    );
    assert.equal(
      factsSubtitle([
        fact(), fact(), fact(), fact(),
        fact({ status: "guess" }), fact({ status: "guess" }), fact({ status: "guess" }),
      ]),
      "7 записей · 3 предположения",
    );
    assert.equal(
      factsSubtitle(Array.from({ length: 5 }, () => fact({ status: "guess" }))),
      "5 записей · 5 предположений",
    );
    assert.equal(
      factsSubtitle(Array.from({ length: 21 }, () => fact())),
      "21 запись",
    );
  });
});

describe("sourceCaption", () => {
  const ownWords = {
    text: "у меня Camry",
    receivedAt: new Date(2026, 8, 12, 9, 0),
    kind: "text" as const,
    durationSeconds: null,
    photoText: null,
    analysisKind: "about_me",
  };
  const errand = {
    text: "забрать Мишу из садика",
    receivedAt: new Date(2026, 8, 24, 17, 40),
    kind: "text" as const,
    durationSeconds: null,
    photoText: null,
    analysisKind: "task",
  };

  it("сказанное прямо — «с ваших слов», выведенное — «из сообщения», дата — записи", () => {
    assert.equal(sourceCaption(fact({ source: ownWords })), "с ваших слов · 12 сентября");
    assert.equal(
      sourceCaption(fact({ source: errand, createdAt: new Date(2026, 8, 24, 17, 40) })),
      "из сообщения · 24 сентября",
    );
  });

  it("подпись не зависит от статуса: подтверждённое предположение остаётся «из сообщения»", () => {
    assert.equal(isOwnWords(fact({ source: errand, status: "fact" })), false);
    assert.equal(isOwnWords(fact({ source: ownWords, status: "guess" })), true);
  });

  it("без источника — «из сообщения»", () => {
    assert.equal(sourceCaption(fact({ createdAt: new Date(2026, 9, 3) })), "из сообщения · 3 октября");
  });
});
