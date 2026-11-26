import { describe, expect, it } from "vitest";
import { applyDisplaySuggestions, resolveDisplaySuggestions } from "./displaySegmentationSuggestions";

const w = (word, start, end) => ({ word, start, end });
const LONG = {
  _id: 5, segment_id: "s5", start: 22.4, end: 26.0,
  text: "Ahora tengo un culo inédito que se lleva todos los méritos",
  words: [
    w("Ahora", 22.64, 22.9), w("tengo", 22.95, 23.2), w("un", 23.25, 23.3), w("culo", 23.35, 23.7),
    w("inédito", 23.75, 24.14), w("que", 24.46, 24.6), w("se", 24.62, 24.7), w("lleva", 24.75, 25.0),
    w("todos", 25.05, 25.3), w("los", 25.32, 25.4), w("méritos", 25.45, 25.88),
  ],
};
const EN = { _id: 6, segment_id: "s6", start: 27.0, end: 27.4, text: "En", words: [w("En", 27.05, 27.3)] };
const NEXT = {
  _id: 7, segment_id: "s7", start: 27.5, end: 29.0, text: "la noche que te vi",
  words: [w("la", 27.5, 27.6), w("noche", 27.7, 28.0), w("que", 28.1, 28.2), w("te", 28.25, 28.3), w("vi", 28.4, 28.9)],
};
const SPLIT = {
  id: "split-1", kind: "split", index: 0, segment_id: "s5", text: LONG.text, start: LONG.start, end: LONG.end,
  cuts: [5], parts: [{ text: "Ahora tengo un culo inédito" }, { text: "que se lleva todos los méritos" }],
};
const MERGE = {
  id: "merge-1", kind: "merge", index: 1, segment_id: "s6", next_segment_id: "s7",
  text: "En", next_text: "la noche que te vi", start: 27.0, end: 29.0,
};
let minted = 0;
const mintSegmentId = () => `n-${++minted}`;

describe("resolveDisplaySuggestions", () => {
  it("keeps only suggestions whose line text is still exactly the same", () => {
    const rows = [LONG, EN, NEXT];
    expect(resolveDisplaySuggestions([SPLIT, MERGE], rows).map((s) => [s.id, s.rowId])).toEqual([
      ["split-1", 5], ["merge-1", 6],
    ]);
    const edited = [{ ...LONG, text: `${LONG.text}!` }, EN, { ...NEXT, text: "la noche" }];
    expect(resolveDisplaySuggestions([SPLIT, MERGE], edited)).toEqual([]);
  });

  it("hides dismissed suggestions and matches rows without segment_id by text + start", () => {
    const { segment_id: _sid, ...noId } = LONG;
    expect(resolveDisplaySuggestions([SPLIT], [noId]).map((s) => s.rowId)).toEqual([5]);
    expect(resolveDisplaySuggestions([SPLIT, MERGE], [LONG, EN, NEXT], new Set(["split-1"]))
      .map((s) => s.id)).toEqual(["merge-1"]);
  });
});

describe("applyDisplaySuggestions", () => {
  it("splits at the suggested token keeping each half's word clocks", () => {
    const [item] = resolveDisplaySuggestions([SPLIT], [LONG]);
    const { segments, applied } = applyDisplaySuggestions([LONG], [item], { nextId: 10, mintSegmentId });
    expect(segments.map((s) => s.text)).toEqual(["Ahora tengo un culo inédito", "que se lleva todos los méritos"]);
    expect(segments[0].words.map((x) => x.word)).toEqual(["Ahora", "tengo", "un", "culo", "inédito"]);
    expect(segments[1].words).toHaveLength(6);
    // Outer bounds kept; inner boundary from the words.
    expect(segments[0].start).toBe(22.4);
    expect(segments[0].end).toBe(24.14);
    expect(segments[1].start).toBe(24.46);
    expect(segments[1].end).toBe(26.0);
    expect(new Set(segments.map((s) => s.segment_id)).size).toBe(2);
    expect(segments.map((s) => s.segment_id)).not.toContain("s5");
    expect(applied).toEqual([expect.objectContaining({ kind: "split", words: true })]);
  });

  it("joins with the next line concatenating words; accept-all applies both", () => {
    const rows = [LONG, EN, NEXT];
    const items = resolveDisplaySuggestions([SPLIT, MERGE], rows);
    const { segments, applied } = applyDisplaySuggestions(rows, items, { nextId: 10, mintSegmentId });
    expect(segments.map((s) => s.text)).toEqual([
      "Ahora tengo un culo inédito", "que se lleva todos los méritos", "En la noche que te vi",
    ]);
    const merged = segments[2];
    expect(merged.words.map((x) => x.word)).toEqual(["En", "la", "noche", "que", "te", "vi"]);
    expect(merged.start).toBe(27.0);
    expect(merged.end).toBe(29.0);
    expect(merged._id).toBe(6);
    expect(applied.map((a) => a.kind)).toEqual(["split", "merge"]);
  });

  it("recovers lost words from the recognizer evidence before splitting", () => {
    const { words, ...lost } = LONG;
    const row = { ...lost, provider_evidence: { words } };
    const [item] = resolveDisplaySuggestions([SPLIT], [row]);
    const { segments } = applyDisplaySuggestions([row], [item], { nextId: 10, mintSegmentId });
    expect(segments[0].words).toHaveLength(5);
    expect(segments[1].words).toHaveLength(6);
    expect(segments[0].end).toBe(24.14);
  });
});
