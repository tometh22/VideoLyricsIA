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
