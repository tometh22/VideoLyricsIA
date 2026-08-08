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

/**
 * Clamp one shared delta against song bounds and every unselected neighbour.
 * This works for contiguous and non-contiguous selections: every selected
 * line contributes its own lower/upper bound and the intersection wins.
 */
export function clampSelectionShiftDelta(
  allSegments, selectedIds, requestedDelta, duration, gap = 0.05,
) {
  if (!Array.isArray(allSegments) || !allSegments.length) return 0;
  const selected = selectedIds instanceof Set ? selectedIds : new Set(selectedIds || []);
  const delta = Number(requestedDelta);
  if (!selected.size || !Number.isFinite(delta)) return 0;
  let lower = -Infinity;
  let upper = Infinity;
  const safeGap = Math.max(0, Number(gap) || 0);
  const safeDuration = Number(duration);

  allSegments.forEach((segment, index) => {
    if (!selected.has(segment?._id)) return;
    const start = Number(segment.start);
    const end = Number(segment.end);
    if (!Number.isFinite(start) || !Number.isFinite(end)) return;
    lower = Math.max(lower, -start);
    if (Number.isFinite(safeDuration) && safeDuration > 0) {
      upper = Math.min(upper, safeDuration - end);
    }
    for (let previous = index - 1; previous >= 0; previous -= 1) {
      const neighbour = allSegments[previous];
      if (selected.has(neighbour?._id)) continue;
      lower = Math.max(lower, Number(neighbour.end) + safeGap - start);
      break;
    }
    for (let next = index + 1; next < allSegments.length; next += 1) {
      const neighbour = allSegments[next];
      if (selected.has(neighbour?._id)) continue;
      upper = Math.min(upper, Number(neighbour.start) - safeGap - end);
      break;
    }
  });
  if (!Number.isFinite(lower)) lower = 0;
  if (lower > upper) return 0;
  return Math.max(lower, Math.min(delta, upper));
}

export function clampResizeTiming(
  allSegments, id, requestedStart, requestedEnd, duration, gap = 0.05, minDuration = 0.3,
) {
  const index = allSegments.findIndex((segment) => segment?._id === id);
  if (index < 0) return null;
  const segment = allSegments[index];
  const safeGap = Math.max(0, Number(gap) || 0);
  const previous = index > 0 ? allSegments[index - 1] : null;
  const next = index + 1 < allSegments.length ? allSegments[index + 1] : null;
  const minStart = previous ? Number(previous.end) + safeGap : 0;
  const maxEnd = next
    ? Number(next.start) - safeGap
    : (Number(duration) > 0 ? Number(duration) : Infinity);
  const start = Math.max(minStart, Math.min(Number(requestedStart), Number(requestedEnd) - minDuration));
  const end = Math.min(maxEnd, Math.max(Number(requestedEnd), start + minDuration));
  if (!Number.isFinite(start) || !Number.isFinite(end) || end - start < minDuration - 1e-6) {
    return { start: Number(segment.start), end: Number(segment.end), blocked: true };
  }
  return { start, end, blocked: false };
}
