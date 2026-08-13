/**
 * Word-timing-aware line splitting for the lyrics editor.
 *
 * The render carries per-word timestamps (`words: [{word,start,end,score}]`)
 * end-to-end. When an operator splits a line, each half should inherit the REAL
 * timing of its words — not a character-ratio guess (the old behaviour, which
 * also wrongly left the parent's full `words` array on both children).
 *
 * `splitWordsAtCharOffset` maps a cursor char offset to a word boundary and
 * slices BOTH the text and the `words` array at the same index. It returns null
 * for any case where a word-accurate split isn't possible (degenerate empty
 * half, or text tokens that don't line up 1:1 with `words` — e.g. the text was
 * edited after alignment) so the caller can fall back to char-ratio safely.
 *
 * Pure + framework-free so it's unit-testable in isolation.
 */

/** First finite word.start in order, or null. */
export function firstWordStart(words) {
  if (!Array.isArray(words)) return null;
  for (const w of words) {
    if (w && Number.isFinite(w.start)) return w.start;
  }
  return null;
}

/** Last finite word.end in order, or null. */
export function lastWordEnd(words) {
  if (!Array.isArray(words)) return null;
  for (let i = words.length - 1; i >= 0; i--) {
    if (words[i] && Number.isFinite(words[i].end)) return words[i].end;
  }
  return null;
}

/**
 * @param {string} text
 * @param {Array<{word:string,start:number,end:number,score?:number}>} words
 * @param {number} charOffset  cursor position (char index into `text`)
 * @returns {{textA:string,textB:string,wordsA:Array,wordsB:Array,wordSplitIndex:number}|null}
 */
export function splitWordsAtCharOffset(text, words, charOffset) {
  if (typeof text !== "string" || !Array.isArray(words) || words.length < 2) {
    return null;
  }

  // Tokenize into non-space runs with their [cs, ce) char spans. This handles
  // leading/trailing spaces and runs of multiple spaces correctly.
  const tokens = [];
  const re = /\S+/g;
  let m;
  while ((m = re.exec(text)) !== null) {
    tokens.push({ cs: m.index, ce: m.index + m[0].length });
  }
  if (tokens.length < 2) return null;
  // Positional 1:1 mapping is required to slice `words` by index. If the text
  // was edited after alignment the counts diverge → bail to the fallback.
  if (tokens.length !== words.length) return null;

  const co = Math.max(0, Math.min(Number(charOffset) || 0, text.length));

  // wordSplitIndex = index of the FIRST word that moves to line 2.
  let wordSplitIndex = tokens.length;
  for (let i = 0; i < tokens.length; i++) {
    const { cs, ce } = tokens[i];
    if (co <= cs) {
      wordSplitIndex = i; // cursor before this token (gap / leading whitespace)
      break;
    }
    if (co < ce) {
      // Cursor inside this token → round to the nearest word boundary so a
      // single word's timestamp is never split.
      wordSplitIndex = co - cs >= ce - co ? i + 1 : i;
      break;
    }
    wordSplitIndex = i + 1; // cursor past this token; keep scanning
  }

  // Reject degenerate splits (would create an empty line).
  if (wordSplitIndex <= 0 || wordSplitIndex >= tokens.length) return null;

  const splitChar = tokens[wordSplitIndex].cs;
  const textA = text.slice(0, splitChar).trim();
  const textB = text.slice(splitChar).trim();
  if (!textA || !textB) return null;

  return {
    textA,
    textB,
    wordsA: words.slice(0, wordSplitIndex),
    wordsB: words.slice(wordSplitIndex),
    wordSplitIndex,
  };
}
