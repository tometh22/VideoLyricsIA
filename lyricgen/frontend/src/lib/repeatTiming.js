// Text equality identifies candidates, never acoustic equivalence. Every copy
// requires an explicit selection and is bound to the source/target snapshots.
import { updateSegmentTiming } from "./segmentWords";
const textKey = value => String(value || "").trim().replace(/\s+/g, " ");
export const repeatTimingSnapshot = row => JSON.stringify([row?._id, row?.segment_id, row?.text, row?.start, row?.end, row?.locked, row?.words]);
export function proposeRepeatTiming(rows, id, start, end, duration, gap = 0.05) {
  const source = rows.find(row => row._id === id);
  const length = end - start;
  if (!source || !textKey(source.text) || !Number.isFinite(length) || length <= 0
    || Math.abs(length - (source.end - source.start)) < 0.001) return null;
  const candidates = rows.filter(row => row._id !== id && row.start > source.start && textKey(row.text) === textKey(source.text))
    .filter(row => {
      const proposedEnd = row.start + length;
      return Math.abs(proposedEnd - row.end) >= 0.001
        && (!duration || proposedEnd <= duration)
        && !rows.some(other => other._id !== row._id && other.start < proposedEnd + gap && other.end > row.start);
    }).map(row => ({ id: row._id, start: row.start, end: row.start + length, snapshot: repeatTimingSnapshot(row) }));
  return candidates.length ? { sourceId: id,
    sourceSnapshot: repeatTimingSnapshot({ ...updateSegmentTiming(source, start, end), locked: true }),
    length, candidates } : null;
}
export function repeatTimingChanges(rows, proposal, ids, duration, gap = 0.05) {
  if (!proposal || repeatTimingSnapshot(rows.find(row => row._id === proposal.sourceId)) !== proposal.sourceSnapshot) return [];
  const selected = new Set(ids);
  const changes = proposal.candidates.filter(item => selected.has(item.id) && repeatTimingSnapshot(rows.find(row => row._id === item.id)) === item.snapshot);
  // Revalidate against the entire current document, including intervening edits.
  return changes.filter(item => (!duration || item.end <= duration) && !rows.some(other => {
    if (other._id === item.id) return false;
    const replacement = changes.find(change => change.id === other._id);
    return (replacement?.start ?? other.start) < item.end + gap && (replacement?.end ?? other.end) > item.start;
  }));
}
