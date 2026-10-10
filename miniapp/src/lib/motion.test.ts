/**
 * Прокрутка цифр (`design.md` §1, «Движение»): число раскладывается на
 * разряды, каждый крутится колонкой 0–9 к своей цифре.
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { digitsOf } from "./motion.ts";

describe("digitsOf", () => {
  it("разряды слева направо", () => {
    assert.deepEqual(digitsOf(5), [5]);
    assert.deepEqual(digitsOf(310), [3, 1, 0]);
    assert.deepEqual(digitsOf(0), [0]);
  });

  it("минуты — двумя разрядами: «05», а не «5»", () => {
    assert.deepEqual(digitsOf(5, 2), [0, 5]);
    assert.deepEqual(digitsOf(40, 2), [4, 0]);
    assert.deepEqual(digitsOf(125, 2), [1, 2, 5]);
  });

  it("дробное округляется вниз, отрицательное и не число — ноль", () => {
    assert.deepEqual(digitsOf(9.8), [9]);
    assert.deepEqual(digitsOf(-3), [0]);
    assert.deepEqual(digitsOf(Number.NaN, 2), [0, 0]);
  });
});
