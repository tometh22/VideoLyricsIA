import { describe, it, expect } from "vitest";
import {
  splitWordsAtCharOffset,
  firstWordStart,
  lastWordEnd,
  chooseSplitTokenIndex,
  planSegmentSplit,
  lineWords,
  tokenWords,
  mergeLineRows,
  splitLineAtTokens,
} from "./splitWords";

// The motivating defect: a divergent-live line where whisperX glued the next
// phrase's first word ("No") onto this line. Splitting before "No" must give
// line 2 the REAL word time (12.5–12.9), not a char-ratio interpolation.
const WORDS = [
  { word: "tengo", start: 10.0, end: 10.4 },
  { word: "una", start: 10.4, end: 10.6 },
  { word: "mala", start: 10.6, end: 11.0 },
  { word: "noticia", start: 11.0, end: 11.8 },
  { word: "No", start: 12.5, end: 12.9 },
];
const TEXT = "tengo una mala noticia No";

describe("splitWordsAtCharOffset", () => {
  it("splits at the word boundary before 'No' with real word timing", () => {
    const caret = "tengo una mala noticia ".length; // 23, right before "No"
    const r = splitWordsAtCharOffset(TEXT, WORDS, caret);
    expect(r).not.toBeNull();
    expect(r.wordSplitIndex).toBe(4);
    expect(r.textA).toBe("tengo una mala noticia");
    expect(r.textB).toBe("No");
    expect(r.wordsB).toEqual([{ word: "No", start: 12.5, end: 12.9 }]);
    expect(r.wordsA).toHaveLength(4);
    // Real timing, NOT char-ratio:
    expect(firstWordStart(r.wordsB)).toBe(12.5);
    expect(lastWordEnd(r.wordsB)).toBe(12.9);
    expect(firstWordStart(r.wordsA)).toBe(10.0);
    expect(lastWordEnd(r.wordsA)).toBe(11.8);
  });

  it("rounds a cursor mid-word to the nearest boundary (keeps words atomic)", () => {
    // caret inside "noticia" closer to its end → "noticia" stays on line 1.
    const caret = "tengo una mala noti".length; // 19, inside "noticia"
    const r = splitWordsAtCharOffset(TEXT, WORDS, caret);
    expect(r).not.toBeNull();
    // "noti|cia": cs=15, ce=22, caret=19 → 19-15=4 >= 22-19=3 → token stays L1
    expect(r.textA).toBe("tengo una mala noticia");
    expect(r.wordsA).toHaveLength(4);
  });

  it("returns null at offset 0 (would create an empty line 1)", () => {
    expect(splitWordsAtCharOffset(TEXT, WORDS, 0)).toBeNull();
  });

  it("returns null at the end (would create an empty line 2)", () => {
    expect(splitWordsAtCharOffset(TEXT, WORDS, TEXT.length)).toBeNull();
  });

  it("handles multiple/trailing spaces without off-by-one", () => {
    const text = "hola   mundo ";
    const words = [
      { word: "hola", start: 1, end: 2 },
      { word: "mundo", start: 3, end: 4 },
    ];
    const r = splitWordsAtCharOffset(text, words, 5); // in the gap after "hola"
    expect(r).not.toBeNull();
    expect(r.textA).toBe("hola");
    expect(r.textB).toBe("mundo");
    expect(r.wordsB).toEqual([{ word: "mundo", start: 3, end: 4 }]);
  });

  it("returns null when token count != words length (text edited after align)", () => {
    const r = splitWordsAtCharOffset("tengo una mala noticia", WORDS, 10); // 4 tokens, 5 words
    expect(r).toBeNull();
  });

  it("returns null for no/insufficient words (caller uses char-ratio fallback)", () => {
    expect(splitWordsAtCharOffset(TEXT, [], 10)).toBeNull();
    expect(splitWordsAtCharOffset(TEXT, [{ word: "x", start: 1, end: 2 }], 1)).toBeNull();
    expect(splitWordsAtCharOffset(TEXT, null, 10)).toBeNull();
  });
});

describe("firstWordStart / lastWordEnd", () => {
  it("skips entries with missing/NaN timing", () => {
    const words = [
      { word: "a", start: undefined, end: undefined },
      { word: "b", start: 2.0, end: 2.5 },
      { word: "c", start: 3.0, end: NaN },
    ];
    expect(firstWordStart(words)).toBe(2.0);
    expect(lastWordEnd(words)).toBe(2.5);
  });
  it("returns null when no finite timing exists", () => {
    expect(firstWordStart([{ word: "a" }])).toBeNull();
    expect(lastWordEnd([{ word: "a" }])).toBeNull();
    expect(firstWordStart(null)).toBeNull();
  });
});

// Real case, job 248d012f186a (staging, 2-oct-2026). "✂ Dividir" produced
// "Aprendamos a" 140.80–142.17 | "perdonar y seremos perdonados" 142.22–145.65
// with `words: []` on both halves.
const APRENDAMOS = {
  text: "Aprendamos a perdonar y seremos perdonados",
  start: 140.8,
  end: 145.65,
  words: [
    { word: "Aprendamos", start: 140.8, end: 141.4 },
    { word: "a", start: 141.44, end: 141.52 },
    { word: "perdonar", start: 141.58, end: 142.62 },
    { word: "y", start: 143.32, end: 143.4 },
    { word: "seremos", start: 143.42, end: 144.04 },
    { word: "perdonados", start: 144.12, end: 145.46 },
  ],
};

describe("chooseSplitTokenIndex", () => {
  it("cuts at the longest sung pause (perdonar | y, 0.70 s)", () => {
    expect(chooseSplitTokenIndex(APRENDAMOS.text, APRENDAMOS.words)).toBe(3);
  });

  it("without word timing cuts at the most character-balanced boundary", () => {
    expect(chooseSplitTokenIndex(APRENDAMOS.text, null)).toBe(3); // 21 | 19 chars
    // 12|16 vs 16|12: tie → the earlier cut.
    expect(chooseSplitTokenIndex("No hay nadie más que vos y yo", [])).toBe(3);
  });

  it("ignores a long pause that would leave a tiny half", () => {
    const words = [
      { word: "Oh", start: 0, end: 0.2 },
      { word: "cuando", start: 2.0, end: 2.3 },
      { word: "te", start: 2.3, end: 2.4 },
      { word: "vuelva", start: 2.4, end: 2.8 },
      { word: "a", start: 2.8, end: 2.9 },
      { word: "ver", start: 2.9, end: 3.3 },
    ];
    expect(chooseSplitTokenIndex("Oh cuando te vuelva a ver", words)).not.toBe(1);
  });

  it("returns null for a single word", () => {
    expect(chooseSplitTokenIndex("perdonados", null)).toBeNull();
  });
});

describe("planSegmentSplit", () => {
  it("button split keeps each half's words and real boundary times", () => {
    const plan = planSegmentSplit(APRENDAMOS);
    expect(plan.timing).toBe("words");
    expect(plan.a.text).toBe("Aprendamos a perdonar");
    expect(plan.b.text).toBe("y seremos perdonados");
    expect(plan.a.words.map((w) => w.word)).toEqual(["Aprendamos", "a", "perdonar"]);
    expect(plan.b.words.map((w) => w.word)).toEqual(["y", "seremos", "perdonados"]);
    // Outer bounds are the line's own; the inner boundary is the word gap.
    expect(plan.a.start).toBe(140.8);
    expect(plan.a.end).toBe(142.62);
    expect(plan.b.start).toBe(143.32);
    expect(plan.b.end).toBe(145.65);
  });

  it("caret split (Enter) partitions words at the caret, not by chars", () => {
    const plan = planSegmentSplit(APRENDAMOS, { charOffset: "Aprendamos a ".length });
    expect(plan.a.text).toBe("Aprendamos a");
    expect(plan.a.words).toHaveLength(2);
    expect(plan.b.words).toHaveLength(4);
    expect(plan.a.end).toBe(141.52);
    expect(plan.b.start).toBe(141.58);
  });

  it("is robust to punctuation-only tokens that have no word", () => {
    const seg = {
      text: "perdonar — y seremos",
      start: 1,
      end: 5,
      words: [
        { word: "perdonar,", start: 1, end: 2 },
        { word: "y", start: 3, end: 3.2 },
        { word: "seremos", start: 3.3, end: 4.5 },
      ],
    };
    const plan = planSegmentSplit(seg, { splitTokenIndex: 2 });
    expect(plan.a.text).toBe("perdonar —");
    expect(plan.a.words.map((w) => w.word)).toEqual(["perdonar,"]);
    expect(plan.b.words.map((w) => w.word)).toEqual(["y", "seremos"]);
    expect(plan.a.end).toBe(2);
    expect(plan.b.start).toBe(3);
  });

  it("keeps line 2 strictly after line 1 when words touch", () => {
    const seg = {
      text: "hola mundo",
      start: 1,
      end: 3,
      words: [{ word: "hola", start: 1, end: 2 }, { word: "mundo", start: 2, end: 3 }],
    };
    const plan = planSegmentSplit(seg, { charOffset: 5 });
    expect(plan.timing).toBe("words");
    expect(plan.b.start).toBeGreaterThan(plan.a.end);
  });

  it("falls back to char ratio and drops words when text no longer matches", () => {
    const seg = { ...APRENDAMOS, text: "Aprendamos a perdonar y así seremos perdonados" };
    const plan = planSegmentSplit(seg);
    expect(plan.timing).toBe("char_ratio");
    expect(plan.a.words).toBeNull();
    expect(plan.b.words).toBeNull();
    expect(plan.a.start).toBe(140.8);
    expect(plan.b.end).toBe(145.65);
    expect(plan.b.start).toBeGreaterThan(plan.a.end);
  });

  it("falls back to char ratio when the line has no words", () => {
    const { words: _w, ...noWords } = APRENDAMOS;
    const plan = planSegmentSplit(noWords);
    expect(plan.timing).toBe("char_ratio");
    expect(plan.a.text).toBe("Aprendamos a perdonar");
    expect(plan.a.words).toBeNull();
  });

  it("caret inside a word of an unaligned line still cuts exactly there", () => {
    const plan = planSegmentSplit({ text: "perdonary", start: 0, end: 2 }, { charOffset: 8 });
    expect(plan.a.text).toBe("perdonar");
    expect(plan.b.text).toBe("y");
  });

  it("returns null when a half would be empty", () => {
    expect(planSegmentSplit(APRENDAMOS, { charOffset: 0 })).toBeNull();
    expect(planSegmentSplit(APRENDAMOS, { charOffset: APRENDAMOS.text.length })).toBeNull();
    expect(planSegmentSplit({ text: "solo", start: 0, end: 1 })).toBeNull();
  });
});

// Demos 28-oct-2026 (job e59960ce3263): the row had lost `words` but still
// carried the recognizer clocks in provider_evidence; the split saved both
// halves by character ratio and without words.
describe("word clocks survive split and merge", () => {
  const EVIDENCE = [
    { word: "No", start: 3.573, end: 3.793 }, { word: "erro", start: 3.934, end: 4.514 },
    { word: "que", start: 4.594, end: 4.754 }, { word: "já", start: 4.814, end: 4.994 },
    { word: "sarou", start: 5.074, end: 7.136 }, { word: "Não", start: 7.776, end: 7.896 },
    { word: "sei", start: 8.036, end: 8.517 },
  ];
  const ROW = {
    text: "No erro que já sarou? Não sei", start: 3.413, end: 9.017,
    provider_evidence: { words: EVIDENCE },
  };

  it("lineWords recovers clocks from the recognizer evidence of the window", () => {
    expect(lineWords(ROW).map((w) => w.word)).toEqual(["No", "erro", "que", "já", "sarou", "Não", "sei"]);
    expect(lineWords({ ...ROW, start: 6, end: 9 })).toBeNull(); // outside the window
    expect(lineWords({ ...ROW, text: "No erro que já curou? Não sei" })).toBeNull(); // corrected word
  });

  it("splitting a row without words keeps real word timing (not char ratio)", () => {
    const plan = planSegmentSplit(ROW, { charOffset: "No erro que já sarou? ".length });
    expect(plan.timing).toBe("words");
    expect(plan.a).toMatchObject({ text: "No erro que já sarou?", start: 3.413, end: 7.136 });
    expect(plan.b).toMatchObject({ text: "Não sei", start: 7.776, end: 9.017 });
    expect(plan.a.words).toHaveLength(5);
    expect(plan.b.words).toHaveLength(2);
  });

  it("a word moved to another line by hand is skipped, not mis-assigned", () => {
    // "Pode" was moved to the next line: the words still hold it at the end.
    const seg = {
      text: "Enquanto eu for tua prioridade", start: 13.5, end: 16.4,
      words: [
        { word: "Enquanto", start: 13.655, end: 13.996 }, { word: "eu", start: 14.016, end: 14.076 },
        { word: "for", start: 14.176, end: 14.396 }, { word: "tua", start: 14.456, end: 14.696 },
        { word: "prioridade", start: 14.756, end: 15.996 }, { word: "Pode", start: 16.096, end: 16.376 },
      ],
    };
    expect(tokenWords(seg.text, seg.words).map((w) => w.word)).toEqual(
      ["Enquanto", "eu", "for", "tua", "prioridade"]);
    const plan = planSegmentSplit(seg, { splitTokenIndex: 3 });
    expect(plan.timing).toBe("words");
    expect(plan.b.words.map((w) => w.word)).toEqual(["tua", "prioridade"]);
  });

  it("a repeated phrase moved away is disambiguated by the current line window", () => {
    // Review PR #1605: "Te amo" was moved to the previous line; the remaining
    // line kept all five words. The first "Te"/"amo" (1-2 s) are not its own.
    const words = [
      { word: "Te", start: 1.0, end: 1.5 }, { word: "amo", start: 2.0, end: 2.5 },
      { word: "Te", start: 5.0, end: 5.4 }, { word: "amo", start: 6.0, end: 6.4 },
      { word: "siempre", start: 7.0, end: 7.8 },
    ];
    const seg = { text: "Te amo siempre", start: 5, end: 8, words };
    expect(lineWords(seg).map((w) => w.start)).toEqual([5.0, 6.0, 7.0]);
    const plan = planSegmentSplit(seg, { splitTokenIndex: 2 });
    expect(plan.timing).toBe("words");
    expect(plan.a).toMatchObject({ text: "Te amo", start: 5, end: 6.4 });
    expect(plan.a.words.map((w) => w.start)).toEqual([5.0, 6.0]);
    expect(plan.b).toMatchObject({ text: "siempre", start: 7.0, end: 8 });
  });

  it("declines recovery when the occurrence of a repeated word is ambiguous", () => {
    const words = [
      { word: "Te", start: 1.0, end: 1.5 }, { word: "amo", start: 2.0, end: 2.5 },
      { word: "Te", start: 5.0, end: 5.4 }, { word: "amo", start: 6.0, end: 6.4 },
      { word: "siempre", start: 7.0, end: 7.8 },
    ];
    // Window covers both "Te amo": no way to tell which pair is this line's.
    const seg = { text: "Te amo siempre", start: 0.8, end: 8, words };
    expect(tokenWords(seg.text, words)).toBeNull();
    expect(lineWords(seg)).toBeNull();
    // Old path: char-ratio timing, no fabricated clocks.
    const plan = planSegmentSplit(seg, { splitTokenIndex: 2 });
    expect(plan.timing).toBe("char_ratio");
    expect(plan.a.words).toBeNull();
    expect(plan.b.words).toBeNull();
  });

  it("mergeLineRows joins recovered clocks and keeps the outer bounds", () => {
    const a = { _id: 1, text: "No erro que já sarou?", start: 3.413, end: 7.566, provider_evidence: { words: EVIDENCE } };
    const b = { _id: 2, text: "Não sei", start: 7.616, end: 9.017, words: EVIDENCE.slice(5) };
    const merged = mergeLineRows(a, b);
    expect(merged).toMatchObject({ _id: 1, text: "No erro que já sarou? Não sei", start: 3.413, end: 9.017 });
    expect(merged.words).toHaveLength(7);
    expect(mergeLineRows({ ...a, provider_evidence: undefined }, b).words).toBeUndefined();
  });

  it("splitLineAtTokens cuts several times keeping every slice of words", () => {
    const parts = splitLineAtTokens(ROW, [5, 2]);
    expect(parts.map((p) => p.text)).toEqual(["No erro", "que já sarou?", "Não sei"]);
    expect(parts.map((p) => p.words.length)).toEqual([2, 3, 2]);
    expect(parts[0].start).toBe(3.413);
    expect(parts[0].end).toBe(4.514);
    expect(parts[1].start).toBe(4.594);
    expect(parts[2].end).toBe(9.017);
    expect(splitLineAtTokens(ROW, [0])).toBeNull();
    expect(splitLineAtTokens(ROW, [7])).toBeNull();
  });
});
