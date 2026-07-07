// Value-equality check for two segments arrays. Used by LyricsEditor's
// prop-sync useEffect to decide whether an incoming `segments` prop is
// genuinely new content (operator just opened a different song) or just
// the autosave roundtrip's `setCurrentReview` echoing back the values
// the operator already has in `edited`.
//
// Without this helper the prop-sync useEffect re-seeds `edited` on every
// new prop REFERENCE, which clobbers in-flight local edits — including
// the drag-resize of a segment edge. The operator drags, the visual
// previews the new position, the autosave POSTs, the parent's
// setCurrentReview sends back a NEW segments array reference with the
// SAME values the operator just dragged — and the reseed nukes
// `locked`/`pos`/`scale`/`rot` plus reassigns _ids, which under some
// race conditions also stomps a still-pending second drag.
//
// Comparing by value (start/end/text per segment, epsilon-tolerant on
// floats) preserves the operator's intent. If the parent really sends
// genuinely-different segments (load a new song, undo from history), at
// least one value differs and the reseed fires as before.

const EPSILON_S = 1e-3;

// Sort by start time before comparing. The backend sorts segments by `start`
// on every /save-segments write (#184), so after a split or overlap edit the
// local array can be in a different order than the server response. A purely
// positional comparison saw them as "different content" and reseeded on every
// autosave cycle → [reseed-storm] / rows remounting in a loop (Sentry #7560982997).
const _byStart = (x, y) => (Number(x.start) || 0) - (Number(y.start) || 0);

export function segmentsValuesEqual(a, b) {
  if (a === b) return true;
  if (!Array.isArray(a) || !Array.isArray(b)) return false;
  if (a.length !== b.length) return false;
  const sa = [...a].sort(_byStart);
  const sb = [...b].sort(_byStart);
  for (let i = 0; i < sa.length; i++) {
    const segA = sa[i] || {};
    const segB = sb[i] || {};
    if (Math.abs((Number(segA.start) || 0) - (Number(segB.start) || 0)) > EPSILON_S) return false;
    if (Math.abs((Number(segA.end) || 0) - (Number(segB.end) || 0)) > EPSILON_S) return false;
    if (String(segA.text || "") !== String(segB.text || "")) return false;
  }
  return true;
}
