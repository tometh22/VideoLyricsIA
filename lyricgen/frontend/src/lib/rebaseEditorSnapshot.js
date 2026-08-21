import { mergeThreeWay } from "../editorMerge";

// Rebase a final generation snapshot without turning a stale /generate into
// an unchecked overwrite. The immutable editor version is the preferred base;
// original_segments is the compatibility fallback for old jobs.
export async function rebaseEditorSnapshot({
  authFetch,
  api,
  jobId,
  localSegments,
  baseRevision,
  editorVersionId,
}) {
  const latestResponse = await authFetch(`${api}/editor/${jobId}`);
  let latest = {};
  try { latest = await latestResponse.json(); } catch { /* handled below */ }
  if (!latestResponse.ok || !Number.isInteger(latest.revision)) {
    return { ok: false, reason: "latest-editor-unavailable" };
  }

  let baseSegments = null;
  if (editorVersionId) {
    const versionResponse = await authFetch(
      `${api}/editor/${jobId}/versions/${encodeURIComponent(editorVersionId)}`,
    );
    if (versionResponse.ok) {
      try {
        const version = await versionResponse.json();
        if (Array.isArray(version?.segments)) baseSegments = version.segments;
      } catch { /* compatibility fallback below */ }
    }
  }
  if (!baseSegments && Number.isInteger(baseRevision)) {
    try {
      const summariesResponse = await authFetch(
        `${api}/editor/${jobId}/versions?limit=50`,
      );
      if (summariesResponse.ok) {
        const summaries = await summariesResponse.json();
        const baseVersion = (summaries?.versions || []).find(
          (version) => version.revision === baseRevision,
        );
        if (baseVersion?.id) {
          const versionResponse = await authFetch(
            `${api}/editor/${jobId}/versions/${encodeURIComponent(baseVersion.id)}`,
          );
          if (versionResponse.ok) {
            const version = await versionResponse.json();
            if (Array.isArray(version?.segments)) baseSegments = version.segments;
          }
        }
      }
    } catch { /* compatibility fallback below */ }
  }
  if (!baseSegments && Array.isArray(latest.original_segments)) {
    baseSegments = latest.original_segments;
  }
  if (!baseSegments) return { ok: false, reason: "merge-base-unavailable" };

  const merged = mergeThreeWay(baseSegments, localSegments, latest.segments || []);
  return {
    ok: true,
    latest,
    segments: merged.merged,
    hadLineConflicts: merged.conflicts.length > 0,
  };
}
