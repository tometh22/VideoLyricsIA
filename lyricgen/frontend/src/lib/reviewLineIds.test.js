import legacy from "../shared/legacyReviewIdentities.json";
import { describe, it, expect } from "vitest";
import cases from "../shared/reviewLineIds.json";
import { reviewLineIds } from "./reviewLineIds";

describe("snapshot review occurrence identities (shared Python/JS contract)", () => {
  it.each(cases)("$name", ({ segments, expected }) => {
    const before = structuredClone(segments);
    expect(reviewLineIds(segments)).toEqual(expected);
    expect(new Set(expected).size).toBe(segments.length);
    expect(segments).toEqual(before);
  });
});


it("attests all 66 legacy occurrences while preserving the original 63 persisted identities", () => {
  const before = structuredClone(legacy.segments);
  expect(reviewLineIds(legacy.segments)).toEqual(legacy.approval_ids);
  expect(new Set(legacy.approval_ids).size).toBe(66);
  expect(new Set(legacy.segments.map(row => row.segment_id)).size).toBe(63);
  expect(legacy.segments).toEqual(before);
});
