/**
 * Wizard state persistence (sessionStorage).
 *
 * Survives navigations + tab refresh, expires after 24h. The user's pain
 * point this solves: corrected lyrics for a batch (`approvedJobs[*].segments`)
 * used to live only in React memory, so navigating to the dashboard or
 * refreshing the tab wiped every segment correction the operator made.
 *
 * What persists:
 *   - File metadata (name, size, lastModified) — File blobs themselves
 *     are not serializable. We DON'T need them for `/generate` once a
 *     song has a `transcribeJobId` (backend re-uses the audio from R2).
 *     We DO surface the filename in the resume banner so the operator
 *     recognizes which batch they're resuming.
 *   - approvedJobs[].segments — the actual lyric corrections.
 *   - approvedJobs[].transcribeJobId — lets `/generate` skip the upload.
 *   - approvedJobs[].(font|textCase|fontScale|lyricTransition|...) — render
 *     params the operator picked.
 *   - currentReview.* — the song the operator was actively editing
 *     (segments, queueIdx, render params).
 *   - reviewQueue (metadata only).
 *
 * What does NOT persist:
 *   - `File` blobs. After a refresh, audio playback in `LyricsEditor`
 *     won't work until the operator re-uploads the file (or we add an
 *     R2-signed-URL playback path; not in this PR). Segment editing
 *     still works because segments are pure data.
 *   - `prefetchCache` — that's an opportunistic Whisper warmup; on
 *     restore the cache is empty and the wizard falls through to the
 *     slow path (re-transcribe), which is the existing behavior anyway.
 */

const KEY = "genly:wizard:v1";
const TTL_MS = 24 * 60 * 60 * 1000;

function fileMeta(file) {
  if (!file || typeof file !== "object") return null;
  return {
    name: file.name || "",
    size: file.size || 0,
    type: file.type || "",
    lastModified: file.lastModified || 0,
  };
}

function stripFile(obj) {
  if (!obj || typeof obj !== "object") return obj;
  const { file, _file, ...rest } = obj;
  const meta = fileMeta(file || _file);
  return meta ? { ...rest, _fileMetadata: meta } : rest;
}

function stripQueue(queue) {
  if (!Array.isArray(queue)) return [];
  return queue.map(stripFile);
}

/**
 * Snapshot the current wizard state to sessionStorage. Strips File blobs
 * along the way. Throws are swallowed (Quota etc.) — the wizard still
 * works without persistence; we just lose the resume affordance.
 */
export function save({ files, approvedJobs, currentReview, reviewQueue }) {
  try {
    const payload = {
      timestamp: Date.now(),
      files: Array.isArray(files) ? files.map(stripFile) : [],
      approvedJobs: Array.isArray(approvedJobs) ? approvedJobs.map(stripFile) : [],
      currentReview: currentReview
        ? { ...stripFile(currentReview), queue: stripQueue(currentReview.queue) }
        : null,
      reviewQueue: stripQueue(reviewQueue),
    };
    sessionStorage.setItem(KEY, JSON.stringify(payload));
  } catch (e) {
    // QuotaExceededError, circular ref, browser w/ disabled storage etc.
    console.warn("[wizard] persistence save failed:", e?.message || e);
  }
}

/**
 * Read whatever was last saved. Returns null when there's nothing valid
 * (no key, expired, parse error). Caller decides whether to offer resume.
 */
export function load() {
  try {
    const raw = sessionStorage.getItem(KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed.timestamp !== "number") return null;
    if (Date.now() - parsed.timestamp > TTL_MS) {
      sessionStorage.removeItem(KEY);
      return null;
    }
    return parsed;
  } catch {
    return null;
  }
}

export function clear() {
  try { sessionStorage.removeItem(KEY); } catch { /* noop */ }
}

/**
 * Does the snapshot have anything worth resuming? An empty object with
 * just a timestamp shouldn't trigger the banner.
 */
export function hasResumableContent(snapshot) {
  if (!snapshot) return false;
  return (
    (snapshot.approvedJobs?.length || 0) > 0 ||
    snapshot.currentReview != null ||
    (snapshot.reviewQueue?.length || 0) > 0
  );
}

/**
 * Build the human-readable summary for the resume banner.
 */
export function summarize(snapshot) {
  if (!snapshot) return null;
  const approved = snapshot.approvedJobs?.length || 0;
  const inProgress = snapshot.currentReview ? 1 : 0;
  const total = snapshot.reviewQueue?.length
    || snapshot.files?.length
    || (approved + inProgress);
  const mins = snapshot.timestamp
    ? Math.max(1, Math.floor((Date.now() - snapshot.timestamp) / 60_000))
    : 0;
  const songNames = [
    ...(snapshot.approvedJobs || []).map(j => j._fileMetadata?.name || ""),
    ...(snapshot.currentReview ? [snapshot.currentReview._fileMetadata?.name || ""] : []),
  ].filter(Boolean).slice(0, 3);
  return { approved, inProgress, total, mins, songNames };
}

/**
 * `currentReview` survives serialization but the LyricsEditor expects
 * `currentReview.file.name` on a real File. Synthesize a minimal
 * "file-like" object so existing code paths don't NPE. Audio playback
 * stays disabled (no blob), but segment editing works.
 */
export function rehydrateReview(savedReview) {
  if (!savedReview) return null;
  const meta = savedReview._fileMetadata || {};
  const stubFile = {
    name: meta.name || "audio.mp3",
    size: meta.size || 0,
    type: meta.type || "audio/mpeg",
    lastModified: meta.lastModified || 0,
    _restoredStub: true,
  };
  return {
    ...savedReview,
    file: stubFile,
    queue: (savedReview.queue || []).map(rehydrateQueueEntry),
  };
}

/**
 * Same as rehydrateReview but for items inside reviewQueue / approvedJobs
 * (they only need the `file.name` to display correctly in headers).
 */
export function rehydrateQueueEntry(savedEntry) {
  if (!savedEntry) return null;
  const meta = savedEntry._fileMetadata || {};
  return {
    ...savedEntry,
    file: {
      name: meta.name || "audio.mp3",
      size: meta.size || 0,
      type: meta.type || "audio/mpeg",
      lastModified: meta.lastModified || 0,
      _restoredStub: true,
    },
  };
}
