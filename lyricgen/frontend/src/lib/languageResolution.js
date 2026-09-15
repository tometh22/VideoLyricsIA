// Resolve only the exact snapshot just saved by the editor. Never GET a newer
// revision and silently attest somebody else's changes.
export async function resolveSavedLanguageReview(request, jobId, baseRevision) {
  if (!jobId || !Number.isInteger(baseRevision) || baseRevision < 0) {
    return { ok: false, reason: 'save_required' };
  }
  try {
    const response = await request(`/jobs/${jobId}/language-resolution`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ base_revision: baseRevision }),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) return { ok: false,
      reason: response.status === 409 ? 'stale_revision' : 'server' };
    return { ok: true, revision: body.revision };
  } catch { return { ok: false, reason: 'network' }; }
}
