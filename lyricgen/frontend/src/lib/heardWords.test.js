import { describe, expect, it } from "vitest";
import { findAlertLineIndex, lineWithAlertInserted } from "./heardWords";

const pajaro = [
  { segment_id: "a", start: 40.2, end: 43.0, text: "Pues, mientras canto, mis penas se desvanecen" },
  { segment_id: "b", start: 43.5, end: 47.8, text: "Tu garantía de reloco se fundió" },
  { segment_id: "c", start: 48.7, end: 51.1, text: "Te lo pido, por favor" },
];

describe("heard words placement", () => {
  it("appends after the anchor word", () => {
    expect(lineWithAlertInserted(pajaro, 1, { text: "dormite ya", anchor_before: "fundio" }))
      .toBe("Tu garantía de reloco se fundió dormite ya");
  });

  it("puts a leading word back and lowercases the old first word", () => {
    const segments = [{ start: 56.5, end: 62.3, text: "Hace un año atrás y siempre tú" }];
    expect(lineWithAlertInserted(segments, 0, { text: "que", anchor_after: "hace", insert_at_word: 0 }))
      .toBe("Que hace un año atrás y siempre tú");
  });

  it("finds the line by id and falls back to time", () => {
    expect(findAlertLineIndex(pajaro, { line_segment_id: "c", start: 0, end: 0 })).toBe(2);
    expect(findAlertLineIndex(pajaro, { line_segment_id: "gone", start: 49, end: 50 })).toBe(2);
  });
});
