export function reviewJobPath(jobId) {
  const normalized = String(jobId || "").trim();
  return normalized ? `/review/${encodeURIComponent(normalized)}` : "/review";
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
