import { describe, expect, it } from "vitest";
import {
  applyHeardWordsAlert,
  dismissHeardWordsAlert,
  findAlertLineIndex,
  previewHeardWordsAlert,
} from "./heardWords";

const pajaro = [
  { segment_id: "a", start: 40.2, end: 43.0, text: "Pues, mientras canto, mis penas se desvanecen" },
  { segment_id: "b", start: 43.5, end: 47.8, text: "Tu garantía de reloco se fundió" },
  { segment_id: "c", start: 48.7, end: 51.1, text: "Te lo pido, por favor" },
];

describe("heard words", () => {
  it("appends the phrase lost when the client quote was pasted over the line", () => {
    const alert = {
      text: "dormite ya", start: 46.1, end: 46.9, action: "insert",
      line_segment_id: "b", anchor_before: "fundio", anchor_after: "", insert_at_word: 6,
    };
    const next = applyHeardWordsAlert(pajaro, alert);
    expect(next[1].text).toBe("Tu garantía de reloco se fundió dormite ya");
    expect(next[0]).toBe(pajaro[0]);
  });

  it("puts a leading word back at the start and lowercases the old first word", () => {
    const segments = [
      { segment_id: "x", start: 54.5, end: 56.3, text: "Te recuerdo más" },
      { segment_id: "y", start: 56.5, end: 62.3, text: "Hace un año atrás y siempre tú" },
    ];
    const alert = {
      text: "que", start: 56.6, end: 56.8, action: "insert",
      line_segment_id: "y", anchor_before: "", anchor_after: "hace", insert_at_word: 0,
    };
    expect(applyHeardWordsAlert(segments, alert)[1].text)
      .toBe("Que hace un año atrás y siempre tú");
  });

  it("uses the anchors even when the line changed locally since the save", () => {
    const edited = pajaro.map((s) => (s.segment_id === "b"
      ? { ...s, text: "Tu garantía de reloco se fundió," } : s));
    const alert = {
      text: "dormite ya", start: 46.1, end: 46.9, action: "insert",
      line_segment_id: "b", anchor_before: "fundio", insert_at_word: 99,
    };
    expect(applyHeardWordsAlert(edited, alert)[1].text)
      .toBe("Tu garantía de reloco se fundió, dormite ya");
  });

  it("creates a new line in the gap for a missing chorus pass", () => {
    let n = 0;
    const mint = () => ({ _id: 100 + n, segment_id: `new-${n++}` });
    const alert = {
      text: "borracho y agresivo", start: 81.3, end: 88.9, action: "new_line",
      new_line: { start: 81.34, end: 88.9 },
    };
    const segments = [{ segment_id: "z", start: 75.9, end: 78.7, text: "Te está poniendo nervioso" }];
    const next = applyHeardWordsAlert(segments, alert, mint);
    expect(next).toHaveLength(2);
    expect(next[1]).toMatchObject({ segment_id: "new-0", start: 81.34, end: 88.9, text: "Borracho y agresivo" });
  });

  it("stores 'no se canta' on the nearest line without duplicating", () => {
    const alert = { key: "dormite ya@46", start: 46.1, end: 46.9 };
    const once = dismissHeardWordsAlert(pajaro, alert);
    expect(once[1].heard_dismissed).toEqual(["dormite ya@46"]);
    expect(dismissHeardWordsAlert(once, alert)[1].heard_dismissed).toEqual(["dormite ya@46"]);
  });

  it("falls back to time when the segment id is gone", () => {
    expect(findAlertLineIndex(pajaro, { line_segment_id: "gone", start: 49, end: 50 })).toBe(2);
  });

  it("previews the change before applying it", () => {
    const alert = { text: "dormite ya", start: 46.1, end: 46.9, action: "insert", line_segment_id: "b", anchor_before: "fundio" };
    expect(previewHeardWordsAlert(pajaro, alert)).toEqual({
      kind: "insert",
      before: "Tu garantía de reloco se fundió",
      after: "Tu garantía de reloco se fundió dormite ya",
    });
  });
});
