import { useState, useRef, useCallback, useEffect } from "react";
import {
  Routes, Route, Navigate, Outlet,
  useNavigate, useLocation, useParams,
} from "react-router-dom";
import { useI18n } from "./i18n";
import { IS_PRODUCTION, APP_ENV } from "./env";
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
  const [readyToGenerate, setReadyToGenerate] = useState(false);

  const [jobs, setJobs] = useState([]);
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
    localStorage.removeItem("genly_token");
    localStorage.removeItem("genly_user");
    setToken(null);
    setUser(null);
    navigate("/");
  }, [navigate]);

  const fetchHistory = useCallback(async () => {
    if (!getToken()) return;
    try {
      const res = await authFetch(`${API}/jobs`);
      if (res.status === 401) { handleLogout(); return; }
      const data = await res.json();
      if (Array.isArray(data)) setHistory(data);
    } catch {}
  }, [handleLogout]);

  useEffect(() => { if (token) fetchHistory(); }, [token, fetchHistory]);

  const pollJob = useCallback((jobId) => {
    // Poll every 3 s (instead of 1 s) and skip the tick entirely when the tab
    // is hidden. For a user with a few tabs open and 20 active jobs this cuts
    // the request rate by ~90%.
    return new Promise((resolve) => {
      const iv = setInterval(async () => {
        if (typeof document !== "undefined" && document.hidden) return;
        try {
          const data = await (await authFetch(`${API}/status/${jobId}`)).json();
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

  // --- Review flow ---
  const handleStartReview = async () => {
    if (!files.length || !files.every((f) => f.artist.trim())) return;
    setReviewQueue([...files]);
    navigate("/review");
    transcribeNext([...files], 0);
  };

  const handleGenerateDirect = () => {
    if (!files.length || !files.every((f) => f.artist.trim())) return;
    const jobList = files.map((f) => ({
      filename: f.file.name, _file: f.file, artist: f.artist.trim(),
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
    setTranscribing(true);
    setTranscribeError(null);

    const formData = new FormData();
    formData.append("file", entry.file);
    if (entry.language) formData.append("language", entry.language);
    // Forward the artist (and a derived title) so the backend's reference-
    // lyrics fetcher (Gemini-grounded search) gets clean inputs even when
    // the MP3 filename is something generic like "track.mp3".
    if (entry.artist) formData.append("artist", entry.artist);
    const _title = entry.file.name
      .replace(/\.mp3$/i, "")
      .replace(/^.*?\s-\s/, "")
      .trim();
    if (_title) formData.append("title", _title);

    try {
      const res = await authFetch(`${API}/transcribe`, { method: "POST", body: formData });
      if (!res.ok) throw new Error(`Server error ${res.status}`);
      const text = await res.text();
      if (!text) throw new Error("Empty response");
      const data = JSON.parse(text);
      setTranscribing(false);
      setCurrentReview({
        file: entry.file, artist: entry.artist, language: entry.language,
        genre: entry.genre || "", font: entry.font || "",
        concept: entry.concept || "", movementStyle: entry.movementStyle || "",
        segments: data.segments, referenceLyrics: data.reference_lyrics || "",
        coverageWarning: !!data.coverage_warning,
        recoverySource: data.recovery_source || "",
        queueIdx: idx, queue,
      });
    } catch (err) {
      setTranscribing(false);
      setTranscribeError(t("batch.error_server"));
    }
  };

  const handleApproveLyrics = (editedSegments) => {
    const r = currentReview;
    const newApproved = [...approvedJobs, {
      file: r.file, artist: r.artist, language: r.language,
      genre: r.genre || "", font: r.font || "", concept: r.concept || "",
      movementStyle: r.movementStyle || "",
      segments: editedSegments,
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
      language: a.language, genre: a.genre || "", font: a.font || "",
      concept: a.concept || "", movementStyle: a.movementStyle || "",
      segments: a.segments,
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
        formData.append("file", jobList[i]._file);
        formData.append("artist", jobList[i].artist);
        formData.append("style", style);
        if (jobList[i].language) formData.append("language", jobList[i].language);
        if (jobList[i].genre) formData.append("genre", jobList[i].genre);
        if (jobList[i].font) formData.append("font", jobList[i].font);
        if (jobList[i].concept) formData.append("concept", jobList[i].concept);
        if (jobList[i].movementStyle) formData.append("movement_style", jobList[i].movementStyle);
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

        try {
          const res = await authFetch(`${API}/generate`, { method: "POST", body: formData });
          const data = await res.json();
          if (data.detail) {
            // Plan limit error
            setJobs((prev) => prev.map((j, idx) =>
              idx === i ? { ...j, status: "error", error: data.detail } : j
            ));
            continue;
          }
          setJobs((prev) => prev.map((j, idx) => (idx === i ? { ...j, job_id: data.job_id } : j)));
          await pollJob(data.job_id);
        } catch {
          setJobs((prev) => prev.map((j, idx) =>
            idx === i ? { ...j, status: "error", error: t("batch.error_server") } : j
          ));
        }
      }
    };
    await Promise.all(Array.from({ length: Math.min(PARALLEL_WORKERS, jobList.length) }, () => worker()));
  };

  const processQueueDirect = async (jobList) => {
    let nextIdx = 0;
    const worker = async () => {
      while (nextIdx < jobList.length) {
        const i = nextIdx++;
        setJobs((prev) => prev.map((j, idx) =>
          idx === i ? { ...j, status: "processing", current_step: "whisper", progress: 0 } : j
        ));
        const formData = new FormData();
        formData.append("file", jobList[i]._file);
        formData.append("artist", jobList[i].artist);
        formData.append("style", style);
        formData.append("delivery_profile", delivery.delivery_profile);
        if (delivery.delivery_profile !== "youtube") {
          formData.append("umg_frame_size", delivery.umg_frame_size);
          formData.append("umg_fps", String(delivery.umg_fps));
          formData.append("umg_prores_profile", String(delivery.umg_prores_profile));
        }
        if (jobList[i].language) formData.append("language", jobList[i].language);
        if (jobList[i].genre) formData.append("genre", jobList[i].genre);
        if (jobList[i].font) formData.append("font", jobList[i].font);
        if (jobList[i].concept) formData.append("concept", jobList[i].concept);
        if (jobList[i].movementStyle) formData.append("movement_style", jobList[i].movementStyle);
        if (animateImage && backgroundFile) formData.append("animate_image", "true");
        if (backgroundId) formData.append("background_id", backgroundId);
        else if (backgroundFile) formData.append("background_file", backgroundFile);

        // Retry-on-429 with exponential backoff. Batch uploads of 30+ files can
        // briefly exceed the per-user rate limit; 429s are transient, not failures.
        // EXCEPTION: a 429 with "batch limit" is structural — the user has too
        // many jobs in flight. Retrying won't help until previous jobs finish;
        // surface the error directly instead of looping.
        let res = null;
        let data = null;
        let attempt = 0;
        const MAX_429_RETRIES = 5;
        let networkError = false;
        let batchLimitHit = false;
        while (attempt <= MAX_429_RETRIES) {
          try {
            res = await authFetch(`${API}/upload`, { method: "POST", body: formData });
          } catch {
            networkError = true;
            break;
          }
          if (res.status !== 429) {
            data = await res.json();
            break;
          }
          // Peek at the body to distinguish rate-limit-burst from batch-limit.
          // A batch-limit 429 contains "batch limit" in the detail; rate-limit
          // 429s contain "Rate limit exceeded".
          let body = null;
          try { body = await res.clone().json(); } catch { body = null; }
          if (body && body.detail && /batch limit/i.test(body.detail)) {
            data = body;
            batchLimitHit = true;
            break;
          }
          // 429: wait 2^attempt seconds (2, 4, 8, 16, 32) then retry.
          const waitMs = Math.min(32000, 2000 * Math.pow(2, attempt));
          setJobs((prev) => prev.map((j, idx) =>
            idx === i ? { ...j, status: "queued", error: null } : j
          ));
          await new Promise((r) => setTimeout(r, waitMs));
          attempt++;
        }
        if (networkError || !res || !data) {
          setJobs((prev) => prev.map((j, idx) =>
            idx === i ? { ...j, status: "error", error: t("batch.error_server") } : j
          ));
          continue;
        }
        if (data.detail) {
          setJobs((prev) => prev.map((j, idx) =>
            idx === i ? { ...j, status: "error", error: data.detail } : j
          ));
          continue;
        }
        setJobs((prev) => prev.map((j, idx) => (idx === i ? { ...j, job_id: data.job_id } : j)));
        await pollJob(data.job_id);
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
      return (
        <div className="w-full max-w-md mx-auto mt-16 animate-fade-in text-center">
          <div className="w-12 h-12 mx-auto mb-4 border-2 border-brand border-t-transparent rounded-full animate-spin" />
          <h2 className="text-xl font-bold mb-2">{t("transcribe.title")}</h2>
          <p className="text-gray-500 text-sm">{t("transcribe.subtitle")}</p>
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
