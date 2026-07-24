import { describe, it, expect } from "vitest";
import { splitWordsAtCharOffset, firstWordStart, lastWordEnd } from "./splitWords";

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
