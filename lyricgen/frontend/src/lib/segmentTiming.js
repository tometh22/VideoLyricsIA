// Timing helpers shared by the lyrics review UI.

export const MIN_SEGMENT_GAP = 0.05;
export const MIN_SEGMENT_DURATION = 0.3;

const roundTiming = (value) => Math.round(value * 10000) / 10000;

/**
 * Keep the editor list in timeline order without changing timestamps.
 * Sorting is stable, so simultaneous lines retain their incoming order.
 */
export function sortSegmentsChronologically(segments) {
  if (!Array.isArray(segments)) return [];
  return segments
    .map((segment, index) => ({ segment, index }))
    .filter(({ segment }) => segment && typeof segment === "object")
    .sort((left, right) => {
      const a = Number(left.segment.start);
      const b = Number(right.segment.start);
      const safeA = Number.isFinite(a) ? Math.max(0, a) : 0;
      const safeB = Number.isFinite(b) ? Math.max(0, b) : 0;
      return safeA - safeB || left.index - right.index;
    })
    .map(({ segment }) => segment);
}

export function normalizeSegmentsTiming(
  segments,
  { minGap = MIN_SEGMENT_GAP, minDuration = MIN_SEGMENT_DURATION } = {},
) {
  if (!Array.isArray(segments)) return [];
  const gap = Math.max(0, Number(minGap) || 0);
  const durationFloor = Math.max(0.001, Number(minDuration) || 0.001);
  let previousStart = null;

  return segments
    .filter((segment) => segment && typeof segment === "object")
    .map((segment) => {
      const rawStart = Number(segment.start);
      const rawEnd = Number(segment.end);
      let start = Number.isFinite(rawStart) ? Math.max(0, rawStart) : 0;
      const originalDuration = Number.isFinite(rawEnd)
        ? Math.max(durationFloor, rawEnd - start)
        : durationFloor;

      if (previousStart != null) start = Math.max(start, previousStart + gap);
      const end = Math.max(start + durationFloor, start + originalDuration);
      previousStart = start;
      return { ...segment, start: roundTiming(start), end: roundTiming(end) };
    });
}

export function timingAnomalies(segments) {
  if (!Array.isArray(segments)) {
    return { regressions: 0, overlaps: 0, duplicateStarts: 0 };
  }
  const valid = segments
    .filter((segment) => segment && typeof segment === "object")
    .map((segment) => ({
      start: Number(segment.start),
      end: Number(segment.end),
    }))
    .filter(({ start, end }) => Number.isFinite(start) && Number.isFinite(end));
  const regressions = valid.slice(1).reduce((count, current, index) => (
    count + (current.start < valid[index].start ? 1 : 0)
  ), 0);
  const ordered = [...valid].sort((left, right) => left.start - right.start || left.end - right.end);
  let overlaps = 0;
  let furthestEnd = null;
  ordered.forEach(({ start, end }) => {
    if (furthestEnd != null && furthestEnd > start) overlaps += 1;
    furthestEnd = Math.max(furthestEnd ?? end, end);
  });
  const roundedStarts = valid.map(({ start }) => Math.round(start * 1000) / 1000);
  const duplicateStarts = roundedStarts.length - new Set(roundedStarts).size;
  return { regressions, overlaps, duplicateStarts };
}

function finiteStart(segment) {
  const value = Number(segment?.start);
  return Number.isFinite(value) ? value : null;
}

/**
 * Select by chronological timestamp, independently of payload row order.
 * This keeps overlapping or legacy non-monotonic payloads from making the
 * active-row highlight move backwards while the audio clock moves forwards.
 */
export function selectActiveSegmentId(segments, currentTime) {
  if (!Array.isArray(segments) || !segments.length) return null;
  const time = Number(currentTime);
  if (!Number.isFinite(time)) return null;

  let containing = null;
  let latestStarted = null;
  segments.forEach((segment, index) => {
    const start = finiteStart(segment);
    if (start == null || start > time) return;
    const rawEnd = Number(segment.end);
    const end = Number.isFinite(rawEnd) ? rawEnd : start;
    const candidate = { segment, index, start };
    if (time < end && (!containing || start > containing.start || (start === containing.start && index < containing.index))) {
      containing = candidate;
    }
    if (!latestStarted || start > latestStarted.start || (start === latestStarted.start && index < latestStarted.index)) {
      latestStarted = candidate;
    }
  });

  const selected = containing || latestStarted;
  return selected?.segment?._id ?? selected?.index ?? null;
}
