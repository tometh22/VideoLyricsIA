import { describe, it, expect } from "vitest";
import { sanitizeSegmentsForSave, findSanitizedDiffs } from "./sanitizeSegments";

describe("sanitizeSegmentsForSave", () => {
  it("passes through already-valid segments unchanged (values)", () => {
    const segs = [{ start: 0, end: 2.5, text: "hola" }, { start: 2.5, end: 4, text: "chau" }];
    expect(sanitizeSegmentsForSave(segs)).toEqual(segs);
  });

  it("swaps an inverted start>end (dragged ENTRA past SALE) — the 400 'out of range'", () => {
    const [s] = sanitizeSegmentsForSave([{ start: 5, end: 3, text: "x" }]);
    expect(s.start).toBe(3);
    expect(s.end).toBe(5);
  });

  it("replaces NaN start with 0 and NaN end with start (the 400 'must be finite')", () => {
    const [s] = sanitizeSegmentsForSave([{ start: NaN, end: NaN, text: "x" }]);
    expect(s.start).toBe(0);
    expect(s.end).toBe(0);
    const [s2] = sanitizeSegmentsForSave([{ start: 2, end: Number("nope"), text: "x" }]);
    expect(s2.start).toBe(2);
    expect(s2.end).toBe(2);
  });

  it("clamps negatives to 0 and caps end at the 24h ceiling", () => {
    const [s] = sanitizeSegmentsForSave([{ start: -3, end: -1, text: "x" }]);
    expect(s.start).toBe(0);
    expect(s.end).toBe(0);
    const [s2] = sanitizeSegmentsForSave([{ start: 0, end: 24 * 3600 + 999, text: "x" }]);
    expect(s2.end).toBe(24 * 3600);
  });

  it("coerces non-string text and caps length at 2000 chars", () => {
    const [s] = sanitizeSegmentsForSave([{ start: 0, end: 1, text: null }]);
    expect(s.text).toBe("");
    const [s2] = sanitizeSegmentsForSave([{ start: 0, end: 1, text: 12345 }]);
    expect(s2.text).toBe("12345");
    const long = "a".repeat(5000);
    const [s3] = sanitizeSegmentsForSave([{ start: 0, end: 1, text: long }]);
    expect(s3.text).toHaveLength(2000);
  });

  it("preserves extra fields (_id, locked) while fixing the contract fields", () => {
    const [s] = sanitizeSegmentsForSave([{ start: 9, end: 1, text: "x", _id: 7, locked: true }]);
    expect(s._id).toBe(7);
    expect(s.locked).toBe(true);
    expect(s.start).toBe(1);
    expect(s.end).toBe(9);
  });

  it("the result is always backend-valid (0 ≤ start ≤ end ≤ MAX, finite, string text)", () => {
    const nasty = [
      { start: 5, end: 3, text: "inverted" },
      { start: NaN, end: 2, text: 999 },
      { start: -10, end: 999999, text: null },
      { start: "1.5", end: "2.5", text: "stringy nums" },
    ];
    for (const s of sanitizeSegmentsForSave(nasty)) {
      expect(Number.isFinite(s.start)).toBe(true);
      expect(Number.isFinite(s.end)).toBe(true);
      expect(s.start).toBeGreaterThanOrEqual(0);
      expect(s.end).toBeGreaterThanOrEqual(s.start);
      expect(s.end).toBeLessThanOrEqual(24 * 3600);
      expect(typeof s.text).toBe("string");
    }
  });

  it("returns non-array input untouched (defensive)", () => {
    expect(sanitizeSegmentsForSave(null)).toBe(null);
    expect(sanitizeSegmentsForSave(undefined)).toBe(undefined);
  });
});

describe("findSanitizedDiffs", () => {
  it("reports the indices the sanitiser changed", () => {
    const orig = [{ start: 0, end: 1, text: "ok" }, { start: 5, end: 3, text: "bad" }];
    const diffs = findSanitizedDiffs(orig, sanitizeSegmentsForSave(orig));
    expect(diffs).toHaveLength(1);
    expect(diffs[0].index).toBe(1);
  });

  it("returns [] when nothing was changed", () => {
    const orig = [{ start: 0, end: 1, text: "ok" }];
    expect(findSanitizedDiffs(orig, sanitizeSegmentsForSave(orig))).toEqual([]);
  });
});
