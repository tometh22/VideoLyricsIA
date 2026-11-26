import { describe, expect, it } from "vitest";
import { activeWordIndex } from "./karaokeTiming";

describe("karaoke preview follows current text audit 4", () => {
  it("rejects stale word timing after a rewrite with the same number of tokens", () => {
    const stale = [{ word: "hola", start: 0, end: 1 }, { word: "mundo", start: 9, end: 10 }];
    // Backend ass_render._word_timings rejects these because tokens differ.
    // Preview must also use its fallback instead of trusting equal counts.
    expect(activeWordIndex("otra frase", stale, 0, 10, 7)).toBe(activeWordIndex("otra frase", null, 0, 10, 7));
  });
  it("preserves real alignment for case, punctuation and NFC-equivalent text", () => {
    const words = [{ word: "frágil", start: 2, end: 3 }, { word: "espejo", start: 9, end: 10 }];
    expect(activeWordIndex("¡FRA\u0301GIL! espejo", words, 0, 10, 1)).toBe(-1);
    expect(activeWordIndex("¡FRA\u0301GIL! espejo", words, 0, 10, 7)).toBe(0);
  });

});
