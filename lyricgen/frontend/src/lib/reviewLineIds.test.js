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
