import { describe, expect, it } from "vitest";
import { accrueActiveEditMs, summarizeOperatorCorrections } from "./LyricsEditor";

describe("operator correction telemetry", () => {
  it("separates text work from timing work without storing lyric text", () => {
    const summary = summarizeOperatorCorrections(
      [
        { start: 1, end: 2, text: "alpha beta" },
        { start: 3, end: 4, text: "gamma delta" },
      ],
      [
        { start: 1, end: 2, text: "alpha changed" },
        { start: 3.5, end: 4.5, text: "gamma delta" },
        { start: 6, end: 7, text: "new line" },
      ],
    );
    expect(summary).toEqual({
      line_count: 3,
      text_changes: 1,
      timing_changes: 1,
      lines_added: 1,
      lines_removed: 0,
      lines_reordered: 0,
    });
    expect(JSON.stringify(summary)).not.toContain("alpha");
  });

  it("uses stable ids so insertion does not manufacture cascading edits", () => {
    const before = [
      { _id: 0, start: 1, end: 2, text: "one" },
      { _id: 1, start: 3, end: 4, text: "two" },
    ];
    const after = [
      { _id: 2, start: 0, end: 0.5, text: "intro" },
      before[0], before[1],
    ];
    expect(summarizeOperatorCorrections(before, after)).toMatchObject({
      text_changes: 0, timing_changes: 0, lines_added: 1, lines_removed: 0,
    });
  });
});

describe("active operator clock", () => {
  it("caps inactivity and never adds blurred time", () => {
    const clock = {
      totalMs: 0, lastTickMs: 0, lastActivityMs: 0, active: true,
    };
    expect(accrueActiveEditMs(clock, 120_000)).toBe(60_000);
    clock.active = false;
    expect(accrueActiveEditMs(clock, 300_000)).toBe(60_000);
  });
});
