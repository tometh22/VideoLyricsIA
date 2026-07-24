// Pure timing helpers for the lyrics editor's sync / shift operations.
//
// A positive cascade used to clamp each segment independently. Besides
// allowing start > end, that could stack every trailing line at the same
// timestamp. Block shifts therefore use one shared bounded delta so ordering,
// lengths, and gaps remain unchanged.

/**
 * Bound a shared timeline delta so every segment stays inside [0, duration].
 */
export function clampBlockShiftDelta(segments, requestedDelta, duration) {
  if (!Array.isArray(segments) || segments.length === 0) return 0;
  const delta = Number(requestedDelta);
  if (!Number.isFinite(delta)) return 0;

  const starts = segments.map((segment) => Number(segment?.start));
  const ends = segments.map((segment) => Number(segment?.end));
  if ([...starts, ...ends].some((value) => !Number.isFinite(value))) return 0;

  const minStart = Math.min(...starts);
  const maxEnd = Math.max(...ends);
  const lowerBound = -minStart;
  const upperBound = duration && Number.isFinite(Number(duration))
    ? Math.max(lowerBound, Number(duration) - maxEnd)
    : Infinity;
  return Math.max(lowerBound, Math.min(delta, upperBound));
}

/**
 * Shift a whole block using one bounded delta. Relative timing is invariant.
 */
export function shiftBlockWithinDuration(segments, requestedDelta, duration) {
  const delta = clampBlockShiftDelta(segments, requestedDelta, duration);
  return segments.map((segment) => ({
    ...segment,
    start: Number(segment.start) + delta,
    end: Number(segment.end) + delta,
  }));
}
