import { describe, expect, it } from "vitest";
import { segmentsValuesEqual } from "./segmentsValuesEqual";

describe("segmentsValuesEqual", () => {
  it("returns true for identical references", () => {
    const a = [{ start: 0, end: 1, text: "x" }];
    expect(segmentsValuesEqual(a, a)).toBe(true);
  });

  it("returns true for equal-value arrays with different refs", () => {
    const a = [{ start: 0, end: 1, text: "x" }];
    const b = [{ start: 0, end: 1, text: "x" }];
    expect(segmentsValuesEqual(a, b)).toBe(true);
  });

  it("returns true ignoring extra fields like _id, locked, pos, scale", () => {
    const a = [{ start: 0, end: 1, text: "x", _id: 0, locked: true, pos: { x: 0.1 } }];
    const b = [{ start: 0, end: 1, text: "x", _id: 99 }];
    expect(segmentsValuesEqual(a, b)).toBe(true);
  });

  it("treats floats within 1ms epsilon as equal", () => {
    const a = [{ start: 0.0001, end: 1.0009, text: "x" }];
    const b = [{ start: 0, end: 1.001, text: "x" }];
    expect(segmentsValuesEqual(a, b)).toBe(true);
  });

  it("returns false when start differs > epsilon", () => {
    const a = [{ start: 0, end: 1, text: "x" }];
    const b = [{ start: 0.01, end: 1, text: "x" }];
    expect(segmentsValuesEqual(a, b)).toBe(false);
  });

  it("returns false when end differs > epsilon", () => {
    const a = [{ start: 0, end: 1, text: "x" }];
    const b = [{ start: 0, end: 2, text: "x" }];
    expect(segmentsValuesEqual(a, b)).toBe(false);
  });

  it("returns false when text differs", () => {
    const a = [{ start: 0, end: 1, text: "uno" }];
    const b = [{ start: 0, end: 1, text: "dos" }];
    expect(segmentsValuesEqual(a, b)).toBe(false);
  });

  it("returns false when lengths differ", () => {
    expect(segmentsValuesEqual(
      [{ start: 0, end: 1, text: "x" }],
      [{ start: 0, end: 1, text: "x" }, { start: 1, end: 2, text: "y" }]
    )).toBe(false);
  });

  it("returns true for two empty arrays", () => {
    expect(segmentsValuesEqual([], [])).toBe(true);
  });

  it("returns false when one side isn't an array", () => {
    // The ref-equality short-circuit covers the trivial identical-string
    // case; the Array.isArray guard catches mixed inputs.
    expect(segmentsValuesEqual(null, [])).toBe(false);
    expect(segmentsValuesEqual([], undefined)).toBe(false);
    expect(segmentsValuesEqual("foo", ["foo"])).toBe(false);
  });

  it("handles multi-segment arrays where only one segment differs (drag-resize scenario)", () => {
    // Operator dragged the end of segment[0] from 2.0 to 2.5.
    const baseline = [
      { start: 0, end: 2.0, text: "Línea uno" },
      { start: 2.0, end: 4, text: "Línea dos" },
    ];
    const afterDrag = [
      { start: 0, end: 2.5, text: "Línea uno" },
      { start: 2.0, end: 4, text: "Línea dos" },
    ];
    expect(segmentsValuesEqual(baseline, afterDrag)).toBe(false);
  });

  it("returns true when backend reorders segments by start (reseed-storm fix)", () => {
    // User split a segment leaving local array out-of-order. Backend sorts
    // by start on save and returns them in a different order. The comparison
    // must treat this as "same values" to avoid the reseed loop.
    const local = [
      { start: 5, end: 8, text: "B" },
      { start: 3, end: 5, text: "C" },
      { start: 0, end: 3, text: "A" },
    ];
    const fromServer = [
      { start: 0, end: 3, text: "A" },
      { start: 3, end: 5, text: "C" },
      { start: 5, end: 8, text: "B" },
    ];
    expect(segmentsValuesEqual(local, fromServer)).toBe(true);
  });

  it("handles autosave roundtrip: same values, fresh refs, extra fields differ", () => {
    // Local `edited` has _id + locked from the recent drag.
    const edited = [
      { start: 0, end: 2.5, text: "x", _id: 0, locked: true },
      { start: 2.5, end: 4, text: "y", _id: 1 },
    ];
    // Server roundtrip: cleaned + persisted, fresh array reference,
    // _id stripped (will be re-added by reseed if it fires).
    const fromAutosave = [
      { start: 0, end: 2.5, text: "x", locked: true },
      { start: 2.5, end: 4, text: "y" },
    ];
    expect(segmentsValuesEqual(edited, fromAutosave)).toBe(true);
  });

  it("still detects a genuine text swap between two segments with the SAME start (sort-invariance must not mask a real edit)", () => {
    // Guards the reseed-storm fix (#724): the sort-by-start comparison must
    // not collapse a real edit to "equal". Two lines share an identical start
    // and their text is genuinely swapped. JS Array.sort is stable, so equal
    // `start` keys keep their original order — the positional text compare
    // after the sort must therefore see the swap and return false. If a future
    // refactor makes the sort unstable (or compares as a multiset), this flips
    // to true and the editor would silently drop the operator's change.
    const before = [
      { start: 1.0, end: 2.0, text: "A" },
      { start: 1.0, end: 2.0, text: "B" },
    ];
    const afterSwap = [
      { start: 1.0, end: 2.0, text: "B" },
      { start: 1.0, end: 2.0, text: "A" },
    ];
    expect(segmentsValuesEqual(before, afterSwap)).toBe(false);
  });
});
