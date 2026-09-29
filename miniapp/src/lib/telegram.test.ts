/**
 * Тексты системных окон — чистая часть `telegram.ts`; само окно рисует
 * Telegram, на Node его нет.
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { deleteQuestion } from "./telegram.ts";

describe("deleteQuestion", () => {
  it("разовая задача — как раньше", () => {
    assert.deepEqual(deleteQuestion("Забрать костюм", false), {
      title: "Удалить задачу?",
      message: "«Забрать костюм» исчезнет вместе с напоминаниями. Сообщение в переписке останется.",
    });
  });

  it("повторяющаяся — удаляется вся серия (§13.6)", () => {
    assert.deepEqual(deleteQuestion("Созвон с командой", true), {
      title: "Удалить задачу со всеми повторами?",
      message:
        "«Созвон с командой» исчезнет вместе с напоминаниями — и этот раз, и все следующие. " +
        "Сообщение в переписке останется.",
    });
  });

  it("длинная суть укорачивается — окно Telegram не шире 256 знаков", () => {
    const { message } = deleteQuestion("а".repeat(300), true);
    assert.ok(message.length <= 256);
    assert.ok(message.startsWith(`«${"а".repeat(79)}…»`));
  });
});
