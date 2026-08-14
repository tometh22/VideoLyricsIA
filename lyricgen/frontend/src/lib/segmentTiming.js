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

/**
 * Move one lyric line while rippling only the packed neighbours in the
 * movement direction. This keeps every duration intact and avoids the
 * "frozen" drag produced by clamping a line between two 50ms gaps.
 */
export function shiftTimingWithAdjacent(
  allSegments, id, requestedDelta, duration, gap = 0.05,
) {
  const clean = (value) => Math.round(value * 1e9) / 1e9;
  const index = allSegments.findIndex((segment) => segment?._id === id);
  const requested = Number(requestedDelta);
  const safeGap = Math.max(0, Number(gap) || 0);
  if (index < 0 || !Number.isFinite(requested) || Math.abs(requested) < 1e-9) {
    return { changes: [], delta: 0, coupled: false, blocked: false };
  }

  const snapshots = allSegments.map((segment) => ({
    id: segment?._id,
    start: Number(segment?.start),
    end: Number(segment?.end),
  }));
  if (snapshots.some((segment) => !Number.isFinite(segment.start) || !Number.isFinite(segment.end))) {
    return { changes: [], delta: 0, coupled: false, blocked: true };
  }

  const ripple = (delta) => {
    const shifted = snapshots.map((segment) => ({ ...segment }));
    shifted[index].start += delta;
    shifted[index].end += delta;

    if (delta > 0) {
      for (let next = index + 1; next < shifted.length; next += 1) {
        const minStart = shifted[next - 1].end + safeGap;
        if (shifted[next].start >= minStart - 1e-9) break;
        const neighbourDelta = minStart - shifted[next].start;
        shifted[next].start += neighbourDelta;
        shifted[next].end += neighbourDelta;
      }
    } else {
      for (let previous = index - 1; previous >= 0; previous -= 1) {
        const maxEnd = shifted[previous + 1].start - safeGap;
        if (shifted[previous].end <= maxEnd + 1e-9) break;
        const neighbourDelta = maxEnd - shifted[previous].end;
        shifted[previous].start += neighbourDelta;
        shifted[previous].end += neighbourDelta;
      }
    }
    return shifted;
  };

  let safeDelta = requested;
  let shifted = ripple(safeDelta);
  if (safeDelta > 0 && Number(duration) > 0) {
    const overflow = Math.max(0, ...shifted.map((segment) => segment.end - Number(duration)));
    if (overflow > 0) {
      safeDelta = Math.max(0, safeDelta - overflow);
      shifted = ripple(safeDelta);
    }
  } else if (safeDelta < 0) {
    const underflow = Math.max(0, ...shifted.map((segment) => -segment.start));
    if (underflow > 0) {
      safeDelta = Math.min(0, safeDelta + underflow);
      shifted = ripple(safeDelta);
    }
  }

  const changes = shifted
    .filter((segment, segmentIndex) => (
      Math.abs(segment.start - snapshots[segmentIndex].start) > 1e-6
      || Math.abs(segment.end - snapshots[segmentIndex].end) > 1e-6
    ))
    .map((segment) => ({
      ...segment,
      start: clean(segment.start),
      end: clean(segment.end),
    }));
  return {
    changes,
    delta: clean(safeDelta),
    coupled: changes.length > 1,
    blocked: Math.abs(requested) > 1e-6 && Math.abs(safeDelta) < 1e-6,
  };
}

export function clampResizeTiming(
  allSegments, id, requestedStart, requestedEnd, duration, gap = 0.05, minDuration = 0.3,
  edge = null,
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

  const originalStart = Number(segment.start);
  const originalEnd = Number(segment.end);
  const blocked = () => ({ start: originalStart, end: originalEnd, blocked: true });

  // A resize handle owns exactly one boundary. Keeping the opposite boundary
  // anchored prevents the block from jumping when the operator crosses the
  // minimum-duration limit.
  if (edge === "start") {
    const maxStart = originalEnd - minDuration;
    if (!Number.isFinite(originalEnd) || !Number.isFinite(minStart) || maxStart < minStart) return blocked();
    const start = Math.max(minStart, Math.min(Number(requestedStart), maxStart));
    if (!Number.isFinite(start)) return blocked();
    return { start, end: originalEnd, blocked: false };
  }
  if (edge === "end") {
    const minEnd = originalStart + minDuration;
    if (!Number.isFinite(originalStart) || Number.isNaN(maxEnd) || maxEnd < minEnd) return blocked();
    const end = Math.min(maxEnd, Math.max(Number(requestedEnd), minEnd));
    if (!Number.isFinite(end)) return blocked();
    return { start: originalStart, end, blocked: false };
  }

  // Backward-compatible two-sided clamp for non-interactive callers.
  const start = Math.max(minStart, Math.min(Number(requestedStart), Number(requestedEnd) - minDuration));
  const end = Math.min(maxEnd, Math.max(Number(requestedEnd), start + minDuration));
  if (!Number.isFinite(start) || !Number.isFinite(end) || end - start < minDuration - 1e-6) {
    return blocked();
  }
  return { start, end, blocked: false };
}

/**
 * Resize one edge and, when two lyric lines are packed together, move the
 * shared boundary instead of making the dragged handle look frozen.
 *
 * The adjacent line keeps its opposite edge, so this is a local two-line
 * edit rather than a cascade through the rest of the song. Both lines retain
 * the configured minimum duration and gap.
 */
export function clampResizeTimingWithAdjacent(
  allSegments, id, requestedStart, requestedEnd, duration, gap = 0.05, minDuration = 0.3,
  edge = null,
) {
  const bounded = clampResizeTiming(
    allSegments, id, requestedStart, requestedEnd, duration, gap, minDuration, edge,
  );
  if (!bounded) return null;

  const index = allSegments.findIndex((segment) => segment?._id === id);
  const segment = allSegments[index];
  const safeGap = Math.max(0, Number(gap) || 0);
  const safeMinDuration = Math.max(0, Number(minDuration) || 0);
  const single = (result = bounded) => ({
    changes: [{ id, start: result.start, end: result.end }],
    blocked: result.blocked,
    coupled: false,
  });

  if (edge === "end") {
    const next = index + 1 < allSegments.length ? allSegments[index + 1] : null;
    const requested = Number(requestedEnd);
    if (!next || !Number.isFinite(requested) || requested <= bounded.end + 1e-6) return single();

    const songCeiling = Number(duration) > 0 ? Number(duration) : Infinity;
    const maxEnd = Math.min(Number(next.end) - safeMinDuration - safeGap, songCeiling);
    const end = Math.min(requested, maxEnd);
    if (!Number.isFinite(end) || end <= bounded.end + 1e-6) {
      return { ...single(), blocked: true };
    }
    return {
      changes: [
        { id, start: Number(segment.start), end },
        { id: next._id, start: end + safeGap, end: Number(next.end) },
      ],
      blocked: false,
      coupled: true,
    };
  }

  if (edge === "start") {
    const previous = index > 0 ? allSegments[index - 1] : null;
    const requested = Number(requestedStart);
    if (!previous || !Number.isFinite(requested) || requested >= bounded.start - 1e-6) return single();

    const minStart = Number(previous.start) + safeMinDuration + safeGap;
    const start = Math.max(requested, minStart, 0);
    if (!Number.isFinite(start) || start >= bounded.start - 1e-6) {
      return { ...single(), blocked: true };
    }
    return {
      changes: [
        { id: previous._id, start: Number(previous.start), end: start - safeGap },
        { id, start, end: Number(segment.end) },
      ],
      blocked: false,
      coupled: true,
    };
  }

  return single();
}

/**
 * Canonicalize an editor payload by timestamp without changing timings.
 * Browser row order is transient: inserting/rebasing a lyric can append it
 * temporarily, while playback and persistence require timeline order.
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

function finiteStart(segment) {
  const value = Number(segment?.start);
  return Number.isFinite(value) ? value : null;
}

/**
 * Choose the active row from timestamps, never from the incoming array
 * position. The later start wins inside an overlap; equal timestamps keep
 * the earliest stable row so duplicate legacy lines do not flicker.
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
    if (time < end && (!containing || start > containing.start
      || (start === containing.start && index < containing.index))) {
      containing = candidate;
    }
    if (!latestStarted || start > latestStarted.start
      || (start === latestStarted.start && index < latestStarted.index)) {
      latestStarted = candidate;
    }
  });

  const selected = containing || latestStarted;
  return selected?.segment?._id ?? selected?.index ?? null;
}
