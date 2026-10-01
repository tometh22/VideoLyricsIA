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

// Signed R2 media URLs are short-lived capabilities, not wizard state. Keep
// them out of sessionStorage too; a reload re-fetches a fresh URL from the
// authorized endpoint, while lyric drafts remain resumable.
function stripEphemeralMedia(obj) {
  if (!obj || typeof obj !== "object") return obj;
  const {
    audioUrl,
    audioSource,
    audioPreviewPending,
    audioPreviewRetryAt,
    audioRefreshAt,
    ...rest
  } = obj;
  return rest;
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
export function save({
  files,
  approvedJobs,
  currentReview,
  reviewQueue,
  // Audit fix 2026-05-25: top-level state del wizard que ANTES se perdía
  // en refresh. CRÍTICO para UMG: delivery (delivery_profile + umg_*)
  // cae a "youtube" silently si no se persiste → renders sin ProRes
  // master. style/customColors/inspiredByLyrics/etc. también se pierden.
  wizardStage,
  style,
  customColors,
  delivery,
  backgroundId,
  backgroundMode,
  bgSelectMode,
  animateImage,
  inspiredByLyrics,
  // Add-on premium "Escenas" (multi-escena). Sin esto el toggle se perdía en
  // un refresh/remount → el job se degradaba a fondo único en silencio.
  enableScenes,
}) {
  try {
    const payload = {
      timestamp: Date.now(),
      files: Array.isArray(files) ? files.map(stripFile) : [],
      approvedJobs: Array.isArray(approvedJobs) ? approvedJobs.map(stripFile) : [],
      currentReview: currentReview
        ? {
          ...stripEphemeralMedia(stripFile(currentReview)),
          queue: stripQueue(currentReview.queue),
        }
        : null,
      reviewQueue: stripQueue(reviewQueue),
      // Audit fix 2026-05-25: extended snapshot.
      wizardStage: wizardStage || null,
      topLevel: {
        style: style || null,
        customColors: customColors || null,
        delivery: delivery || null,
        backgroundId: backgroundId || null,
        backgroundMode: backgroundMode || null,
        bgSelectMode: bgSelectMode || null,
        animateImage: !!animateImage,
        inspiredByLyrics: inspiredByLyrics !== false,
        enableScenes: !!enableScenes,
      },
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
 *
 * HOTFIX 2026-05-29: a snapshot is ONLY resumable if at least one entry
 * has a real audio file (`File` blob) we can replay. After a refresh
 * the blobs are gone (sessionStorage can't hold them), so the rehydrate
 * produces stub objects with `name`/`size`/`type` but no `slice` /
 * Blob interface. Restoring such state lands the operator on a wizard
 * that can't play audio AND can't call `/generate` (the backend needs
 * the audio file). Worse, multiple call sites assume `entry.file` is a
 * real File and crash with "Cannot read properties of null (reading
 * 'name')" or `createObjectURL` "Overload resolution failed", tripping
 * the GlobalErrorBoundary into "Algo salió mal".
 *
 * The defensive contract: we only mark the snapshot resumable if there
 * is something the operator could actually finish — at least one entry
 * with a real File (transient survive within the same tab, e.g. after
 * navigating to /videos and back, but NOT across a full refresh). When
 * the snapshot is "skeletal" (only stubs + segments), we treat it as
 * non-resumable and let the caller `clear()` it to start fresh.
 *
 * This is a one-way fix: a skeletal snapshot is impossible to recover
 * because the audio bytes simply aren't there. Offering "Continuar"
 * just routes the operator into a broken state.
 */
function _hasReplayableAudio(entry) {
  if (!entry || !entry.file) return false;
  // A real File is a Blob; a rehydrated stub is a plain object with
  // _restoredStub or no slice/Blob interface. We accept the stub iff
  // the page hasn't been refreshed since save — `_file` (raw File)
  // would still be on the entry then.
  if (entry._file && typeof entry._file.slice === "function") return true;
  if (entry.file && typeof entry.file.slice === "function") return true;
  return false;
}

export function hasResumableContent(snapshot) {
  if (!snapshot) return false;
  const hasContent =
    (snapshot.approvedJobs?.length || 0) > 0 ||
    snapshot.currentReview != null ||
    (snapshot.reviewQueue?.length || 0) > 0;
  if (!hasContent) return false;

  // 2026-05-29 defensive: require at least one entry with a replayable
  // File. A post-refresh snapshot has only stubs (no Blob) and is
  // impossible to /generate from — surface that as "not resumable".
  const allEntries = [
    ...(snapshot.approvedJobs || []),
    ...(snapshot.currentReview ? [snapshot.currentReview] : []),
    ...(snapshot.reviewQueue || []),
    ...(snapshot.files || []),
  ];
  return allEntries.some(_hasReplayableAudio);
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
