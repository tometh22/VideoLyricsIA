const EPSILON = 0.0001;

const validLine = (line) => Number.isFinite(line?.start) && Number.isFinite(line?.end)
  && line.start >= 0 && line.end > line.start && typeof line.text === "string";
const sameLine = (a, b) => a.start === b.start && a.end === b.end && a.text === b.text;

export function buildQualityComparison(segments, window, duration) {
  const { start, end, current_segments: before, proposed_segments: after } = window;
  if (![start, end, duration].every(Number.isFinite) || end <= start || duration <= 0
    || !Array.isArray(segments) || !Array.isArray(before) || !Array.isArray(after)
    || !after.length || ![...before, ...after].every(line => validLine(line)
      && line.start >= start - EPSILON && line.end <= end + EPSILON)) return null;

  // Match the same interval used by the proposal apply endpoint, including empty gaps.
  const overlaps = line => line.start < end - EPSILON && line.end > start + EPSILON;
  const snapshot = segments.filter(overlaps);
  if (snapshot.length !== before.length || !snapshot.every((line, i) => sameLine(line, before[i]))) return null;

  let first = 0;
  let left = before.length;
  let right = after.length;
  while (first < left && first < right && sameLine(before[first], after[first])) first++;
  while (left > first && right > first && sameLine(before[left - 1], after[right - 1])) {
    left--;
    right--;
  }
  const oldLines = before.slice(first, left);
  const newLines = after.slice(first, right);
  const changedTimes = oldLines.length === newLines.length && oldLines.every((line, i) => line.text === newLines[i].text)
    ? oldLines.flatMap((line, i) => ["start", "end"].flatMap(key =>
      line[key] === newLines[i][key] ? [] : [line[key], newLines[i][key]]))
    : [...oldLines, ...newLines].flatMap(line => [line.start, line.end]);
  if (!changedTimes.length) return null;

  const firstChange = Math.min(...changedTimes);
  const lastChange = Math.max(...changedTimes);
  if (lastChange > duration || lastChange - firstChange > 8) return null;
  const length = Math.max(6, lastChange - firstChange + 2);
  const clipStart = Math.max(0, Math.min((firstChange + lastChange - length) / 2, duration - length));
  const clipEnd = Math.min(duration, clipStart + length);
  const proposed = [...segments.filter(line => !overlaps(line)), ...after]
    .sort((a, b) => a.start - b.start || a.end - b.end);
  return { start: clipStart, end: clipEnd, current: segments, proposed };
}
