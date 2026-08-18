import { segmentsEquivalent } from "../editorMerge";

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

// Legacy local drafts do not have the durable editor's three-way merge and
// version history guarantees. Restore only against their exact base. A stale
// but already-saved draft is discarded; a genuinely divergent stale draft is
// kept in storage and surfaced as a conflict, never posted automatically.
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
  return { action: "conflict", segments: null };
}
