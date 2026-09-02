// The API has emitted two equivalent revision-conflict shapes during the
// editor migration. Keep the wire-format compatibility in one place so the
// wizard, post-render editor, and generation retry path cannot disagree about
// whether a 409 is safe to rebase.

export function isEditorRevisionConflict(response, payload) {
  if (response?.status !== 409 || !payload || typeof payload !== "object") {
    return false;
  }

  const detail = payload.detail;
  return detail === "editor_revision_conflict"
    || detail?.detail === "editor_revision_conflict"
    || detail?.code === "editor_revision_conflict"
    || payload.code === "editor_revision_conflict";
}

export function editorRevisionConflictDetail(payload) {
  if (!payload || typeof payload !== "object") return null;
  const detail = payload.detail;
  if (
    detail && typeof detail === "object"
    && (detail.detail === "editor_revision_conflict"
      || detail.code === "editor_revision_conflict")
  ) return detail;
  if (detail === "editor_revision_conflict") return payload;
  if (payload.code === "editor_revision_conflict") return payload;
  return null;
}
