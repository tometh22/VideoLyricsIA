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

export function segmentsValuesEqual(a, b) {
  if (a === b) return true;
  if (!Array.isArray(a) || !Array.isArray(b)) return false;
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) {
    const sa = a[i] || {};
    const sb = b[i] || {};
    const startA = Number(sa.start) || 0;
    const startB = Number(sb.start) || 0;
    if (Math.abs(startA - startB) > EPSILON_S) return false;
    const endA = Number(sa.end) || 0;
    const endB = Number(sb.end) || 0;
    if (Math.abs(endA - endB) > EPSILON_S) return false;
    const textA = String(sa.text || "");
    const textB = String(sb.text || "");
    if (textA !== textB) return false;
  }
  return true;
}
