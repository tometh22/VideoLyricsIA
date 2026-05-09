import { useState, useRef, useCallback, useEffect } from "react";
import {
  Routes, Route, Navigate, Outlet,
  useNavigate, useLocation, useParams,
} from "react-router-dom";
import { useI18n } from "./i18n";
import { IS_PRODUCTION, APP_ENV } from "./env";
import { fetchWithTimeout } from "./fetchWithTimeout";
import { uploadFileToR2 } from "./r2Upload";
import LoginPage from "./components/LoginPage";
import Landing from "./components/Landing";
import Sidebar from "./components/Sidebar";
import Dashboard from "./components/Dashboard";
import HistoryView from "./components/HistoryView";
import UploadZone from "./components/UploadZone";
import LyricsEditor from "./components/LyricsEditor";
import BatchProgress from "./components/BatchProgress";
import JobDetail from "./components/JobDetail";
import Settings from "./components/Settings";
import AdminPanel from "./components/AdminPanel";

const API = import.meta.env.VITE_API_URL || "";

// --- Auth helpers ---
function getToken() {
  return localStorage.getItem("genly_token");
}
function getUser() {
  try {
    return JSON.parse(localStorage.getItem("genly_user") || "null");
  } catch {
    return null;
  }
}
function authHeaders() {
  const token = getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}
function authFetch(url, opts = {}) {
  const headers = { ...opts.headers, ...authHeaders() };
  return fetch(url, { ...opts, headers });
}

// Translates a fetch failure (network error or HTTP error response) into a
// localized, actionable banner string. Replaces the previous generic
// "Error al procesar. Intentá de nuevo." that hid the real cause —
// Railway's edge returns 502 with no CORS headers when the API container
// OOMs/timeouts on a large upload, so the browser sees only "Failed to
// fetch" and we have to infer the cause from context.
async function describeFetchError(err, res, t) {
  if (!res) {
    // Network-level failure (TypeError "Failed to fetch") OR a CORS-blocked
    // 502 from the edge proxy. Most common cause in this app: the upload
    // body was too large/slow and the edge cut the connection.
    return t("batch.error_network_or_502");
  }
  if (res.status === 413) return t("batch.error_too_large");
  if (res.status === 408 || res.status === 504) return t("batch.error_timeout");
  if (res.status >= 500) {
    let detail = "";
    try {
      const body = await res.clone().json();
      detail = body && body.detail ? `: ${String(body.detail).slice(0, 200)}` : "";
    } catch {
      try {
        const text = (await res.clone().text()).slice(0, 200).trim();
        if (text && !text.startsWith("<")) detail = `: ${text}`;
      } catch {}
    }
    return t("batch.error_server_5xx", { status: res.status }) + detail;
  }
  // 4xx (other than 408/413) — try to read a server-provided detail.
  try {
    const body = await res.clone().json();
    if (body && body.detail) return String(body.detail);
  } catch {}
  return t("batch.error_http", { status: res.status, detail: "" });
}
// Same as authFetch but aborts after `timeoutMs`. Use for dashboard /
// list hooks where a hung backend must surface as an error state, not
// as a permanent spinner.
function authFetchWithTimeout(url, opts = {}, timeoutMs = 10_000) {
  const headers = { ...opts.headers, ...authHeaders() };
  return fetchWithTimeout(url, { ...opts, headers }, timeoutMs);
}

// --- Routing helpers ---
function RequireAuth({ token, children }) {
  if (!token) return <Navigate to="/" replace />;
  return children;
}

// Handles one-shot URL-param callbacks (Stripe billing return, email
// verification, password-reset deep links). Mounted once inside the
// router, NOT as a child of <Routes>, so it doesn't remount per nav.
function RootEffects({ setUser, setResetToken }) {
  const navigate = useNavigate();
  const location = useLocation();
  const ranRef = useRef(false);

  useEffect(() => {
    if (ranRef.current) return;
    ranRef.current = true;
    const params = new URLSearchParams(location.search);
    if (params.get("billing") === "success") {
      if (getToken()) {
        authFetch(`${API}/auth/me`).then(r => r.json()).then(userData => {
          localStorage.setItem("genly_user", JSON.stringify(userData));
          setUser(userData);
        }).catch(() => {});
      }
      navigate(location.pathname, { replace: true });
    }
    if (params.get("verify_email")) {
      fetch("/auth/verify-email", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: params.get("verify_email") }),
      }).catch(() => {});
      navigate(location.pathname, { replace: true });
    }
    if (params.get("reset_password")) {
      setResetToken(params.get("reset_password"));
      navigate("/login", { replace: true });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return null;
}

// Layout shell for authenticated routes. Computes Sidebar's activeView
// from the current pathname so Sidebar.jsx itself doesn't change.
function AppShell({ user, sidebarOpen, setSidebarOpen, onLogout }) {
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const activeView =
    (pathname === "/new" || pathname === "/review" || pathname === "/generating") ? "new" :
    (pathname === "/videos" || pathname.startsWith("/videos/")) ? "history" :
    pathname === "/account" ? "settings" :
    pathname === "/admin" ? "admin" :
    "dashboard";

  const handleNav = (id) => {
    if (id === "dashboard") navigate("/dashboard");
    else if (id === "new") navigate("/new");
    else if (id === "history") navigate("/videos");
    else if (id === "settings") navigate("/account");
    else if (id === "admin") navigate("/admin");
  };

  return (
    <div className="min-h-screen bg-surface flex">
      <Sidebar
        activeView={activeView}
        onNav={handleNav}
        open={sidebarOpen}
        onToggle={() => setSidebarOpen(!sidebarOpen)}
        user={user}
        onLogout={onLogout}
      />

      <div className={`flex-1 min-h-screen transition-all duration-300 ${sidebarOpen ? "ml-64" : "ml-0"}`}>
        {/* Ambient */}
        <div className="fixed inset-0 pointer-events-none">
          <div className="absolute top-[-30%] left-[20%] w-[600px] h-[600px] bg-brand/[0.03] rounded-full blur-[120px]" />
          <div className="absolute bottom-[-20%] right-[-5%] w-[500px] h-[500px] bg-brand-light/[0.02] rounded-full blur-[100px]" />
        </div>

        {/* Top bar */}
        <header className="sticky top-0 z-20 flex items-center justify-between px-8 py-4 border-b border-white/[0.04] bg-surface/80 backdrop-blur-xl" style={{boxShadow: '0 1px 12px rgba(0,0,0,0.2)'}}>
          <div className="flex items-center gap-3">
            {!sidebarOpen && (
              <button onClick={() => setSidebarOpen(true)} className="mr-2 text-gray-400 hover:text-white transition-colors">
                <svg className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24"><path d="M4 6h16M4 12h16M4 18h16"/></svg>
              </button>
            )}
          </div>
          <div className="flex items-center gap-4">
            {user && (
              <div className="flex items-center gap-2">
                <div className="w-7 h-7 rounded-lg bg-gradient-to-br from-brand/20 to-brand-light/20 flex items-center justify-center border border-white/[0.06]">
                  <span className="text-[10px] font-bold text-brand uppercase">{user.username?.charAt(0)}</span>
                </div>
                <span className="text-xs text-gray-500">{user.username}</span>
              </div>
            )}
          </div>
        </header>

        {/* Content */}
        <main className="relative z-10 px-8 pt-8 pb-20">
          <Outlet />
        </main>
      </div>
    </div>
  );
}

// Old `/v/:id` URLs (shared before the rename) bounce to the new
// `/videos/:id` so previously-pasted links keep working.
function LegacyVideoRedirect() {
  const { id } = useParams();
  return <Navigate to={`/videos/${id}`} replace />;
}

// Deep-link adapter for /videos/:id — fetches the job by id so refreshing on
// JobDetail or pasting a shared URL works without depending on App's
// in-memory selectedJob.
function JobDetailRoute({ fetchHistory }) {
  const { id } = useParams();
  const navigate = useNavigate();
  const [job, setJob] = useState(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    let alive = true;
    setJob(null);
    setError(false);
    authFetch(`${API}/status/${id}`)
      .then(r => r.ok ? r.json() : Promise.reject())
      .then(j => { if (alive) setJob(j); })
      .catch(() => { if (alive) setError(true); });
    return () => { alive = false; };
  }, [id]);

  if (error) {
    return (
      <div className="text-center mt-16">
        <p className="text-gray-500 mb-4">No se encontró el video.</p>
        <button onClick={() => navigate("/dashboard")} className="btn-secondary">Volver</button>
      </div>
    );
  }
  if (!job) {
    return <div className="w-12 h-12 mx-auto mt-16 border-2 border-brand border-t-transparent rounded-full animate-spin" />;
  }
  return (
    <div className="flex justify-center">
      <JobDetail
        job={job}
        onBack={() => navigate("/dashboard")}
        onJobUpdate={(updatedJob) => { setJob(updatedJob); fetchHistory(); }}
      />
    </div>
  );
}

export default function App() {
  const { t } = useI18n();
  const navigate = useNavigate();

  const [token, setToken] = useState(getToken());
  const [user, setUser] = useState(getUser());
  const [files, setFiles] = useState([]);
  const [delivery, setDelivery] = useState({
    delivery_profile: "youtube",
    umg_frame_size: "HD",
    umg_fps: 24,
    umg_prores_profile: 3,
  });
  const style = "oscuro";

  const [reviewQueue, setReviewQueue] = useState([]);
  const [currentReview, setCurrentReview] = useState(null);
  const [approvedJobs, setApprovedJobs] = useState([]);
  const [transcribing, setTranscribing] = useState(false);
  const [transcribeError, setTranscribeError] = useState(null);
  // {phase: "uploading"|"transcribing", loaded, total} during the
  // upload→whisper handoff. Drives the progress bar in /review.
  const [transcribeProgress, setTranscribeProgress] = useState(null);
  const [readyToGenerate, setReadyToGenerate] = useState(false);

  const [jobs, setJobs] = useState([]);
  // Pre-fetched transcription results for batch review songs 1..N-1.
  // While the user edits song 0, songs 1..N are uploaded + transcribed
  // in background. keyed by queue index.
  const prefetchCache = useRef({});
  const [history, setHistory] = useState([]);
  const [backgroundFile, setBackgroundFile] = useState(null);
  const [animateImage, setAnimateImage] = useState(false);
  const [backgroundId, setBackgroundId] = useState(null);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [resetToken, setResetToken] = useState(null);
  const pollingIntervals = useRef(new Set());
  const PARALLEL_WORKERS = 5;

  // --- Stamp the document title with the environment when not in prod ---
  useEffect(() => {
    if (!IS_PRODUCTION) {
      document.title = `[${APP_ENV.toUpperCase()}] GenLy`;
    }
  }, []);

  // --- Auth ---
  const handleLogin = (newToken, newUser) => {
    localStorage.setItem("genly_token", newToken);
    localStorage.setItem("genly_user", JSON.stringify(newUser));
    setToken(newToken);
    setUser(newUser);
  };

  const handleLogout = useCallback(() => {
    // Stop every active poll BEFORE clearing the token. Otherwise the
    // intervals keep firing authFetch() with no token, triggering 401s
    // on every tick until the tab is closed (and re-entering this
    // handler in a loop).
    pollingIntervals.current.forEach((iv) => clearInterval(iv));
    pollingIntervals.current.clear();
    localStorage.removeItem("genly_token");
    localStorage.removeItem("genly_user");
    setToken(null);
    setUser(null);
    navigate("/");
  }, [navigate]);

  // `historyError` lets the dashboard surface a "connection failed,
  // retry" state instead of silently rendering an empty list when /jobs
  // hangs or 5xx's (CORS misconfig, backend cold start, R2 outage). The
  // poller and detail-view consumers don't see this — they get the
  // current `history` array, fresh or stale.
  const [historyError, setHistoryError] = useState(false);
  // `historyLoaded` distinguishes "first fetch still in flight" from
  // "fetch returned []". Without it, HistoryView showed "Aún no hay
  // videos" during the initial load on slow tenants — operators with
  // hundreds of jobs thought their catalog was wiped.
  const [historyLoaded, setHistoryLoaded] = useState(false);
  const fetchHistory = useCallback(async () => {
    if (!getToken()) return;
    // /jobs has historically been the slow query for big tenants (no
    // composite index on tenant_id+created_at), so a single 10s timeout
    // turns into "permanent" empty state. Two short retries with
    // exponential backoff usually catch the second call after PG has
    // the plan cached, without bashing the backend.
    const maxAttempts = 3;
    for (let attempt = 1; attempt <= maxAttempts; attempt++) {
      try {
        const res = await authFetchWithTimeout(`${API}/jobs`);
        if (res.status === 401) { handleLogout(); return; }
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        if (!Array.isArray(data)) throw new Error("malformed");
        setHistory(data);
        setHistoryError(false);
        setHistoryLoaded(true);
        return;
      } catch {
        if (attempt < maxAttempts) {
          await new Promise((r) => setTimeout(r, 1000 * 2 ** attempt));
          continue;
        }
        setHistoryError(true);
        setHistoryLoaded(true);
      }
    }
  }, [handleLogout]);

  useEffect(() => { if (token) fetchHistory(); }, [token, fetchHistory]);

  const pollJob = useCallback((jobId) => {
    // Poll every 3 s (instead of 1 s) and skip the tick entirely when the tab
    // is hidden. For a user with a few tabs open and 20 active jobs this cuts
    // the request rate by ~90%.
    return new Promise((resolve) => {
      const iv = setInterval(async () => {
        if (typeof document !== "undefined" && document.hidden) return;
        // Token can disappear mid-poll if the user logs out from another
        // tab; bail rather than spamming 401s.
        if (!getToken()) {
          clearInterval(iv);
          pollingIntervals.current.delete(iv);
          resolve("aborted");
          return;
        }
        try {
          const res = await authFetch(`${API}/status/${jobId}`);
          if (res.status === 401) {
            clearInterval(iv);
            pollingIntervals.current.delete(iv);
            handleLogout();
            resolve("unauthorized");
            return;
          }
          if (!res.ok) return;
          const data = await res.json();
          setJobs((prev) => prev.map((j) =>
            j.job_id === jobId ? { ...j, status: data.status, current_step: data.current_step, progress: data.progress, error: data.error } : j
          ));
          if (data.status === "done" || data.status === "error" || data.status === "pending_review" || data.status === "validation_failed") {
            clearInterval(iv);
            pollingIntervals.current.delete(iv);
            fetchHistory();
            resolve(data.status);
          }
        } catch {}
      }, 3000);
      pollingIntervals.current.add(iv);
    });
  }, [fetchHistory]);

  useEffect(() => () => {
    pollingIntervals.current.forEach((iv) => clearInterval(iv));
  }, []);

  // Pre-upload + transcribe songs at indices fromIdx..queue.length-1 in the
  // background while the user is actively reviewing a different song. Results
  // land in prefetchCache.current[idx] so transcribeNext can serve them
  // instantly instead of making the user wait for the round-trip.
  const prefetchRemaining = useCallback(async (queue, fromIdx) => {
    for (let idx = fromIdx; idx < queue.length; idx++) {
      // Skip if already fetched or in-flight.
      if (prefetchCache.current[idx]) continue;
      prefetchCache.current[idx] = { status: "loading" };
      const entry = queue[idx];
      try {
        const { jobId } = await uploadFileToR2(entry.file, {
          meta: { artist: entry.artist || "", title: (entry.songTitle || "").trim() },
        });
        const res = await authFetch(`${API}/transcribe-uploaded`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            job_id: jobId,
            language: entry.language || "",
            artist: entry.artist || "",
            title: (entry.songTitle || "").trim(),
          }),
        });
        if (res.ok) {
          const data = await res.json();
          prefetchCache.current[idx] = { status: "ready", data, jobId };
        } else {
          prefetchCache.current[idx] = { status: "error" };
        }
      } catch {
        prefetchCache.current[idx] = { status: "error" };
      }
    }
  }, []);

  // --- Review flow ---
  const handleStartReview = async () => {
    if (!files.length || !files.every((f) => f.artist.trim())) return;
    prefetchCache.current = {};
    setReviewQueue([...files]);
    navigate("/review");
    transcribeNext([...files], 0);
  };

  const handleGenerateDirect = () => {
    if (!files.length || !files.every((f) => f.artist.trim())) return;
    const jobList = files.map((f) => ({
      filename: f.file.name, _file: f.file, artist: f.artist.trim(),
      songTitle: (f.songTitle || "").trim(),
      language: f.language, genre: f.genre || "", font: f.font || "",
      concept: f.concept || "", movementStyle: f.movementStyle || "",
      status: "queued", current_step: null,
      progress: 0, job_id: null, error: null,
    }));
    setJobs(jobList);
    navigate("/generating");
    processQueueDirect(jobList);
  };

  const transcribeNext = async (queue, idx) => {
    if (idx >= queue.length) return;
    const entry = queue[idx];

    // Fast path: a background prefetch already finished for this index.
    const cached = prefetchCache.current[idx];
    if (cached?.status === "ready") {
      const { data, jobId } = cached;
      setTranscribing(false);
      setTranscribeProgress(null);
      setCurrentReview({
        file: entry.file, artist: entry.artist, language: entry.language,
        songTitle: entry.songTitle || "",
        genre: entry.genre || "", font: entry.font || "",
        concept: entry.concept || "", movementStyle: entry.movementStyle || "",
        textCase: entry.textCase || "upper",
        fontScale: entry.fontScale || "1.0",
        lyricTransition: entry.lyricTransition || "cut",
        textMotion: entry.textMotion || "none",
        segments: data.segments, referenceLyrics: data.reference_lyrics || "",
        coverageWarning: !!data.coverage_warning,
        recoverySource: data.recovery_source || "",
        transcribeJobId: data.job_id || jobId,
        queueIdx: idx, queue,
      });
      // Kick off prefetch for all remaining songs.
      prefetchRemaining(queue, idx + 1);
      return;
    }

    // Slow path: upload + transcribe now (first song, or prefetch missed).
    setTranscribing(true);
    setTranscribeError(null);
    setTranscribeProgress({ phase: "uploading", loaded: 0, total: entry.file.size });

    let transcribeRes = null;
    try {
      // Step 1: stream the audio body straight to R2 via a presigned URL.
      // The API container never sees the bytes — that's the whole point
      // of the v2 flow. uploadFileToR2 picks single-PUT or multipart
      // automatically based on file size.
      const { jobId: uploadJobId } = await uploadFileToR2(entry.file, {
        meta: {
          artist: entry.artist || "",
          title: (entry.songTitle || "").trim(),
        },
        onProgress: (loaded, total) => {
          setTranscribeProgress({ phase: "uploading", loaded, total });
        },
      });

      // Step 2: tell the API to fetch the just-uploaded audio from R2,
      // run Whisper / lrclib, return segments. Same shape as the
      // legacy /transcribe response.
      setTranscribeProgress({ phase: "transcribing", loaded: 0, total: 0 });
      transcribeRes = await authFetch(`${API}/transcribe-uploaded`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          job_id: uploadJobId,
          language: entry.language || "",
          artist: entry.artist || "",
          title: (entry.songTitle || "").trim(),
        }),
      });
      if (!transcribeRes.ok) {
        const reason = await describeFetchError(null, transcribeRes, t);
        setTranscribing(false);
        setTranscribeProgress(null);
        setTranscribeError(reason);
        return;
      }
      const data = await transcribeRes.json();
      setTranscribing(false);
      setTranscribeProgress(null);
      setCurrentReview({
        file: entry.file, artist: entry.artist, language: entry.language,
        songTitle: entry.songTitle || "",
        genre: entry.genre || "", font: entry.font || "",
        concept: entry.concept || "", movementStyle: entry.movementStyle || "",
        textCase: entry.textCase || "upper",
        fontScale: entry.fontScale || "1.0",
        lyricTransition: entry.lyricTransition || "cut",
        textMotion: entry.textMotion || "none",
        segments: data.segments, referenceLyrics: data.reference_lyrics || "",
        coverageWarning: !!data.coverage_warning,
        recoverySource: data.recovery_source || "",
        transcribeJobId: data.job_id || uploadJobId,
        queueIdx: idx, queue,
      });
      // Kick off background upload+transcription for songs idx+1..N-1
      // while the user is reading/editing the current song's lyrics.
      prefetchRemaining(queue, idx + 1);
    } catch (err) {
      setTranscribing(false);
      setTranscribeProgress(null);
      const reason = await describeFetchError(err, transcribeRes, t);
      setTranscribeError(reason);
    }
  };

  const handleApproveLyrics = (editedSegments) => {
    const r = currentReview;
    const newApproved = [...approvedJobs, {
      file: r.file, artist: r.artist, language: r.language,
      songTitle: r.songTitle || "",
      genre: r.genre || "", font: r.font || "", concept: r.concept || "",
      movementStyle: r.movementStyle || "",
      textCase: r.textCase || "upper",
      fontScale: r.fontScale || "1.0",
      lyricTransition: r.lyricTransition || "cut",
      textMotion: r.textMotion || "none",
      segments: editedSegments,
      transcribeJobId: r.transcribeJobId || null,
    }];
    setApprovedJobs(newApproved);
    setCurrentReview(null);

    const nextIdx = r.queueIdx + 1;
    if (nextIdx < r.queue.length) {
      transcribeNext(r.queue, nextIdx);
    } else if (r.queue.length === 1) {
      startGenerationWithSegments(newApproved);
    } else {
      setReadyToGenerate(true);
    }
  };

  const startGenerationWithSegments = async (approved) => {
    const jobList = approved.map((a) => ({
      filename: a.file.name, _file: a.file, artist: a.artist,
      songTitle: (a.songTitle || "").trim(),
      language: a.language, genre: a.genre || "", font: a.font || "",
      concept: a.concept || "", movementStyle: a.movementStyle || "",
      textCase: a.textCase || "upper",
      fontScale: a.fontScale || "1.0",
      lyricTransition: a.lyricTransition || "cut",
      textMotion: a.textMotion || "none",
      segments: a.segments,
      transcribeJobId: a.transcribeJobId || null,
      status: "queued", current_step: null, progress: 0, job_id: null, error: null,
    }));
    setJobs(jobList);
    navigate("/generating");
    setReadyToGenerate(false);
    setApprovedJobs([]);

    let nextIdx = 0;
    const worker = async () => {
      while (nextIdx < jobList.length) {
        const i = nextIdx++;
        setJobs((prev) => prev.map((j, idx) =>
          idx === i ? { ...j, status: "processing", current_step: "background", progress: 22 } : j
        ));
        const formData = new FormData();
        // When /transcribe persisted the audio for us, send the job_id so
        // the backend reuses the file from R2 / disk instead of re-reading
        // a 30-50 MB WAV body. Falls back to the legacy file upload if the
        // backend didn't return a job_id (older deploy).
        if (jobList[i].transcribeJobId) {
          formData.append("job_id", jobList[i].transcribeJobId);
        } else {
          formData.append("file", jobList[i]._file);
        }
        formData.append("artist", jobList[i].artist);
        if (jobList[i].songTitle) formData.append("song_title", jobList[i].songTitle);
        formData.append("style", style);
        if (jobList[i].language) formData.append("language", jobList[i].language);
        if (jobList[i].genre) formData.append("genre", jobList[i].genre);
        if (jobList[i].font) formData.append("font", jobList[i].font);
        if (jobList[i].concept) formData.append("concept", jobList[i].concept);
        if (jobList[i].movementStyle) formData.append("movement_style", jobList[i].movementStyle);
        formData.append("text_case", jobList[i].textCase || "upper");
        formData.append("font_scale", String(jobList[i].fontScale || "1.0"));
        formData.append("lyric_transition", jobList[i].lyricTransition || "cut");
        formData.append("text_motion", jobList[i].textMotion || "none");
        if (animateImage && backgroundFile) formData.append("animate_image", "true");
        formData.append("segments_json", JSON.stringify(jobList[i].segments));
        formData.append("delivery_profile", delivery.delivery_profile);
        if (delivery.delivery_profile !== "youtube") {
          formData.append("umg_frame_size", delivery.umg_frame_size);
          formData.append("umg_fps", String(delivery.umg_fps));
          formData.append("umg_prores_profile", String(delivery.umg_prores_profile));
        }
        if (backgroundId) formData.append("background_id", backgroundId);
        else if (backgroundFile) formData.append("background_file", backgroundFile);

        let res = null;
        try {
          res = await authFetch(`${API}/generate`, { method: "POST", body: formData });
          let data;
          try {
            data = await res.json();
          } catch {
            // Non-JSON body (HTML error page from edge proxy on 502/504).
            const reason = await describeFetchError(null, res, t);
            setJobs((prev) => prev.map((j, idx) =>
              idx === i ? { ...j, status: "error", error: reason } : j
            ));
            continue;
          }
          if (!res.ok || data.detail) {
            const reason = data.detail || await describeFetchError(null, res, t);
            setJobs((prev) => prev.map((j, idx) =>
              idx === i ? { ...j, status: "error", error: reason } : j
            ));
            continue;
          }
          setJobs((prev) => prev.map((j, idx) => (idx === i ? { ...j, job_id: data.job_id } : j)));
          await pollJob(data.job_id);
        } catch (err) {
          const reason = await describeFetchError(err, res, t);
          setJobs((prev) => prev.map((j, idx) =>
            idx === i ? { ...j, status: "error", error: reason } : j
          ));
        }
      }
    };
    await Promise.all(Array.from({ length: Math.min(PARALLEL_WORKERS, jobList.length) }, () => worker()));
  };

  const processQueueDirect = async (jobList) => {
    // v2 flow: browser → R2 (presigned PUT) → /generate with job_id +
    // empty segments_json (auto-transcribe in worker). The audio body
    // never touches the API container, so we don't need the 429/503
    // soft-fail retry maze that wrapped the old /upload — R2 is its own
    // throttle domain and r2Upload.js already retries failed parts.
    let nextIdx = 0;
    const worker = async () => {
      while (nextIdx < jobList.length) {
        const i = nextIdx++;
        setJobs((prev) => prev.map((j, idx) =>
          idx === i ? {
            ...j, status: "processing", current_step: "uploading", progress: 0,
          } : j
        ));
        let uploadJobId = null;
        try {
          const result = await uploadFileToR2(jobList[i]._file, {
            meta: {
              artist: jobList[i].artist,
              title: jobList[i].songTitle || "",
            },
            onProgress: (loaded, total) => {
              const pct = total > 0 ? Math.round((loaded / total) * 100) : 0;
              setJobs((prev) => prev.map((j, idx) =>
                idx === i ? {
                  ...j, current_step: "uploading", progress: pct,
                } : j
              ));
            },
          });
          uploadJobId = result.jobId;
        } catch (err) {
          const reason = await describeFetchError(err, err.response || null, t);
          setJobs((prev) => prev.map((j, idx) =>
            idx === i ? { ...j, status: "error", error: reason } : j
          ));
          continue;
        }

        // Upload finished. Hand the job off to the worker; segments_json=[]
        // tells the pipeline to run Whisper itself (no editor flow).
        setJobs((prev) => prev.map((j, idx) =>
          idx === i ? {
            ...j, current_step: "whisper", progress: 0, job_id: uploadJobId,
          } : j
        ));
        const generateBody = new FormData();
        generateBody.append("job_id", uploadJobId);
        generateBody.append("artist", jobList[i].artist);
        if (jobList[i].songTitle) generateBody.append("song_title", jobList[i].songTitle);
        generateBody.append("style", style);
        generateBody.append("segments_json", "[]");
        generateBody.append("delivery_profile", delivery.delivery_profile);
        if (delivery.delivery_profile !== "youtube") {
          generateBody.append("umg_frame_size", delivery.umg_frame_size);
          generateBody.append("umg_fps", String(delivery.umg_fps));
          generateBody.append("umg_prores_profile", String(delivery.umg_prores_profile));
        }
        if (jobList[i].language) generateBody.append("language", jobList[i].language);
        if (jobList[i].genre) generateBody.append("genre", jobList[i].genre);
        if (jobList[i].font) generateBody.append("font", jobList[i].font);
        if (jobList[i].concept) generateBody.append("concept", jobList[i].concept);
        if (jobList[i].movementStyle) generateBody.append("movement_style", jobList[i].movementStyle);
        generateBody.append("text_case", jobList[i].textCase || "upper");
        generateBody.append("font_scale", String(jobList[i].fontScale || "1.0"));
        generateBody.append("lyric_transition", jobList[i].lyricTransition || "cut");
        generateBody.append("text_motion", jobList[i].textMotion || "none");
        if (animateImage && backgroundFile) generateBody.append("animate_image", "true");
        if (backgroundId) generateBody.append("background_id", backgroundId);
        else if (backgroundFile) generateBody.append("background_file", backgroundFile);

        let genRes = null;
        try {
          genRes = await authFetch(`${API}/generate`, {
            method: "POST", body: generateBody,
          });
          let data;
          try {
            data = await genRes.json();
          } catch {
            const reason = await describeFetchError(null, genRes, t);
            setJobs((prev) => prev.map((j, idx) =>
              idx === i ? { ...j, status: "error", error: reason } : j
            ));
            continue;
          }
          if (!genRes.ok || data.detail) {
            const reason = data.detail || await describeFetchError(null, genRes, t);
            setJobs((prev) => prev.map((j, idx) =>
              idx === i ? { ...j, status: "error", error: reason } : j
            ));
            continue;
          }
          await pollJob(uploadJobId);
        } catch (err) {
          const reason = await describeFetchError(err, genRes, t);
          setJobs((prev) => prev.map((j, idx) =>
            idx === i ? { ...j, status: "error", error: reason } : j
          ));
        }
      }
    };
    await Promise.all(Array.from({ length: Math.min(PARALLEL_WORKERS, jobList.length) }, () => worker()));
  };

  const handleReset = (skipConfirm = false) => {
    // Confirm whenever there's any wizard state at risk — not only when
    // jobs are running. Without this, the user could lose an in-progress
    // batch (transcribing / approved / ready-to-generate) without warning.
    const hasState = jobs.some((j) => j.status === "processing" || j.status === "queued")
                  || approvedJobs.length > 0
                  || currentReview !== null
                  || reviewQueue.length > 0
                  || files.length > 0;
    if (hasState && !skipConfirm && !window.confirm(t("batch.confirm_cancel"))) return;
    pollingIntervals.current.forEach((iv) => clearInterval(iv));
    pollingIntervals.current.clear();
    prefetchCache.current = {};
    setFiles([]); setJobs([]); setBackgroundFile(null); setBackgroundId(null);
    setReviewQueue([]); setCurrentReview(null); setApprovedJobs([]);
    setTranscribing(false); setReadyToGenerate(false); setTranscribeError(null);
    navigate("/dashboard");
    fetchHistory();
  };

  // Step-back inside the lyrics-review wizard. Walks one step backward
  // through the batch queue without resetting state:
  //   - canción N>1 → re-open the editor for canción N-1 with its
  //     already-edited segments. Pops that entry from approvedJobs
  //     so it can be re-approved.
  //   - canción 1 (no approved yet) → /new with files[] still intact.
  // Distinct from handleReset (which discards the whole batch).
  const handleBackInReview = () => {
    if (approvedJobs.length > 0) {
      const last = approvedJobs[approvedJobs.length - 1];
      setApprovedJobs(approvedJobs.slice(0, -1));
      setCurrentReview({
        file: last.file,
        artist: last.artist,
        language: last.language,
        genre: last.genre || "",
        font: last.font || "",
        concept: last.concept || "",
        movementStyle: last.movementStyle || "",
        textCase: last.textCase || "upper",
        fontScale: last.fontScale || "1.0",
        lyricTransition: last.lyricTransition || "cut",
        textMotion: last.textMotion || "none",
        segments: last.segments,
        referenceLyrics: "",
        coverageWarning: false,
        recoverySource: "",
        queueIdx: approvedJobs.length - 1,
        queue: reviewQueue,
      });
      setReadyToGenerate(false);
      setTranscribing(false);
      setTranscribeError(null);
      return;
    }
    setCurrentReview(null);
    setReviewQueue([]);
    setTranscribing(false);
    setTranscribeError(null);
    navigate("/new");
  };

  const handleGenerateBatch = () => {
    setReadyToGenerate(false);
    startGenerationWithSegments(approvedJobs);
  };

  const handleSelectJob = (jobId) => {
    navigate(`/videos/${jobId}`);
  };

  const handleBulkApproveBatch = async (jobIds) => {
    if (!Array.isArray(jobIds) || jobIds.length === 0) return;
    for (const jobId of jobIds) {
      try {
        const res = await authFetch(`${API}/approve/${jobId}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ notes: "" }),
        });
        if (res.ok) {
          setJobs((prev) =>
            prev.map((j) => j.job_id === jobId ? { ...j, status: "done" } : j)
          );
        }
      } catch {}
    }
    fetchHistory();
  };

  const handleDeleteJob = async (jobId) => {
    try {
      const res = await authFetch(`${API}/jobs/${jobId}`, { method: "DELETE" });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        alert(data.detail || "No se pudo eliminar el video.");
        return;
      }
      // Optimistically drop from local list so the row disappears immediately.
      setHistory((prev) => prev.filter((j) => j.job_id !== jobId));
    } catch {
      alert("Error de red al eliminar.");
    }
  };

  const handleBulkDeleteJobs = async (jobIds) => {
    if (!Array.isArray(jobIds) || jobIds.length === 0) return;
    try {
      const res = await authFetch(`${API}/jobs/bulk-delete`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job_ids: jobIds }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        alert(data.detail || "No se pudieron eliminar.");
        return;
      }
      const data = await res.json().catch(() => ({ deleted: [], skipped: {} }));
      const deletedSet = new Set(data.deleted || []);
      setHistory((prev) => prev.filter((j) => !deletedSet.has(j.job_id)));
      const skippedCount = Object.keys(data.skipped || {}).length;
      if (skippedCount > 0) {
        alert(`${data.deleted.length} eliminados, ${skippedCount} omitidos (protegidos o no encontrados).`);
      }
    } catch {
      alert("Error de red al eliminar.");
    }
  };

  const allHaveArtist = files.length > 0 && files.every((f) => f.artist.trim());

  // --- Per-route screens (kept inline so they share App-level state) ---

  const newBatchScreen = (
    <div className="w-full max-w-4xl mx-auto animate-fade-in">
      <div className="flex items-center gap-3 mb-8">
        <button onClick={() => navigate("/dashboard")}
          className="w-9 h-9 rounded-xl glass flex items-center justify-center text-gray-400 hover:text-white transition-colors">
          <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
            <path d="M19 12H5M12 19l-7-7 7-7" />
          </svg>
        </button>
        <div>
          <h1 className="text-2xl font-bold">{t("upload.new_batch")}</h1>
          <p className="text-sm text-gray-500">{t("upload.new_batch_sub")}</p>
        </div>
      </div>

      <UploadZone
        files={files}
        onFiles={setFiles}
        delivery={delivery}
        onDeliveryChange={setDelivery}
        backgroundFile={backgroundFile}
        onBackgroundFile={setBackgroundFile}
        backgroundId={backgroundId}
        onBackgroundId={setBackgroundId}
        animateImage={animateImage}
        onAnimateImage={setAnimateImage}
        allHaveArtist={allHaveArtist}
        onStartReview={handleStartReview}
        onGenerateDirect={handleGenerateDirect}
        user={user}
        sidebarOpen={sidebarOpen}
      />
    </div>
  );

  // /review handles three sub-states: spinner while transcribing,
  // LyricsEditor when a song is ready to review, and the batch summary
  // before launching generation. Empty state → redirect home.
  const reviewScreen = (() => {
    if (transcribeError && !transcribing) {
      return (
        <div className="w-full max-w-md mx-auto mt-8 animate-fade-in">
          <div className="rounded-xl bg-red-500/10 border border-red-500/20 px-5 py-4 text-center">
            <p className="text-sm text-red-400">{transcribeError}</p>
            <button onClick={() => { setTranscribeError(null); navigate("/new"); }}
              className="mt-3 text-xs text-gray-400 hover:text-white transition-colors underline">
              {t("detail.back")}
            </button>
          </div>
        </div>
      );
    }
    if (transcribing) {
      const phase = transcribeProgress?.phase;
      const loaded = transcribeProgress?.loaded || 0;
      const total = transcribeProgress?.total || 0;
      const pct = phase === "uploading" && total > 0
        ? Math.round((loaded / total) * 100)
        : null;
      const phaseLabel = (
        phase === "uploading" ? t("transcribe.uploading") :
        phase === "transcribing" ? t("transcribe.title") :
        t("transcribe.title")
      );
      const phaseSub = (
        phase === "uploading" && pct !== null
          ? t("transcribe.uploading_progress", { pct })
          : t("transcribe.subtitle")
      );
      return (
        <div className="w-full max-w-md mx-auto mt-16 animate-fade-in text-center">
          {pct !== null ? (
            <div className="w-full max-w-xs mx-auto mb-4">
              <div className="h-1.5 bg-surface-1 rounded-full overflow-hidden">
                <div
                  className="h-full rounded-full bg-gradient-to-r from-brand to-brand-light transition-all duration-300"
                  style={{ width: `${pct}%` }}
                />
              </div>
            </div>
          ) : (
            <div className="w-12 h-12 mx-auto mb-4 border-2 border-brand border-t-transparent rounded-full animate-spin" />
          )}
          <h2 className="text-xl font-bold mb-2">{phaseLabel}</h2>
          <p className="text-gray-500 text-sm">{phaseSub}</p>
          {reviewQueue.length > 1 && (
            <p className="text-xs text-gray-600 mt-2">
              {t("transcribe.song")} {approvedJobs.length + 1} {t("editor.song_of")} {reviewQueue.length}
            </p>
          )}
        </div>
      );
    }
    if (currentReview) {
      return (
        <div className="flex justify-center">
          <LyricsEditor
            // key forces a fresh mount when stepping forward/backward
            // through the batch — LyricsEditor seeds its `edited` state
            // from props.segments only on mount, so without the key the
            // editor would keep showing the previous song's segments
            // when handleBackInReview swaps currentReview underneath it.
            key={`${currentReview.file.name}:${currentReview.queueIdx}`}
            segments={currentReview.segments}
            filename={currentReview.file.name}
            audioFile={currentReview.file}
            referenceLyrics={currentReview.referenceLyrics || ""}
            coverageWarning={currentReview.coverageWarning}
            recoverySource={currentReview.recoverySource}
            onApprove={handleApproveLyrics}
            onBack={handleBackInReview}
            isBatch={currentReview.queue.length > 1}
            batchProgress={currentReview.queue.length > 1
              ? `${currentReview.queueIdx + 1} ${t("editor.song_of")} ${currentReview.queue.length}`
              : ""}
            user={user}
            font={currentReview.font || ""}
            textCase={currentReview.textCase || "upper"}
            fontScale={parseFloat(currentReview.fontScale || "1.0")}
            lyricTransition={currentReview.lyricTransition || "cut"}
            textMotion={currentReview.textMotion || "none"}
          />
        </div>
      );
    }
    if (readyToGenerate) {
      return (
        <div className="w-full max-w-xl mx-auto animate-fade-in">
          <div className="text-center mb-8">
            <div className="w-14 h-14 mx-auto mb-4 rounded-2xl bg-accent/10 flex items-center justify-center">
              <svg className="w-7 h-7 text-accent" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                <polyline points="20 6 9 17 4 12" />
              </svg>
            </div>
            <h2 className="text-2xl font-bold mb-2">{approvedJobs.length} {t("ready.title")}</h2>
            <p className="text-gray-500">{t("ready.subtitle")}</p>
          </div>

          <div className="space-y-1.5 mb-8 max-h-60 overflow-y-auto">
            {approvedJobs.map((job, i) => (
              <div key={i} className="flex items-center gap-3 glass rounded-xl px-4 py-2.5">
                <div className="w-2 h-2 rounded-full bg-accent shrink-0" />
                <span className="text-sm text-white truncate flex-1">{job.file.name.replace(/\.mp3$/i, "")}</span>
                <span className="text-xs text-gray-500">{job.segments.length} {t("editor.lines")}</span>
              </div>
            ))}
          </div>

          <div className="flex gap-3 justify-center items-center">
            <button onClick={handleBackInReview} className="btn-secondary">
              ← {t("detail.back") || "Volver"}
            </button>
            <button onClick={handleGenerateBatch} className="btn-primary text-lg py-4 px-8">
              {t("ready.generate")} {approvedJobs.length} {t("ready.videos")}
            </button>
          </div>
          <div className="flex justify-center mt-3">
            <button onClick={handleReset} className="text-[11px] text-gray-500 hover:text-red-300 transition-colors underline-offset-2 hover:underline">
              {t("ready.cancel")}
            </button>
          </div>
        </div>
      );
    }
    // No batch in flight → redirect home (e.g. user refreshed during /review).
    return <Navigate to="/dashboard" replace />;
  })();

  const generatingScreen = jobs.length > 0
    ? (
      <div className="flex justify-center">
        <BatchProgress
          jobs={jobs}
          onReset={handleReset}
          onSingleDone={handleSelectJob}
          onSelectJob={handleSelectJob}
          onBulkApprove={handleBulkApproveBatch}
        />
      </div>
    )
    : <Navigate to="/dashboard" replace />;

  return (
    <>
      <RootEffects setUser={setUser} setResetToken={setResetToken} />
      <Routes>
        <Route
          path="/"
          element={
            token
              ? <Navigate to="/dashboard" replace />
              : <Landing
                  onStart={() => navigate("/login")}
                  onLogin={() => navigate("/login")}
                  isLoggedIn={false}
                />
          }
        />
        <Route
          path="/login"
          element={
            token
              ? <Navigate to="/dashboard" replace />
              : <LoginPage
                  onLogin={(t, u) => { handleLogin(t, u); navigate("/dashboard"); }}
                  onBack={() => navigate("/")}
                  resetToken={resetToken}
                  onResetComplete={() => setResetToken(null)}
                />
          }
        />
        <Route
          element={
            <RequireAuth token={token}>
              <AppShell
                user={user}
                sidebarOpen={sidebarOpen}
                setSidebarOpen={setSidebarOpen}
                onLogout={handleLogout}
              />
            </RequireAuth>
          }
        >
          <Route path="/dashboard" element={
            <Dashboard
              user={user}
              history={history}
              historyError={historyError}
              historyLoaded={historyLoaded}
              onRetryHistory={fetchHistory}
              onSelectJob={handleSelectJob}
              onNewBatch={() => { setFiles([]); navigate("/new"); }}
              onViewHistory={() => navigate("/videos")}
            />
          } />
          <Route path="/new" element={newBatchScreen} />
          <Route path="/review" element={reviewScreen} />
          <Route path="/generating" element={generatingScreen} />
          <Route path="/videos" element={
            <HistoryView
              history={history}
              historyError={historyError}
              historyLoaded={historyLoaded}
              onRetryHistory={fetchHistory}
              onSelect={handleSelectJob}
              onDelete={handleDeleteJob}
              onBulkDelete={handleBulkDeleteJobs}
              onBack={() => navigate("/dashboard")}
            />
          } />
          <Route path="/videos/:id" element={<JobDetailRoute fetchHistory={fetchHistory} />} />
          {/* Legacy redirects from earlier route names so any cached
              link, browser-history entry, or sidebar tour state still
              lands in the right place. */}
          <Route path="/history" element={<Navigate to="/videos" replace />} />
          <Route path="/v/:id" element={<LegacyVideoRedirect />} />
          <Route path="/staff" element={<Navigate to="/admin" replace />} />
          <Route path="/settings" element={<Navigate to="/account" replace />} />
          <Route path="/account" element={<Settings onBack={() => navigate("/dashboard")} />} />
          <Route path="/admin" element={
            user?.role === "admin"
              ? <AdminPanel onBack={() => navigate("/dashboard")} />
              : <Navigate to="/dashboard" replace />
          } />
        </Route>
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </>
  );
}
