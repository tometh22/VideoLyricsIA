// Pure timing helpers for the lyrics editor's sync / shift operations.
//
// Why this exists: three places in LyricsEditor (single-line anchor, the
// cascade over trailing lines, and shiftAllSegments) moved a segment's start
// and then clamped only its END to the song `duration`:
//
//     let end = start + segDur;
//     if (end > duration) end = duration;   // start left untouched
//
// When a positive shift pushes `start` past `duration`, that leaves
// `start > end` — an inverted segment. In the cascade this happens to EVERY
// trailing line at once, so a single late anchor could invert 8 lines. The
// save-time sanitiser (sanitizeSegmentsForSave) then "fixes" the inversion by
// swapping, collapsing those lines to a zero-length blip pinned at the very
// end of the song — silently corrupting their timing. It surfaced as the
// "[autosave] sanitized out-of-contract segment(s)" Sentry signal.
//
// clampSegmentToDuration keeps the segment inside [0, duration] WITHOUT
// inverting: if the shift would run past the end, the segment is pinned flush
// against `duration` preserving its length, instead of zeroing out.

/**
 * Move `seg` so its start becomes `newStart` (seconds), keeping it inside
 * [0, duration] and guaranteeing start <= end. Duration is preserved (min
 * 0.5s). `duration` of 0/undefined means "no ceiling". Other fields (_id,
 * text, words, …) pass through untouched.
 */
export function clampSegmentToDuration(seg, newStart, duration) {
  const s = seg || {};
  const segDur = Math.max(0.5, Number(s.end) - Number(s.start) || 0.5);
  let start = Math.max(0, Number(newStart) || 0);
  let end = start + segDur;
  if (duration && end > duration) {
    end = duration;
    // Pin flush to the song end, preserving length — never leave start > end.
    start = Math.max(0, end - segDur);
  }
  return { ...s, start, end };
}
