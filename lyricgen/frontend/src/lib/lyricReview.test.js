import { describe, expect, it } from "vitest";
import {
  applyPunctuation, applyReviewItem, diffTokens, dismissReviewItem, replaceWords, validItems,
} from "./lyricReview";

const occ = (segment_id, fix, start = 0) => ({ line_segment_id: segment_id, start, end: start + 1, fix });

describe("lyric review fixes", () => {
  it("corrects only the repetition that was heard wrong", () => {
    expect(replaceWords("Te quiero, te quiero, mi amor", "quiero,", "extraño,", { atWord: 3, scope: "one" }))
      .toBe("Te quiero, te extraño, mi amor");
    expect(replaceWords("Anda a lavártelo, anda a lavártelo", "lavártelo,", "lavártelos,"))
      .toBe("Anda a lavártelos, anda a lavártelos");
  });

  it("keeps the screen punctuation and never edits inside another word", () => {
    expect(replaceWords("¿Cuando vuelva quiero verte", "¿Cuando vuelva", "¿Cuando vuelvas"))
      .toBe("¿Cuando vuelvas quiero verte");
    expect(replaceWords("Cuando vuelva quiero verte", "¿Cuando vuelva", "¿Cuando vuelvas"))
      .toBe("Cuando vuelvas quiero verte");
    expect(replaceWords("Estuve pensando en vos", "tu", "tú")).toBeNull();
  });

  it("fixes question marks only on the marked clause", () => {
    expect(applyPunctuation("¿Cuando vuelvas?", "remove_question")).toBe("Cuando vuelvas");
    expect(applyPunctuation("¿Qué lindo añorar la zamba de ayer?", "exclaim")).toBe("¡Qué lindo añorar la zamba de ayer!");
    expect(applyPunctuation("¿Cómo estás? ¿Que te pasa?", "accent_question", "¿Que te pasa?"))
      .toBe("¿Cómo estás? ¿Qué te pasa?");
  });

  it("applies one grouped item on every occurrence and reports the changed lines", () => {
    const segments = [
      { _id: 1, segment_id: "a", start: 10, end: 12, text: "¿Cuando vuelvas?" },
      { _id: 2, segment_id: "b", start: 20, end: 22, text: "Quiero verte" },
      { _id: 3, segment_id: "c", start: 30, end: 32, text: "¿Cuando vuelvas?" },
    ];
    const fix = { type: "punctuation", mode: "remove_question" };
    const result = applyReviewItem(segments, { occurrences: [occ("a", fix, 10), occ("c", fix, 30)] });
    expect(result.applied).toBe(2);
    expect(result.lines).toEqual([1, 3]);
    expect(result.segments.map((s) => s.text)).toEqual(["Cuando vuelvas", "Quiero verte", "Cuando vuelvas"]);
  });

  it("reports 0 applied instead of guessing when the line is gone", () => {
    const segments = [{ _id: 1, segment_id: "x", start: 0, end: 1, text: "Hola" }];
    const result = applyReviewItem(segments, { occurrences: [occ("gone", { type: "replace", find: "Hola", replace: "Chau" })] });
    expect(result.applied).toBe(0);
    expect(result.segments).toBe(segments);
  });

  it("applies the option the reviewer picked, including a different fix", () => {
    const segments = [{ _id: 1, segment_id: "a", start: 78, end: 80, text: "Vos sos Román" }];
    const item = { occurrences: [occ("a", { type: "replace", find: "Román", replace: "lo más" }, 78)] };
    expect(applyReviewItem(segments, item).segments[0].text).toBe("Vos sos lo más");
    expect(applyReviewItem(segments, item, { alternative: { replace: "la mar" } }).segments[0].text).toBe("Vos sos la mar");
    const question = [{ _id: 1, segment_id: "q", start: 0, end: 1, text: "¿Donde irás?" }];
    const qItem = { occurrences: [occ("q", { type: "punctuation", mode: "remove_question" })] };
    expect(applyReviewItem(question, qItem, {
      alternative: { fix: { type: "punctuation", mode: "accent_question" } },
    }).segments[0].text).toBe("¿Dónde irás?");
  });

  it("merges with the line the server saw, keeping word timings", () => {
    const segments = [
      { _id: 1, segment_id: "a", start: 150, end: 151, text: "Que", words: [{ word: "Que" }] },
      { _id: 2, segment_id: "b", start: 151.2, end: 154, text: "Dejó el temor", words: [{ word: "Dejó" }] },
    ];
    const { segments: next } = applyReviewItem(segments, {
      occurrences: [occ("a", { type: "merge", direction: "next", other_segment_id: "b", expect: "que" }, 150)],
    });
    expect(next).toHaveLength(1);
    expect(next[0]).toMatchObject({ text: "Que Dejó el temor", start: 150, end: 154 });
    expect(next[0].words).toHaveLength(2);
    // Si la otra línea ya no está al lado, no se une con cualquiera.
    const gone = [segments[0], { _id: 3, segment_id: "z", start: 152, end: 153, text: "Otra" }];
    expect(applyReviewItem(gone, {
      occurrences: [occ("a", { type: "merge", direction: "next", other_segment_id: "b" }, 150)],
    }).applied).toBe(0);
  });

  it("propagates a chorus correction verbatim only if the line is unchanged", () => {
    const segments = [{ _id: 1, segment_id: "a", start: 0, end: 2, text: "no me dejes solo nunca" }];
    const fix = { type: "set_text", text: "¡No me dejes sola nunca!", expect: "no me dejes solo nunca" };
    expect(applyReviewItem(segments, { occurrences: [occ("a", fix)] }).segments[0].text).toBe("¡No me dejes sola nunca!");
    const edited = [{ ...segments[0], text: "otra cosa" }];
    expect(applyReviewItem(edited, { occurrences: [occ("a", fix)] }).applied).toBe(0);
  });

  it("re-cuts two lines like the official lyrics and adjusts timing without overlaps", () => {
    const segments = [
      { _id: 1, segment_id: "a", start: 0, end: 2, text: "Cuando vuelvas quiero" },
      { _id: 2, segment_id: "b", start: 2.1, end: 4, text: "Verte a solas" },
    ];
    const relayout = { type: "relayout", lines: [
      { segment_id: "a", text: "Cuando vuelvas", expect: "cuando vuelvas quiero" },
      { segment_id: "b", text: "Quiero verte a solas", expect: "verte a solas" },
    ] };
    expect(applyReviewItem(segments, { occurrences: [occ("a", relayout)] }).segments.map((s) => s.text))
      .toEqual(["Cuando vuelvas", "Quiero verte a solas"]);
    const timing = applyReviewItem(segments, { occurrences: [occ("a", { type: "timing", end: 9 })] });
    expect(timing.segments[0].end).toBeCloseTo(2.05);
    expect(timing.segments[0].locked).toBe(true);
  });

  it("adds a missing chorus pass as a new line", () => {
    const segments = [{ _id: 4, segment_id: "z", start: 75.9, end: 78.7, text: "Te está poniendo nervioso" }];
    const { segments: next } = applyReviewItem(segments, {
      occurrences: [occ(null, { type: "new_line", text: "borracho y agresivo", start: 81.3, end: 88.9 }, 81.3)],
    }, { mint: () => ({ _id: 9, segment_id: "new" }) });
    expect(next[1]).toMatchObject({ _id: 9, text: "Borracho y agresivo", start: 81.3 });
  });

  it("stores every key of a dismissed item on the line", () => {
    const segments = [{ _id: 1, segment_id: "a", start: 0, end: 1, text: "Hola" }];
    const next = dismissReviewItem(segments, { keys: ["k1", "k2"], occurrences: [occ("a", {}, 0)] });
    expect(next[0].qa_dismissed).toEqual(["k1", "k2"]);
  });

  it("shows what is added and what is removed, punctuation included", () => {
    expect(diffTokens("Con tus pasos", "Con tres pasos")).toEqual([
      { text: "Con", op: "same" }, { text: "tus", op: "del" }, { text: "tres", op: "ins" }, { text: "pasos", op: "same" },
    ]);
    expect(diffTokens("¿Cuando vuelva?", "Cuando vuelva")).toEqual([
      { text: "Cuando", op: "punct", before: "¿Cuando" }, { text: "vuelva", op: "punct", before: "vuelva?" },
    ]);
    expect(diffTokens("se fundió", "se fundió dormite ya")).toEqual([
      { text: "se fundió", op: "same" }, { text: "dormite ya", op: "ins" },
    ]);
  });

  it("drops malformed items from an older or failing server", () => {
    expect(validItems([null, { id: "a" }, { id: "b", occurrences: [] }, { id: "c", occurrences: [{ fix: {} }] }]))
      .toEqual([{ id: "c", occurrences: [{ fix: {} }] }]);
    expect(validItems(undefined)).toEqual([]);
  });
});
