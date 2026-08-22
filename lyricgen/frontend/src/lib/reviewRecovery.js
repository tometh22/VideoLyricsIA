import { mergeThreeWay, segmentsEquivalent } from "../editorMerge";

// A wizard snapshot is an optimistic-concurrency snapshot, not an alternate
// source of truth. Reuse it only while it is based on the exact server
// revision currently being edited; otherwise a full-array autosave could
// relabel stale rows with a newer revision and overwrite another tab.
export function isReusableEditSnapshot({ snapshot, jobId, serverRevision }) {
  const review = snapshot?.currentReview;
  return Boolean(
    review?.editingJobId === jobId
      && Array.isArray(review.segments)
      && review.segments.length > 0
      && Number.isInteger(review.segmentsRevision)
      && review.segmentsRevision === serverRevision,
  );
}

// Recover a local draft WITHOUT ever asking the operator to arbitrate.
//
// There is no real multi-user editing in Genly — the same person keeps several
// tabs and windows open on the same song. A draft based on an older revision
// is therefore their own work, not somebody else's, and the old behaviour
// (surface it as a "conflict" and drop it on the floor) cost them typing for a
// race they cannot even perceive. Every outcome here is silent:
//
//   - the draft is already saved  → discard it,
//   - it is based on the current revision  → restore it as-is,
//   - it is stale but we know its base  → three-way merge, so lines the draft
//     never touched keep whatever the other tab wrote,
//   - it is stale with no base  → restore it: nothing else on screen
//     represents what the operator typed, and the save path still writes
//     through the backend revision check, so this can never be a raw
//     overwrite.
export function resolveLegacyDraft({ draft, currentSegments, currentRevision }) {
  if (!Array.isArray(draft?.segments) || draft.segments.length === 0) {
    return { action: "none", segments: null };
  }
  if (segmentsEquivalent(draft.segments, currentSegments)) {
    return { action: "discard", segments: null };
  }
  if (Number.isInteger(draft.base_revision)
    && draft.base_revision === currentRevision) {
    return { action: "restore", segments: draft.segments };
  }
  if (Array.isArray(draft.base_segments) && draft.base_segments.length > 0) {
    const merged = mergeThreeWay(
      draft.base_segments, draft.segments, currentSegments || [],
    );
    return { action: "restore", segments: merged.merged, rebased: true };
  }
  return { action: "restore", segments: draft.segments, rebased: true };
}
