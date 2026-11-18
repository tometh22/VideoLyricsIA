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

// ─── Whole-line split planning ("✂ Dividir", Enter, auto-split) ────────────
//
// `planSegmentSplit` is the single place that decides WHERE a line is cut,
// WHICH words go to each half and WHEN each half starts/ends. Every split in
// the editor goes through it so no path silently drops `words` again
// (job 248d012f186a, 2-oct-2026: "✂ Dividir" cut "Aprendamos a | perdonar y
// seremos perdonados" at the canvas wrap, interpolated the time by characters
// and saved both halves with `words: []`).

const MIN_PAUSE_S = 0.15; // a word gap shorter than this is not a phrase break
const MIN_SIDE_SHARE = 0.25; // each half keeps ≥ 25 % of the line's characters
const FALLBACK_GAP_S = 0.05; // char-ratio path: gap between the two halves
const MIN_HALF_S = 0.3; // char-ratio path: minimum duration of each half
const TOUCH_GAP_S = 0.02; // word path: keep line 2 strictly after line 1
const OVERLAP_TOLERANCE_S = 0.05; // aligner jitter allowed between halves

/** Non-space runs of `text` with their [cs, ce) char spans. */
export function tokenSpans(text) {
  const tokens = [];
  if (typeof text !== "string") return tokens;
  const re = /\S+/g;
  let m;
  while ((m = re.exec(text)) !== null) {
    tokens.push({ cs: m.index, ce: m.index + m[0].length, text: m[0] });
  }
  return tokens;
}

/** Letters/digits only, accent- and case-insensitive ("¡Perdón," → "perdon"). */
function normWord(value) {
  return String(value ?? "")
    .normalize("NFD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase()
    .replace(/[^\p{L}\p{N}]+/gu, "");
}

/**
 * Map text tokens onto `words`. Returns `bounds` where `bounds[k]` is the index
 * of the first word that belongs to tokens[k..] — i.e. cutting the TEXT before
 * token k cuts `words` at `bounds[k]`. Null when the words can't be mapped.
 *
 * 1:1 by position when the counts match (the contract the renderer and the
 * karaoke preview already use). Otherwise punctuation-only entries on either
 * side ("—", "¡", a stray "," word) are ignored and the remaining content
 * tokens must still pair 1:1. Anything else (the text was rewritten after
 * alignment) is unmappable → the caller falls back to char-ratio timing.
 */
export function wordBoundaries(tokens, words) {
  if (!Array.isArray(tokens) || !Array.isArray(words) || words.length < 2) return null;
  if (!words.every((w) => w && typeof w === "object")) return null;
  if (tokens.length < 2) return null;
  if (tokens.length === words.length) return tokens.map((_, k) => k).concat(tokens.length);

  const contentTokens = [];
  tokens.forEach((t, i) => { if (normWord(t.text)) contentTokens.push(i); });
  const contentWords = [];
  words.forEach((w, i) => { if (normWord(w.word ?? w.text)) contentWords.push(i); });
  if (contentTokens.length < 2 || contentTokens.length !== contentWords.length) return null;

  const bounds = new Array(tokens.length + 1);
  let j = contentTokens.length; // content index of the first content token ≥ k
  for (let k = tokens.length; k >= 0; k--) {
    if (k < tokens.length && contentTokens[j - 1] === k) j -= 1;
    bounds[k] = j < contentWords.length ? contentWords[j] : words.length;
  }
  return bounds;
}

/** Cursor char offset → index of the first token that moves to line 2. */
function tokenIndexAtCharOffset(tokens, charOffset, textLength) {
  const co = Math.max(0, Math.min(Number(charOffset) || 0, textLength));
  for (let i = 0; i < tokens.length; i++) {
    const { cs, ce } = tokens[i];
    if (co <= cs) return i;
    // Inside a token → nearest word boundary (a word's timestamp is atomic).
    if (co < ce) return co - cs >= ce - co ? i + 1 : i;
  }
  return tokens.length;
}

/**
 * Where the "✂ Dividir" button cuts a line (no cursor): index of the first
 * token of line 2. Among cuts that leave each half ≥ 25 % of the characters,
 * the LONGEST sung pause between consecutive words wins (that is where the
 * singer breathes — "Aprendamos a perdonar | y seremos perdonados" has a
 * 0.70 s gap, every other boundary < 0.07 s). Without usable word timing, or
 * when no boundary has a real pause, the most character-balanced cut wins.
 */
export function chooseSplitTokenIndex(text, words) {
  const tokens = tokenSpans(text);
  const n = tokens.length;
  if (n < 2) return null;
  const cands = [];
  for (let k = 1; k < n; k++) {
    const lenA = tokens[k - 1].ce - tokens[0].cs;
    const lenB = tokens[n - 1].ce - tokens[k].cs;
    cands.push({
      k,
      share: Math.min(lenA, lenB) / Math.max(1, lenA + lenB),
      imbalance: Math.abs(lenA - lenB),
      pause: null,
    });
  }
  const balanced = cands.filter((c) => c.share >= MIN_SIDE_SHARE);
  const pool = balanced.length ? balanced : cands;

  const bounds = wordBoundaries(tokens, words);
  if (bounds) {
    for (const c of pool) {
      const wi = bounds[c.k];
      if (wi <= 0 || wi >= words.length) continue;
      const prevEnd = lastWordEnd(words.slice(0, wi));
      const nextStart = firstWordStart(words.slice(wi));
      if (prevEnd != null && nextStart != null) c.pause = nextStart - prevEnd;
    }
    const paused = pool
      .filter((c) => c.pause != null && c.pause >= MIN_PAUSE_S)
      .sort((a, b) => b.pause - a.pause || a.imbalance - b.imbalance || a.k - b.k);
    if (paused.length) return paused[0].k;
  }
  return [...pool].sort((a, b) => a.imbalance - b.imbalance || a.k - b.k)[0].k;
}

/**
 * Plan a split of `seg` into two lines.
 *
 * Options (at most one): `charOffset` — the operator's caret (Enter key);
 * `splitTokenIndex` — cut before that whitespace token (reference auto-split).
 * With neither, the cut is `chooseSplitTokenIndex`.
 *
 * Returns `{ a, b, timing }` where `a`/`b` are `{ text, start, end, words }`
 * (`words` null when it can't be partitioned → caller must drop it) and
 * `timing` is "words" (inner boundary = real word times) or "char_ratio".
 * The outer bounds are always the line's own `start`/`end`, so splitting never
 * moves a lead-in or a held final the operator already set. Null when the cut
 * would leave an empty line.
 */
export function planSegmentSplit(seg, { charOffset = null, splitTokenIndex = null } = {}) {
  const text = typeof seg?.text === "string" ? seg.text : "";
  const tokens = tokenSpans(text);
  // Prefer clocks that provably belong to THIS text (own words, or the
  // provider's words of this window when the row lost them); otherwise the
  // historical positional mapping of whatever `words` the row carries.
  const words = lineWords(seg) || (Array.isArray(seg?.words) ? seg.words : null);
  const bounds = wordBoundaries(tokens, words);

  let cut = null;
  if (splitTokenIndex != null) {
    if (splitTokenIndex > 0 && splitTokenIndex < tokens.length) cut = tokens[splitTokenIndex].cs;
  } else if (charOffset != null) {
    const k = bounds ? tokenIndexAtCharOffset(tokens, charOffset, text.length) : -1;
    // Word-aligned line: snap to a word boundary. Otherwise (or if snapping
    // would empty a half) honor the exact caret, as the editor always did —
    // that is how an operator separates two glued words ("perdonary").
    cut = k > 0 && k < tokens.length
      ? tokens[k].cs
      : Math.max(0, Math.min(Number(charOffset) || 0, text.length));
  } else {
    const k = chooseSplitTokenIndex(text, words);
    if (k != null) cut = tokens[k].cs;
  }
  if (cut == null) return null;
  const textA = text.slice(0, cut).trim();
  const textB = text.slice(cut).trim();
  if (!textA || !textB) return null;

  // Partition `words` only when the cut falls between tokens, never inside one.
  let wordsA = null;
  let wordsB = null;
  if (bounds && !tokens.some((t) => t.cs < cut && cut < t.ce)) {
    const k = tokens.filter((t) => t.ce <= cut).length;
    const wi = bounds[k];
    if (wi > 0 && wi < words.length) {
      wordsA = words.slice(0, wi);
      wordsB = words.slice(wi);
    }
  }

  const segStart = Number(seg?.start) || 0;
  const segEnd = Math.max(segStart, Number(seg?.end) || 0);
  // Persistence requires at least 10ms per line. Do not manufacture time
  // outside the parent when it cannot accommodate two durable halves.
  const duration = segEnd - segStart;
  if (duration + 1e-9 < 0.02) return null;
  if (wordsA) {
    const aEnd = lastWordEnd(wordsA);
    const bStartRaw = firstWordStart(wordsB);
    if (
      aEnd != null && bStartRaw != null
      && aEnd > segStart && bStartRaw < segEnd
      && bStartRaw >= aEnd - OVERLAP_TOLERANCE_S
    ) {
      const bStart = bStartRaw > aEnd ? bStartRaw : aEnd + TOUCH_GAP_S;
      if (aEnd - segStart + 1e-9 >= 0.01 && segEnd - bStart + 1e-9 >= 0.01) {
        return {
          a: { text: textA, start: segStart, end: aEnd, words: wordsA },
          b: { text: textB, start: bStart, end: segEnd, words: wordsB },
          timing: "words",
        };
      }
    }
  }

  // Char ratio (long words take longer to sing). Words — when they could be
  // partitioned — still travel with their half: they are the per-word truth
  // even if this line's window was re-timed by hand since alignment.
  const ratio = textA.length / Math.max(1, textA.length + textB.length);
  const midTime = segStart + (segEnd - segStart) * ratio;
  const minHalf = Math.min(MIN_HALF_S, duration / 2);
  const gap = Math.min(FALLBACK_GAP_S, Math.max(0, duration - 2 * minHalf));
  const boundary = Math.max(segStart + minHalf + gap, Math.min(segEnd - minHalf, midTime));
  return {
    a: { text: textA, start: segStart, end: boundary - gap, words: wordsA },
    b: { text: textB, start: boundary, end: segEnd, words: wordsB },
    timing: "char_ratio",
  };
}

/** Reviewed cuts never label clocks from an earlier text as word timing.
 * Share this planner between the selected-line preview and the committed split.
 * Valid recognizer evidence can recover missing clocks; otherwise estimate.
 */
export function planReviewedSegmentSplit(segment, options = {}) {
  return planSegmentSplit({ ...segment, words: lineWords(segment) }, options);
}

// ─── Word clocks that survive split / merge ─────────────────────────────────
//
// Demos 28-oct-2026: 28 of 94 approved lines ended with `words: []`. Rows that
// had lost their `words` (or whose text had been touched) were split by
// character ratio and both halves were saved without clocks, although every
// row still carried the recognizer's clocks in `provider_evidence.words`.
// These helpers recover a 1:1 word array for the CURRENT text — the renderer
// contract: one word per visible token with the same letters
// (ass_render._word_timings, lib/karaokeTiming).

const RECOVERY_WINDOW_TOLERANCE_S = 0.3;

/** Renderer token key: NFC, lowercase, letters/digits only (accents kept). */
export function wordKey(value) {
  return String(value ?? "").normalize("NFC").toLowerCase().replace(/[^\p{L}\p{N}]/gu, "");
}

function usableClock(word) {
  if (!word || typeof word !== "object") return false;
  if (word.synthetic || word.estimated || word.stale) return false;
  const start = Number(word.start);
  const end = Number(word.end);
  return Number.isFinite(start) && Number.isFinite(end) && end >= start;
}

const wordText = (word) => word?.word ?? word?.text;

/** Leftmost (or, with `fromEnd`, rightmost) in-order embedding of keys in words. */
function embedding(keys, words, fromEnd = false) {
  const picks = new Array(keys.length);
  if (!fromEnd) {
    let j = 0;
    for (let k = 0; k < keys.length; k += 1) {
      while (j < words.length && wordKey(wordText(words[j])) !== keys[k]) j += 1;
      if (j >= words.length) return null;
      picks[k] = j;
      j += 1;
    }
  } else {
    let j = words.length - 1;
    for (let k = keys.length - 1; k >= 0; k -= 1) {
      while (j >= 0 && wordKey(wordText(words[j])) !== keys[k]) j -= 1;
      if (j < 0) return null;
      picks[k] = j;
      j -= 1;
    }
  }
  return picks;
}

/**
 * Words for every visible token of `text`, in order, or null. Each token must
 * match (renderer key) a word of `words`; words the text no longer has (the
 * reviewer deleted them or moved them to another line) are skipped. A token
 * the words never had (a corrected or inserted word) makes it null: a clock
 * is never invented. When skipping makes the occurrence ambiguous (a repeated
 * word could come from more than one place: "Te amo Te amo siempre" for
 * "Te amo siempre") it is null too, never the first match by default.
 */
export function tokenWords(text, words) {
  const tokens = String(text ?? "").split(/\s+/).filter(Boolean);
  if (!tokens.length || !Array.isArray(words) || !words.length) return null;
  const keys = tokens.map(wordKey);
  if (keys.some((key) => !key)) return null;
  const first = embedding(keys, words);
  if (!first) return null;
  const last = embedding(keys, words, true);
  if (!last || last.some((j, k) => j !== first[k])) return null;
  let lastStart = -Infinity;
  const out = [];
  for (const j of first) {
    if (!usableClock(words[j]) || Number(words[j].start) < lastStart) return null;
    lastStart = Number(words[j].start);
    out.push({ ...words[j] });
  }
  return out;
}

function insideWindow(seg, words) {
  const start = Number(seg.start);
  const end = Number(seg.end);
  if (!Array.isArray(words) || !Number.isFinite(start) || !Number.isFinite(end)) return null;
  return words.filter((w) => usableClock(w)
    && Number(w.start) >= start - RECOVERY_WINDOW_TOLERANCE_S
    && Number(w.end) <= end + RECOVERY_WINDOW_TOLERANCE_S);
}

/**
 * Best 1:1 word clocks for a line's current text, or null:
 * 1. its own `words` when they ARE the text, one per token (the renderer
 *    contract; kept even if the line window was re-timed by hand);
 * 2. otherwise its own words, then the recognizer's
 *    `provider_evidence.words`, restricted to the CURRENT line window, so a
 *    repeated word that was moved away to another line is never picked up
 *    again; an occurrence that is still ambiguous declines recovery.
 */
export function lineWords(seg) {
  if (!seg || typeof seg.text !== "string") return null;
  const tokens = seg.text.split(/\s+/).filter(Boolean);
  const own = Array.isArray(seg.words) ? seg.words : null;
  if (own && own.length === tokens.length && tokens.length
    && own.every((w, k) => wordKey(wordText(w)) === wordKey(tokens[k]))) {
    return tokenWords(seg.text, own);
  }
  return tokenWords(seg.text, insideWindow(seg, own))
    || tokenWords(seg.text, insideWindow(seg, seg.provider_evidence?.words));
}

/**
 * Join `a` with the following line `b`: text concatenated, outer bounds kept
 * (a.start .. the later end) and `words` = both arrays joined when BOTH lines
 * have clocks for their current text (recovered when needed). Otherwise no
 * words: never half a karaoke line with fabricated clocks.
 */
export function mergeLineRows(a, b) {
  const text = `${String(a?.text || "").trim()} ${String(b?.text || "").trim()}`.trim();
  const aw = lineWords(a) || (Array.isArray(a?.words) ? a.words : null);
  const bw = lineWords(b) || (Array.isArray(b?.words) ? b.words : null);
  const { words: _dropped, ...rest } = a || {};
  return {
    ...rest,
    text,
    start: a?.start,
    end: Math.max(Number(a?.end), Number(b?.end)),
    ...(aw && bw ? { words: [...aw, ...bw] } : {}),
  };
}

/**
 * Cut `seg` before each token index of `cuts` (0 < k < tokens). Same rules as
 * planSegmentSplit, left to right: each part keeps its slice of `words`, the
 * inner boundaries come from the word clocks and the outer bounds stay the
 * line's own. Returns [{ text, start, end, words }] or null.
 */
export function splitLineAtTokens(seg, cuts) {
  const tokens = tokenSpans(seg?.text);
  const sorted = [...new Set((cuts || []).map(Number))].sort((x, y) => x - y);
  if (!sorted.length || sorted.some((k) => !Number.isInteger(k) || k <= 0 || k >= tokens.length)) {
    return null;
  }
  const parts = [];
  let rest = { ...seg, words: lineWords(seg) || seg?.words };
  let consumed = 0;
  for (const cut of sorted) {
    const plan = planSegmentSplit(rest, { splitTokenIndex: cut - consumed });
    if (!plan) return null;
    parts.push(plan.a);
    consumed = cut;
    rest = { ...rest, text: plan.b.text, start: plan.b.start, end: plan.b.end, words: plan.b.words };
  }
  parts.push({ text: rest.text, start: rest.start, end: rest.end, words: rest.words || null });
  return parts;
}
