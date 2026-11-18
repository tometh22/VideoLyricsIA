export function reviewJobPath(jobId) {
  const normalized = String(jobId || "").trim();
  return normalized ? `/review/${encodeURIComponent(normalized)}` : "/review";
}

// An effect cleanup must release its own in-flight attempt. Otherwise React's
// setup -> cleanup -> setup cycle suppresses the second request while the first
// response is correctly ignored, stranding a direct /review link on upload.
export function beginReviewResume(attemptRef, jobId) {
  if (attemptRef.current?.jobId === jobId) return null;
  const attempt = { jobId, cancelled: false };
  attempt.cancel = () => {
    attempt.cancelled = true;
    if (attemptRef.current === attempt) attemptRef.current = null;
  };
  attemptRef.current = attempt;
  return attempt;
}

// The wizard already holds the review of this job in memory: it just
// transcribed it and only gave it its durable /review/<id> URL. Re-reading
// /status there would REPLACE the operator's picks (lyric look, variant,
// typography, colours, background tab, multi-scene) with the job's
// render_params — which are still empty, because they are written by
// /generate. That is how "Revisar letra" lost the chosen look and rendered
// plain white text (incident 11-nov-2026).
//
// Only a review that the wizard built in THIS session from the operator's
// own audio (a live File/Blob) counts. A server-backed review (history link,
// campaign queue, reload — `file: null`) or a rehydrated snapshot (stub
// file) still resumes from the server, exactly as before.
export function reviewAlreadyOpen(currentReview, jobId) {
  if (!currentReview || !jobId) return false;
  if (currentReview.variantMode) return false;
  const file = currentReview.file;
  if (!file || typeof file.slice !== "function" || file._restoredStub) return false;
  const loaded = String(currentReview.transcribeJobId || "").trim();
  return !!loaded && loaded === String(jobId).trim();
}

export function reviewJobIdFromLocation(pathname, search = "") {
  const match = String(pathname || "").match(/^\/review\/([^/]+)\/?$/);
  if (match) {
    try {
      return decodeURIComponent(match[1]);
    } catch {
      return null;
    }
  }

  const legacyResumeId = new URLSearchParams(search).get("resume");
  return legacyResumeId ? legacyResumeId.trim() : null;
}
