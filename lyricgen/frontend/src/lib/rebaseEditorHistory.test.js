import { describe, expect, it } from "vitest";
import { rebaseEditorHistory, rebaseFocusedSegmentId } from "./rebaseEditorHistory";
const row = (id, text, start = id * 2) => ({ _id: id, segment_id: `line-${id}`, text, start, end: start + 1 });
const original = [row(0, "old local"), row(1, "old remote")];
const before = [row(0, "new local"), original[1]];
const history = [{ segments: original, focusedId: 0 }];

describe("undo history after durable merges", () => {
  it("maps focus by the occurrence of legacy repeated identities", () => {
    const before = [row(0, "one"), { ...row(1, "two"), segment_id: "line-0" }];
    const after = [{ ...before[0], _id: 7 }, { ...before[1], text: "corrected", _id: 8 }];
    expect(rebaseFocusedSegmentId(before, after, 1)).toBe(8);
  });
  it("keeps independent remote corrections while preserving local undo and focus", () => {
    const [rebased] = rebaseEditorHistory(history, before, [before[0], row(1, "remote correction")]);
    expect(rebased.segments.map(line => line.text)).toEqual(["old local", "remote correction"]);
    expect(rebased.focusedId).toBe(rebased.segments[0]._id);
  });
  it("keeps current word evidence and review metadata on untouched rows", () => {
    const remote = { ...before[1], review: true, words: [{ word: "remote", start: 2, end: 3 }] };
    const [rebased] = rebaseEditorHistory(history, before, [before[0], remote]);
    expect(rebased.segments[1]).toMatchObject(remote);
    expect(rebased.segments[0].text).toBe("old local");
  });
  it("preserves remote additions and deletions", () => {
    const [rebased] = rebaseEditorHistory(history, before, [before[0], row(2, "new chorus")]);
    expect(rebased.segments.map(line => line.segment_id)).toEqual(["line-0", "line-2"]);
    expect(rebased.segments[0].text).toBe("old local");
  });
  it("discards an ambiguous same-line undo instead of overwriting the merged value", () => {
    expect(rebaseEditorHistory(history, before, [row(0, "remote same line"), before[1]])).toEqual([]);
  });
  it("does not resurrect a remotely deleted line that also had a local edit", () => {
    expect(rebaseEditorHistory(history, before, [before[1]])).toEqual([]);
  });
  it("carries consecutive remote merges through the same undo snapshot", () => {
    const first = [before[0], row(1, "first remote"), row(2, "added")];
    const second = [before[0], row(1, "second remote"), first[2]];
    const one = rebaseEditorHistory(history, before, first);
    const [two] = rebaseEditorHistory(one, first, second);
    expect(two.segments.map(line => line.text)).toEqual(["old local", "second remote", "added"]);
  });
  it("drops no-op snapshots and clears focus on a remotely deleted selection", () => {
    expect(rebaseEditorHistory([{ segments: before, focusedId: 0 }], before, before)).toEqual([]);
    const [rebased] = rebaseEditorHistory([{ ...history[0], focusedId: 1 }], before, [before[0]]);
    expect(rebased.focusedId).toBeNull();
  });
});
