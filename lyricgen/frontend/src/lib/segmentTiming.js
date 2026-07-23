// Pure timing helpers for the lyrics editor's sync / shift operations.
//
// A positive cascade used to clamp only the segment end to the song
// duration. When the shifted start passed the duration that left start > end,
// and save-time sanitization collapsed the affected lines at the song end.

/**
 * Move `seg` to `newStart`, preserving its duration while keeping it inside
 * [0, duration]. A zero/undefined duration means there is no upper bound.
 */
export function clampSegmentToDuration(seg, newStart, duration) {
  const source = seg || {};
  const segmentDuration = Math.max(
    0.5,
    Number(source.end) - Number(source.start) || 0.5,
  );
  let start = Math.max(0, Number(newStart) || 0);
  let end = start + segmentDuration;
  if (duration && end > duration) {
    end = duration;
    start = Math.max(0, end - segmentDuration);
  }
  return { ...source, start, end };
}
