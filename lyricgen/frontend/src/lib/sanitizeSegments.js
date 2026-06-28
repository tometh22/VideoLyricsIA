// Sanitises a segments array to the backend's /save-segments contract before
// it's POSTed. Mirror of the validation in main.py::save_segments:
//   - start/end must be finite numbers, 0 ≤ start ≤ end ≤ MAX_END_S
//   - text must be a string, ≤ MAX_TEXT_CHARS
//
// Why this exists (incident 2026-06-26, Universal AR operator): the editor can
// produce an out-of-contract segment — a manual timestamp edit typed as NaN, a
// drag/anchor that inverts start/end, etc. A SINGLE invalid segment makes
// /save-segments return 400, and that doesn't just drop one edit: it pins
// saveStatus to "error" and blocks every subsequent autosave AND the "Aprobar"
// button for the whole job until the bad segment is found and fixed by hand.
//
// Sanitising at the POST boundary guarantees we never ship an invalid segment,
// so a stray bad value degrades to "saved, clamped" (visible + fixable) instead
// of "everything is stuck". Kept dependency-free and pure for unit testing.

const MAX_END_S = 24 * 3600; // 86400 — matches main.py _MAX_END_S
const MAX_TEXT_CHARS = 2000; // matches main.py _MAX_TEXT_CHARS

/**
 * Return a new array where every segment satisfies the backend contract.
 * Inverted start/end are swapped (the operator clearly meant both times);
 * non-finite numbers fall back to a safe value; text is coerced to a capped
 * string. Extra fields (_id, locked, …) are preserved.
 */
export function sanitizeSegmentsForSave(segments) {
  if (!Array.isArray(segments)) return segments;
  return segments.map((s) => {
    const seg = s || {};
    let start = Number(seg.start);
    let end = Number(seg.end);
    if (!Number.isFinite(start)) start = 0;
    if (!Number.isFinite(end)) end = start;
    if (start < 0) start = 0;
    if (end < 0) end = 0;
    if (start > end) {
      // Inverted edge (dragged ENTRA past SALE) — un-invert rather than drop.
      const t = start;
      start = end;
      end = t;
    }
    if (start > MAX_END_S) start = MAX_END_S;
    if (end > MAX_END_S) end = MAX_END_S;
    let text = typeof seg.text === "string" ? seg.text : String(seg.text ?? "");
    if (text.length > MAX_TEXT_CHARS) text = text.slice(0, MAX_TEXT_CHARS);
    return { ...seg, start, end, text };
  });
}

/**
 * List the segments whose start/end/text the sanitiser had to change, for
 * diagnostics (so we still learn WHAT was out of contract even though we no
 * longer 400 on it). Returns `[]` when the input was already clean.
 */
export function findSanitizedDiffs(original, sanitized) {
  if (!Array.isArray(original) || !Array.isArray(sanitized)) return [];
  const out = [];
  for (let i = 0; i < sanitized.length; i++) {
    const o = original[i] || {};
    const s = sanitized[i] || {};
    if (Number(o.start) !== s.start || Number(o.end) !== s.end || o.text !== s.text) {
      out.push({ index: i, from: { start: o.start, end: o.end }, to: { start: s.start, end: s.end } });
    }
  }
  return out;
}
