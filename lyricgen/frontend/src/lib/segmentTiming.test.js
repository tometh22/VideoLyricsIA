import { describe, expect, it } from "vitest";
import {
  clampBlockShiftDelta,
  clampResizeTiming,
  clampResizeTimingWithAdjacent,
  clampSelectionShiftDelta,
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

describe("collision-safe timeline edits", () => {
  const lines = [
    { _id: "a", start: 0, end: 1 },
    { _id: "b", start: 2, end: 3 },
    { _id: "c", start: 4, end: 5 },
    { _id: "d", start: 6, end: 7 },
  ];

  it("intersects bounds for a non-contiguous selection", () => {
    expect(clampSelectionShiftDelta(lines, new Set(["b", "d"]), -10, 10, 0.05))
      .toBeCloseTo(-0.95);
    expect(clampSelectionShiftDelta(lines, new Set(["b", "d"]), 10, 10, 0.05))
      .toBeCloseTo(0.95);
  });

  it("keeps a moved line 50ms away from neighbours", () => {
    expect(clampSelectionShiftDelta(lines, new Set(["b"]), -2, 10, 0.05))
      .toBeCloseTo(-0.95);
    expect(clampSelectionShiftDelta(lines, new Set(["b"]), 2, 10, 0.05))
      .toBeCloseTo(0.95);
  });

  it("clamps resize handles against adjacent lines", () => {
    expect(clampResizeTiming(lines, "b", 0, 8, 10, 0.05, 0.3)).toMatchObject({
      start: 1.05,
      end: 3.95,
      blocked: false,
    });
  });

  it("keeps the start anchored when the end handle crosses minimum duration", () => {
    expect(clampResizeTiming(lines, "b", 2, 1.5, 10, 0.05, 0.3, "end")).toEqual({
      start: 2,
      end: 2.3,
      blocked: false,
    });
  });

  it("keeps the end anchored when the start handle crosses minimum duration", () => {
    expect(clampResizeTiming(lines, "b", 3.8, 3, 10, 0.05, 0.3, "start")).toEqual({
      start: 2.7,
      end: 3,
      blocked: false,
    });
  });

  it("allows an end handle without a finite song ceiling", () => {
    const openEnded = [{ _id: "only", start: 2, end: 3 }];
    expect(clampResizeTiming(openEnded, "only", 2, 8, 0, 0.05, 0.3, "end")).toEqual({
      start: 2,
      end: 8,
      blocked: false,
    });
  });

  it("moves a packed next boundary when extending an end handle", () => {
    const packed = [
      { _id: "a", start: 0, end: 2 },
      { _id: "b", start: 2.05, end: 3.5 },
    ];
    expect(clampResizeTimingWithAdjacent(packed, "a", 0, 2.5, 10, 0.05, 0.3, "end"))
      .toEqual({
        changes: [
          { id: "a", start: 0, end: 2.5 },
          { id: "b", start: 2.55, end: 3.5 },
        ],
        blocked: false,
        coupled: true,
      });
  });

  it("moves a packed previous boundary when extending a start handle", () => {
    const packed = [
      { _id: "a", start: 0, end: 1 },
      { _id: "b", start: 1.05, end: 2.5 },
    ];
    expect(clampResizeTimingWithAdjacent(packed, "b", 0.7, 2.5, 10, 0.05, 0.3, "start"))
      .toEqual({
        changes: [
          { id: "a", start: 0, end: 0.7 - 0.05 },
          { id: "b", start: 0.7, end: 2.5 },
        ],
        blocked: false,
        coupled: true,
      });
  });

  it("refuses an impossible resize without mutating the snapshot", () => {
    const crowded = [
      { _id: "a", start: 0, end: 1 },
      { _id: "b", start: 1.02, end: 1.2 },
      { _id: "c", start: 1.22, end: 2 },
    ];
    expect(clampResizeTiming(crowded, "b", 1, 1.5, 2, 0.05, 0.3)).toEqual({
      start: 1.02,
      end: 1.2,
      blocked: true,
    });
  });
});
