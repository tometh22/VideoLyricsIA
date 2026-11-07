import { mergeThreeWay, segmentsEquivalent } from "../editorMerge";
import { reseedPreservingIds } from "./segmentIds";

const identity = row => String(row.segment_id ?? row.id ?? row._id);
function occurrenceKeys(rows) {
  const seen = new Map();
  return rows.map(row => {
    const id = identity(row);
    const count = seen.get(id) || 0;
    seen.set(id, count + 1);
    return JSON.stringify([id, count]);
  });
}

export function rebaseFocusedSegmentId(before, after, focusedId) {
  const focusedIndex = before.findIndex(row => row._id === focusedId);
  const focused = before[focusedIndex];
  if (!focused) return null;
  const occurrence = before.slice(0, focusedIndex)
    .filter(row => identity(row) === identity(focused)).length;
  return after.filter(row => identity(row) === identity(focused))[occurrence]?._id ?? null;
}

/**
 * Undo snapshots predate a durable merge. Carry remote-only changes into each
 * snapshot before it can be restored. A same-line collision is ambiguous:
 * discard that snapshot rather than treating Undo as permission to overwrite
 * another editor. This deliberately does not change normal save merge policy.
 */
export function rebaseEditorHistory(history, before, merged) {
  const remoteKeys = occurrenceKeys(merged);
  const remoteByKey = new Map(merged.map((row, index) => [remoteKeys[index], row]));
  return history.flatMap(snapshot => {
    const result = mergeThreeWay(before, snapshot.segments, merged);
    if (result.conflicts.length || segmentsEquivalent(result.merged, merged)) return [];
    // The save merge intentionally ignores renderer words/review metadata.
    // On a row this undo does not change, retain the current full row rather
    // than reviving stale evidence from an older local history entry.
    const keys = occurrenceKeys(result.merged);
    const withCurrentMetadata = result.merged.map((row, index) => {
      const current = remoteByKey.get(keys[index]);
      return current && segmentsEquivalent([row], [current]) ? current : row;
    });
    const segments = reseedPreservingIds(snapshot.segments,
      withCurrentMetadata.sort((a, b) => a.start - b.start));
    const focusedId = rebaseFocusedSegmentId(snapshot.segments, segments, snapshot.focusedId);
    return [{ ...snapshot, segments, focusedId }];
  });
}
