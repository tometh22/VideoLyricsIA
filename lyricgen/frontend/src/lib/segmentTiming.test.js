import { describe, it, expect } from "vitest";
import { clampSegmentToDuration } from "./segmentTiming";

describe("clampSegmentToDuration", () => {
  const DURATION = 100;

  it("shifts within bounds without touching length", () => {
    const out = clampSegmentToDuration({ start: 10, end: 14 }, 20, DURATION);
    expect(out).toMatchObject({ start: 20, end: 24 });
  });

  it("clamps a negative newStart to 0", () => {
    const out = clampSegmentToDuration({ start: 5, end: 8 }, -3, DURATION);
    expect(out).toMatchObject({ start: 0, end: 3 });
  });

  it("does NOT invert when shifted past the song end (regression)", () => {
    // start pushed to 120 on a 100s song. Old code left start=120, end=100
    // (start > end) → the sanitiser squashed it to a zero-length line.
    const out = clampSegmentToDuration({ start: 90, end: 94 }, 120, DURATION);
    expect(out.start).toBeLessThanOrEqual(out.end);
    // Pinned flush to the end, length (4s) preserved.
    expect(out).toMatchObject({ start: 96, end: 100 });
  });

  it("keeps a whole cascade of trailing lines in contract past the end", () => {
    const segs = [
      { start: 88, end: 91 },
      { start: 92, end: 95 },
      { start: 96, end: 99 },
    ];
    const shifted = segs.map((s) =>
      clampSegmentToDuration(s, s.start + 20, DURATION),
    );
    for (const s of shifted) {
      expect(s.start).toBeGreaterThanOrEqual(0);
      expect(s.start).toBeLessThanOrEqual(s.end);
      expect(s.end).toBeLessThanOrEqual(DURATION);
    }
  });

  it("no ceiling when duration is 0/undefined", () => {
    const out = clampSegmentToDuration({ start: 10, end: 12 }, 5000, 0);
    expect(out).toMatchObject({ start: 5000, end: 5002 });
  });

  it("preserves extra fields", () => {
    const out = clampSegmentToDuration(
      { start: 1, end: 2, text: "hola", _id: 7, words: [{ w: "hola" }] },
      3,
      DURATION,
    );
    expect(out.text).toBe("hola");
    expect(out._id).toBe(7);
    expect(out.words).toEqual([{ w: "hola" }]);
  });

  it("falls back to a 0.5s min length on non-finite input", () => {
    const out = clampSegmentToDuration({ start: NaN, end: NaN }, 10, DURATION);
    expect(out.start).toBe(10);
    expect(out.end).toBe(10.5);
  });
});
