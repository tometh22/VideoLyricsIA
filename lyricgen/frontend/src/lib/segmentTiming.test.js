import { describe, expect, it } from "vitest";
import { clampSegmentToDuration } from "./segmentTiming";

describe("clampSegmentToDuration", () => {
  const duration = 100;

  it("shifts within bounds without changing length", () => {
    expect(clampSegmentToDuration({ start: 10, end: 14 }, 20, duration))
      .toMatchObject({ start: 20, end: 24 });
  });

  it("clamps a negative start to zero", () => {
    expect(clampSegmentToDuration({ start: 5, end: 8 }, -3, duration))
      .toMatchObject({ start: 0, end: 3 });
  });

  it("pins a segment past the song end without inversion", () => {
    const result = clampSegmentToDuration(
      { start: 90, end: 94 },
      120,
      duration,
    );
    expect(result).toMatchObject({ start: 96, end: 100 });
    expect(result.start).toBeLessThanOrEqual(result.end);
  });

  it("keeps a trailing cascade in contract", () => {
    const shifted = [
      { start: 88, end: 91 },
      { start: 92, end: 95 },
      { start: 96, end: 99 },
    ].map((segment) =>
      clampSegmentToDuration(segment, segment.start + 20, duration));

    for (const segment of shifted) {
      expect(segment.start).toBeGreaterThanOrEqual(0);
      expect(segment.start).toBeLessThanOrEqual(segment.end);
      expect(segment.end).toBeLessThanOrEqual(duration);
    }
  });

  it("has no ceiling when duration is zero", () => {
    expect(clampSegmentToDuration({ start: 10, end: 12 }, 5000, 0))
      .toMatchObject({ start: 5000, end: 5002 });
  });

  it("preserves non-timing fields", () => {
    const result = clampSegmentToDuration(
      { start: 1, end: 2, text: "hola", _id: 7 },
      3,
      duration,
    );
    expect(result).toMatchObject({ start: 3, end: 4, text: "hola", _id: 7 });
  });

  it("uses the minimum length for non-finite input", () => {
    expect(clampSegmentToDuration(
      { start: Number.NaN, end: Number.NaN },
      10,
      duration,
    )).toMatchObject({ start: 10, end: 10.5 });
  });
});
