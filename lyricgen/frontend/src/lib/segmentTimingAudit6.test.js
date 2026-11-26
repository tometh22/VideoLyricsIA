import { describe, expect, it } from "vitest";
import { clampBlockShiftDelta, shiftBlockWithinDuration } from "./segmentTiming";

describe("block shifts with imported outliers", () => {
  it("never turns a positive cascade into a negative shift because its tail is beyond the audio", () => {
    const rows = [{ start: 1, end: 2 }, { start: 5, end: 6 }];
    expect(clampBlockShiftDelta(rows, .5, 4)).toBe(0);
    expect(shiftBlockWithinDuration(rows, .5, 4)).toEqual(rows);
  });
  it("does not turn an outward negative request into a positive repair", () => {
    expect(clampBlockShiftDelta([{ start: -1, end: 1 }], -.5, 4)).toBe(0);
  });
  it("keeps zero a no-op on an out-of-bounds document", () => {
    expect(clampBlockShiftDelta([{ start: 5, end: 6 }], 0, 4)).toBe(0);
  });
  it("still allows an explicit inward recovery and preserves durations", () => {
    expect(shiftBlockWithinDuration([{ start: 5, end: 6 }], -2, 4)).toEqual([{ start: 3, end: 4 }]);
    expect(clampBlockShiftDelta([{ start: 1, end: 3.8 }], .5, 4)).toBeCloseTo(.2);
  });
});
