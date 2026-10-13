import { describe, expect, it } from "vitest";

import { lineStructureProperties, mergeTimingMode, splitTimingMode } from "./lineStructureEvent";
import { planSegmentSplit } from "./splitWords";

const word = (text, start, end) => ({ word: text, start, end });

describe("splitTimingMode", () => {
  it("words cuando el corte usa los tiempos reales de las palabras", () => {
    const seg = {
      text: "hoy te vi pasar", start: 1, end: 3,
      words: [word("hoy", 1, 1.3), word("te", 1.3, 1.5), word("vi", 1.5, 1.8), word("pasar", 2.2, 2.9)],
    };
    const plan = planSegmentSplit(seg, { charOffset: 10 });
    expect(splitTimingMode(seg, plan)).toBe("words");
  });

  it("no_words cuando la línea no tiene palabras", () => {
    const seg = { text: "hoy te vi pasar", start: 1, end: 3 };
    expect(splitTimingMode(seg, planSegmentSplit(seg, { charOffset: 10 }))).toBe("no_words");
  });

  it("stale_words cuando hay palabras pero el texto ya no coincide", () => {
    const seg = {
      text: "hoy te volví a ver pasar", start: 1, end: 3,
      words: [word("hoy", 1, 1.3), word("te", 1.3, 1.5), word("vi", 1.5, 1.8), word("pasar", 2.2, 2.9)],
    };
    expect(splitTimingMode(seg, planSegmentSplit(seg, { charOffset: 12 }))).toBe("stale_words");
  });

  it("null cuando no se partió", () => {
    expect(splitTimingMode({ text: "hola" }, null)).toBeNull();
  });
});

describe("mergeTimingMode", () => {
  const w = { words: [word("a", 0, 1)] };
  it("distingue los dos lados, uno solo y ninguno", () => {
    expect(mergeTimingMode(w, w)).toBe("words");
    expect(mergeTimingMode(w, {})).toBe("one_side");
    expect(mergeTimingMode({}, w)).toBe("one_side");
    expect(mergeTimingMode({ words: [] }, {})).toBe("no_words");
  });
});

describe("lineStructureProperties", () => {
  it("suma una acción masiva en un solo evento sin texto de la letra", () => {
    const props = lineStructureProperties("split", "bulk", [
      { mode: "words", start: 1, end: 3 },
      { mode: "stale_words", start: 4, end: 5.5 },
      { mode: null, start: 6, end: 7 },
    ]);
    expect(props).toEqual({
      structure_op: "split", trigger: "bulk", count: 2,
      words_count: 1, stale_words_count: 1, one_side_count: 0, no_words_count: 0,
      duration_ms: 3500,
    });
    expect(Object.values(props).every((v) => typeof v === "number" || ["split", "bulk"].includes(v))).toBe(true);
  });

  it("null cuando nada cambió", () => {
    expect(lineStructureProperties("merge", "button", [])).toBeNull();
    expect(lineStructureProperties("merge", "button", [{ mode: null }])).toBeNull();
  });
});
