import { describe, expect, it } from "vitest";
import {
  clampBlockShiftDelta,
  shiftBlockWithinDuration,
} from "./segmentTiming";

describe("block timeline shifts", () => {
  const duration = 100;

  it("shifts a block within bounds without changing offsets", () => {
    expect(shiftBlockWithinDuration(
      [{ start: 10, end: 14 }, { start: 16, end: 20 }],
      5,
      duration,
    )).toEqual([{ start: 15, end: 19 }, { start: 21, end: 25 }]);
  });

  it("bounds a negative shift using the first start", () => {
    expect(clampBlockShiftDelta(
      [{ start: 5, end: 8 }, { start: 10, end: 12 }],
      -9,
      duration,
    )).toBe(-5);
  });

  it("preserves order, lengths, and gaps at the song end", () => {
    const original = [
      { start: 88, end: 91 },
      { start: 92, end: 95 },
      { start: 96, end: 99 },
    ];
    const shifted = [
      ...shiftBlockWithinDuration(original, 20, duration),
    ];

    expect(shifted).toEqual([
      { start: 89, end: 92 },
      { start: 93, end: 96 },
      { start: 97, end: 100 },
    ]);
    expect(shifted.map((segment) => segment.end - segment.start))
      .toEqual(original.map((segment) => segment.end - segment.start));
    expect(shifted.slice(1).map((segment, index) =>
      segment.start - shifted[index].end)).toEqual([1, 1]);
  });

  it("has no ceiling when duration is zero", () => {
    expect(shiftBlockWithinDuration(
      [{ start: 10, end: 12 }],
      5000,
      0,
    )).toEqual([{ start: 5010, end: 5012 }]);
  });

  it("preserves non-timing fields", () => {
    const [result] = shiftBlockWithinDuration(
      [{ start: 1, end: 2, text: "hola", _id: 7 }],
      2,
      duration,
    );
    expect(result).toMatchObject({ start: 3, end: 4, text: "hola", _id: 7 });
  });

  it("refuses to shift non-finite input", () => {
    expect(clampBlockShiftDelta(
      [{ start: Number.NaN, end: Number.NaN }],
      10,
      duration,
    )).toBe(0);
  });
});
