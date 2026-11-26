// Context at the gesture, not reconstructed from the final document. A selected
// line is not necessarily the destination of a free seek; keep that distinction.
export function editorSeekProperties({ from, to, segment, revision, targeted = false,
  qualityMarked = false, unsavedChanges = false, lineIndex = -1 }) {
  const properties = {
    source: "editor",
    from_position_ms: Math.round(Math.max(0, from) * 1000),
    position_ms: Math.round(Math.max(0, to) * 1000),
    line_context: segment ? (targeted ? "target" : "selected") : "none",
    review_marker: !segment ? "unknown"
      : segment.review ? (qualityMarked ? "both" : "review")
        : qualityMarked ? "quality_window" : "none",
    unsaved_changes: unsavedChanges,
  };
  if (Number.isInteger(revision) && revision >= 0) properties.revision = revision;
  // Keep local/audit identity AND the stable merge identity: local _id can
  // be reassigned when a document is hydrated. Never join sessions on it alone.
  if (segment?._id != null) properties.line_id = String(segment._id);
  if (segment?.segment_id != null) properties.segment_id = String(segment.segment_id);
  if (lineIndex >= 0) properties.line_index = lineIndex;
  if (Number.isFinite(segment?.start)) properties.line_start_ms = Math.round(segment.start * 1000);
  return properties;
}
