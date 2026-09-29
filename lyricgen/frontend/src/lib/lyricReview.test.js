import { describe, expect, it } from "vitest";
import {
  applyPunctuation,
  applyReviewItem,
  changedWords,
  dismissReviewItem,
  replaceWords,
} from "./lyricReview";

const occ = (segment_id, fix, start = 0) => ({ line_segment_id: segment_id, start, end: start + 1, fix });

describe("lyric review fixes", () => {
  it("replaces every repetition in the line and keeps the screen punctuation", () => {
    expect(replaceWords("Anda a lavártelo, anda a lavártelo", "lavártelo,", "lavártelos,"))
      .toBe("Anda a lavártelos, anda a lavártelos");
    expect(replaceWords("¿Cuando vuelva quiero verte", "¿Cuando vuelva", "¿Cuando vuelvas"))
      .toBe("¿Cuando vuelvas quiero verte");
  });

  it("still finds the words after the reviewer removed the question marks", () => {
    expect(replaceWords("Cuando vuelva quiero verte", "¿Cuando vuelva", "¿Cuando vuelvas"))
      .toBe("Cuando vuelvas quiero verte");
  });

  it("returns null when the words are no longer in the line", () => {
    expect(replaceWords("Otra cosa", "Pásalo", "Pásenlo")).toBeNull();
  });

  it("fixes question marks the way UMG asked", () => {
    expect(applyPunctuation("¿Cuando vuelvas?", "remove_question")).toBe("Cuando vuelvas");
    expect(applyPunctuation("¿Qué lindo añorar la zamba de ayer?", "exclaim"))
      .toBe("¡Qué lindo añorar la zamba de ayer!");
  });

  it("applies one grouped item on every occurrence", () => {
    const segments = [
      { _id: 1, segment_id: "a", start: 10, end: 12, text: "¿Cuando vuelvas?" },
      { _id: 2, segment_id: "b", start: 20, end: 22, text: "Quiero verte" },
      { _id: 3, segment_id: "c", start: 30, end: 32, text: "¿Cuando vuelvas?" },
    ];
    const fix = { type: "punctuation", mode: "remove_question" };
    const { segments: next, applied } = applyReviewItem(segments, {
      occurrences: [occ("a", fix, 10), occ("c", fix, 30)],
    });
    expect(applied).toBe(2);
    expect(next.map((s) => s.text)).toEqual(["Cuando vuelvas", "Quiero verte", "Cuando vuelvas"]);
  });

  it("applies the alternative the reviewer picked", () => {
    const segments = [{ _id: 1, segment_id: "a", start: 78, end: 80, text: "Vos sos Román" }];
    const item = { occurrences: [occ("a", { type: "replace", find: "Román", replace: "lo más" }, 78)] };
    expect(applyReviewItem(segments, item).segments[0].text).toBe("Vos sos lo más");
    expect(applyReviewItem(segments, item, { alternative: { replace: "la mar" } }).segments[0].text)
      .toBe("Vos sos la mar");
  });

  it("merges a lonely word into the next line", () => {
    const segments = [
      { _id: 1, segment_id: "a", start: 150, end: 151, text: "Que" },
      { _id: 2, segment_id: "b", start: 151.2, end: 154, text: "Dejó el temor de tener que olvidar" },
    ];
    const { segments: next } = applyReviewItem(segments, {
      occurrences: [occ("a", { type: "merge", direction: "next" }, 150)],
    });
    expect(next).toHaveLength(1);
    expect(next[0]).toMatchObject({ text: "Que Dejó el temor de tener que olvidar", start: 150, end: 154 });
  });

  it("adds a missing chorus pass as a new line", () => {
    const segments = [{ _id: 4, segment_id: "z", start: 75.9, end: 78.7, text: "Te está poniendo nervioso" }];
    const { segments: next } = applyReviewItem(segments, {
      occurrences: [occ(null, { type: "new_line", text: "borracho y agresivo", start: 81.3, end: 88.9 }, 81.3)],
    }, { mint: () => ({ _id: 9, segment_id: "new" }) });
    expect(next[1]).toMatchObject({ _id: 9, text: "Borracho y agresivo", start: 81.3 });
  });

  it("inserts missing words where they were sung", () => {
    const segments = [{ _id: 1, segment_id: "b", start: 43.5, end: 47.8, text: "Tu garantía de reloco se fundió" }];
    const fix = { type: "insert", text: "dormite ya", anchor_before: "fundio", anchor_after: "", insert_at_word: 6 };
    expect(applyReviewItem(segments, { occurrences: [occ("b", fix, 46)] }).segments[0].text)
      .toBe("Tu garantía de reloco se fundió dormite ya");
  });

  it("stores every key of a dismissed item on the line", () => {
    const segments = [{ _id: 1, segment_id: "a", start: 0, end: 1, text: "Hola" }];
    const next = dismissReviewItem(segments, { keys: ["k1", "k2"], occurrences: [occ("a", {}, 0)] });
    expect(next[0].qa_dismissed).toEqual(["k1", "k2"]);
  });

  it("marks only the words that change", () => {
    expect(changedWords("Con tus pasos", "Con tres pasos")).toEqual([
      { word: "Con", changed: false }, { word: "tres", changed: true }, { word: "pasos", changed: false },
    ]);
  });
});
