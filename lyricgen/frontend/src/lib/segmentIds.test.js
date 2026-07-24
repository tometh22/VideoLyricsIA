import { describe, expect, it } from "vitest";
import { reseedPreservingIds } from "./segmentIds";

const seg = (id, start, end, text) => ({ _id: id, start, end, text });

describe("reseedPreservingIds", () => {
  it("a pure value-echo preserves every _id (zero remounts)", () => {
    const current = [seg(0, 1, 2, "a"), seg(1, 3, 4, "b"), seg(7, 5, 6, "c")];
    const incoming = current.map(({ _id, ...rest }) => ({ ...rest }));
    const out = reseedPreservingIds(current, incoming);
    expect(out.map((s) => s._id)).toEqual([0, 1, 7]);
  });

  it("an echo REORDERED by the backend sort still preserves ids per row", () => {
    const current = [seg(0, 3, 4, "b"), seg(1, 1, 2, "a")];
    const incoming = [
      { start: 1, end: 2, text: "a" },
      { start: 3, end: 4, text: "b" },
    ];
    const out = reseedPreservingIds(current, incoming);
    expect(out.find((s) => s.text === "a")._id).toBe(1);
    expect(out.find((s) => s.text === "b")._id).toBe(0);
  });

  it("only the genuinely-changed row gets a fresh id", () => {
    const current = [seg(0, 1, 2, "a"), seg(1, 3, 4, "b"), seg(2, 5, 6, "c")];
    const incoming = [
      { start: 1, end: 2, text: "a" },
      { start: 3, end: 4, text: "b EDITADA" },
      { start: 5, end: 6, text: "c" },
    ];
    const out = reseedPreservingIds(current, incoming);
    expect(out[0]._id).toBe(0);
    expect(out[2]._id).toBe(2);
    expect(out[1]._id).toBe(3); // fresh: max(0,1,2)+1
  });

  it("sub-epsilon timing jitter (<=1ms) still matches", () => {
    const current = [seg(4, 1.0, 2.0, "a")];
    const out = reseedPreservingIds(current, [{ start: 1.0005, end: 2.0004, text: "a" }]);
    expect(out[0]._id).toBe(4);
  });

  it("duplicate value rows are consumed once each (repeated chorus lines)", () => {
    const current = [seg(0, 1, 2, "coro"), seg(1, 1, 2, "coro")];
    const out = reseedPreservingIds(current, [
      { start: 1, end: 2, text: "coro" },
      { start: 1, end: 2, text: "coro" },
    ]);
    expect(out.map((s) => s._id).sort()).toEqual([0, 1]);
  });

  it("fresh ids never collide, even with sparse current ids", () => {
    const current = [seg(10, 1, 2, "a")];
    const out = reseedPreservingIds(current, [
      { start: 1, end: 2, text: "a" },
      { start: 3, end: 4, text: "nueva" },
      { start: 5, end: 6, text: "otra" },
    ]);
    expect(out.map((s) => s._id)).toEqual([10, 11, 12]);
    expect(new Set(out.map((s) => s._id)).size).toBe(3);
  });

  it("empty current seeds everything fresh from 0", () => {
    const out = reseedPreservingIds([], [{ start: 0, end: 1, text: "x" }]);
    expect(out[0]._id).toBe(0);
  });
});
