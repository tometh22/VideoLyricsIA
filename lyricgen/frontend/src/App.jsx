import { useState, useRef, useCallback, useEffect } from "react";
import {
  Routes, Route, Navigate, Outlet,
  useNavigate, useLocation, useParams,
} from "react-router-dom";
import { useI18n } from "./i18n";
import { IS_PRODUCTION, APP_ENV } from "./env";
import { fetchWithTimeout } from "./fetchWithTimeout";
import { uploadFileToR2 } from "./r2Upload";
import * as wizardPersistence from "./wizardPersistence";
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
function getTokenExp(token) {
  try {
    return JSON.parse(atob(token.split(".")[1])).exp ?? null;
  } catch {
    return null;
  }
}

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

// authFetch + client-side retry on 503 with Retry-After header. Used for
// endpoints that may transiently saturate (Whisper transcription on burst
// load, where the server retries internally but if it exhausts retries
// it surfaces 503 with Retry-After).
//
// Backend retry handles fast transients (1-30s); this client retry handles
// the rare case where backend exhausts its retries — operator gets
// "Reintentando..." instead of a hard error.
//
// maxRetries=3, max wait 60s per try (cap honors backend's "Retry-After: 60").
async function authFetchWithRetryOn503(url, opts = {}, { maxRetries = 3, onRetry = null } = {}) {
  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    const res = await authFetch(url, opts);
    if (res.status !== 503 || attempt === maxRetries) return res;
    // 503 → check Retry-After (seconds). Cap at 60s to avoid waiting forever.
    let waitS = parseInt(res.headers.get("Retry-After") || "10", 10);
    if (!Number.isFinite(waitS) || waitS <= 0) waitS = 10;
    waitS = Math.min(waitS, 60);
    if (onRetry) onRetry({ attempt: attempt + 1, waitS });
    await new Promise((r) => setTimeout(r, waitS * 1000));
  }
  // Unreachable, but TS-style return for clarity.
  return authFetch(url, opts);
}

// --- Routing helpers ---
function RequireAuth({ token, children }) {
  if (!token) return <Navigate to="/" replace />;
  return children;
}

// Handles one-shot URL-param callbacks (Stripe billing return, email
// verification, password-reset deep links). Mounted once inside the
// router, NOT as a child of <Routes>, so it doesn't remount per nav.
function RootEffects({ setUser, setResetToken, setBillingSuccess }) {
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
      setBillingSuccess(true);
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

// Floating success toast for post-checkout confirmation.
function BillingSuccessToast({ onDismiss }) {
  useEffect(() => {
    const t = setTimeout(onDismiss, 6000);
    return () => clearTimeout(t);
  }, [onDismiss]);

  return (
    <div className="fixed bottom-6 right-6 z-[200] animate-fade-in">
      <div className="flex items-center gap-3 px-5 py-3.5 rounded-2xl bg-[#1a1a24] ring-1 ring-green-500/30 shadow-2xl">
        <div className="w-8 h-8 rounded-full bg-green-500/15 flex items-center justify-center shrink-0">
          <svg className="w-4 h-4 text-green-400" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7"/>
          </svg>
        </div>
        <div>
          <p className="text-sm font-semibold text-white">Plan activado</p>
          <p className="text-xs text-gray-400">Gracias por tu confianza en GenLy AI</p>
        </div>
        <button onClick={onDismiss} className="ml-2 text-gray-500 hover:text-gray-300 transition-colors">
          <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12"/>
          </svg>
        </button>
      </div>
    </div>
  );
}

// Layout shell for authenticated routes. Computes Sidebar's activeView
// from the current pathname so Sidebar.jsx itself doesn't change.
function AppShell({ user, sidebarOpen, setSidebarOpen, onLogout }) {
  const { t } = useI18n();
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const activeView =
    (pathname === "/new" || pathname === "/review" || pathname === "/generating") ? "new" :
    (pathname === "/videos" || pathname.startsWith("/videos/")) ? "history" :
    pathname === "/account" ? "settings" :
    pathname === "/admin" ? "admin" :
    "dashboard";

  const handleNav = (id) => {
    // If the operator is in the middle of a wizard batch (uploaded /
    // transcribed / approved any song) and clicks a sidebar item that
    // moves them off the wizard, ask first. We read directly from the
    // persistence layer (sessionStorage) instead of plumbing state down
    // through props — the persistence useEffect in App keeps the snapshot
    // in sync within one render, and the confirm dialog tolerates that
    // tiny lag.
    const onWizardRoute =
      pathname === "/new" ||
      pathname === "/review" ||
      pathname === "/generating";
    const leavingWizard = onWizardRoute && id !== "new";
    if (leavingWizard
        && wizardPersistence.hasResumableContent(wizardPersistence.load())) {
      const msg =
        t("wizard.confirm_leave") ||
        "Tenés un batch en progreso. Si te vas, podés retomarlo al volver desde el banner amarillo, pero perdés el contexto actual. ¿Continuar?";
      if (!window.confirm(msg)) return;
    }
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

      {sidebarOpen && (
        <div
          className="fixed inset-0 bg-black/50 z-10 md:hidden"
          onClick={() => setSidebarOpen(false)}
        />
      )}

      <div className={`flex-1 min-h-screen transition-all duration-300 ${sidebarOpen ? "md:ml-64" : "md:ml-0"}`}>
        {/* Ambient */}
        <div className="fixed inset-0 pointer-events-none">
          <div className="absolute top-[-30%] left-[20%] w-[600px] h-[600px] bg-brand/[0.03] rounded-full blur-[120px]" />
          <div className="absolute bottom-[-20%] right-[-5%] w-[500px] h-[500px] bg-brand-light/[0.02] rounded-full blur-[100px]" />
        </div>

        {/* Top bar */}
        <header className="sticky top-0 z-20 flex items-center justify-between px-4 md:px-8 py-4 border-b border-white/[0.04] bg-surface/80 backdrop-blur-xl" style={{boxShadow: '0 1px 12px rgba(0,0,0,0.2)'}}>
          <div className="flex items-center gap-3">
            <button
              onClick={() => setSidebarOpen(!sidebarOpen)}
              className={`mr-2 text-gray-400 hover:text-white transition-colors ${sidebarOpen ? "md:hidden" : ""}`}
            >
              <svg className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24"><path d="M4 6h16M4 12h16M4 18h16"/></svg>
            </button>
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
        <main className="relative z-10 px-4 md:px-8 pt-6 md:pt-8 pb-20">
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
        onJobUpdate={(updatedJob) => {
          // fetchHistory() is the expensive call (lists every job in the
          // tenant). It only needs to refresh on a status BOUNDARY —
          // pending_review → editing, editing → pending_review, etc. The
          // /status poll during editing fires every 5s with progress
          // updates only; if we ran fetchHistory on each tick we'd hit
          // /jobs ~150 times during a 13-min edit. Skip those.
          const statusChanged = job?.status !== updatedJob?.status;
          setJob(updatedJob);
          if (statusChanged) fetchHistory();
        }}
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
  const [style, setStyle] = useState("oscuro");

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
  // Stores {queue, idx} of the last failed transcribeNext call so the
  // retry button can re-run it without losing the batch context.
  const transcribeRetryCtx = useRef(null);
  const [history, setHistory] = useState([]);
  const [backgroundFile, setBackgroundFile] = useState(null);
  const [animateImage, setAnimateImage] = useState(false);
  // match_lyrics toggle: when ON (default), Gemini reads the lyrics and
  // builds the background around the song's primary visual subject. OFF
  // falls back to pure genre/concept vocab. UMG 2026-05-14 incident
  // motivation — operator wants a lever to control this per batch.
  const [inspiredByLyrics, setInspiredByLyrics] = useState(true);
  const [backgroundId, setBackgroundId] = useState(null);
  // "as_is" reuses the library asset directly. "variation" tells the
  // backend to extract a frame and run Veo image-to-video to derive a
  // brand-new clip — UMG's path for getting a unique video off a
  // library asset they already used (or want to differentiate from).
  const [backgroundMode, setBackgroundMode] = useState("as_is");
  const [sidebarOpen, setSidebarOpen] = useState(
    typeof window !== "undefined" && window.innerWidth >= 768
  );
  const [resetToken, setResetToken] = useState(null);
  const [billingSuccess, setBillingSuccess] = useState(false);
  const pollingIntervals = useRef(new Set());
  // 2 concurrent workers: enough to keep the queue fed without spiking
  // the API with 5 simultaneous upload-url+generate calls from one user.
  const PARALLEL_WORKERS = 2;

  // ─── Wizard persistence ──────────────────────────────────────────────
  // Snapshot of any pending batch found in sessionStorage at mount time.
  // Drives the resume banner. Cleared when the operator clicks
  // Continuar/Descartar or starts a fresh batch.
  const [resumableWizard, setResumableWizard] = useState(() => {
    const snap = wizardPersistence.load();
    return wizardPersistence.hasResumableContent(snap) ? snap : null;
  });
  // Skip persistence saves while we're actively restoring state — otherwise
  // the useEffect below fires on every setX call from the restore and
  // overwrites the snapshot mid-restore with partial data.
  const restoringRef = useRef(false);

  // Persist every meaningful state change. Debounced via microtask
  // batching: setX calls inside the same handler all trigger one save
  // after React commits. We DON'T persist `jobs` (those are
  // generation-in-progress, already on the server) or wizard control
  // flags like `transcribing`/`transcribeError` (transient, not worth
  // resurrecting).
  useEffect(() => {
    if (restoringRef.current) return;
    const anyState =
      files.length > 0 ||
      approvedJobs.length > 0 ||
      currentReview !== null ||
      reviewQueue.length > 0;
    if (!anyState) {
      // Fresh wizard / cleared explicitly → blow away the snapshot too.
      wizardPersistence.clear();
      return;
    }
    wizardPersistence.save({ files, approvedJobs, currentReview, reviewQueue });
  }, [files, approvedJobs, currentReview, reviewQueue]);

  // beforeunload warning — covers closing the tab, refreshing, or
  // navigating to an external URL. LyricsEditor already has its own
  // "unsaved text edits" warning (lines ~155-161 of LyricsEditor.jsx);
  // this one is broader (any wizard state at all). Returning a string
  // is enough — browsers ignore the message text these days and show
  // their generic "Reload site?" / "Leave site?" prompt.
  useEffect(() => {
    const handler = (e) => {
      const anyState =
        files.length > 0 ||
        approvedJobs.length > 0 ||
        currentReview !== null ||
        reviewQueue.length > 0;
      if (!anyState) return undefined;
      e.preventDefault();
      e.returnValue = "";
      return "";
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [files, approvedJobs, currentReview, reviewQueue]);

  // Imperative resume — called by the banner's "Continuar" button.
  const resumeWizard = useCallback(() => {
    const snap = wizardPersistence.load();
    if (!snap) {
      setResumableWizard(null);
      return;
    }
    restoringRef.current = true;
    try {
      // Restore in the order LyricsEditor / UploadZone read from. Files
      // get rehydrated stubs so existing code that reads `file.name`
      // works; audio playback stays disabled until re-upload but
      // segment editing works fine.
      setFiles((snap.files || []).map(wizardPersistence.rehydrateQueueEntry));
      setReviewQueue((snap.reviewQueue || []).map(wizardPersistence.rehydrateQueueEntry));
      setApprovedJobs((snap.approvedJobs || []).map(wizardPersistence.rehydrateQueueEntry));
      setCurrentReview(wizardPersistence.rehydrateReview(snap.currentReview));
      setResumableWizard(null);
      // If we have a draft in progress → /review. If only approved jobs
      // (came back between songs) → /review too, lets handleBackInReview
      // pop the last one. If only files staged → /new for re-upload.
      if (snap.currentReview || (snap.approvedJobs?.length || 0) > 0) {
        navigate("/review");
      } else {
        navigate("/new");
      }
    } finally {
      // Defer flag flip past the React commit so the persistence useEffect
      // runs once with the FULLY restored state and writes a fresh snapshot.
      setTimeout(() => { restoringRef.current = false; }, 0);
    }
  }, [navigate]);

  const discardResumable = useCallback(() => {
    wizardPersistence.clear();
    setResumableWizard(null);
  }, []);

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

  // reason="expired" → /login so the user can re-authenticate immediately.
  // reason="manual" (default) → / (landing page) for intentional logouts.
  const handleLogout = useCallback((reason = "manual") => {
    // Stop every active poll / SSE stream BEFORE clearing the token.
    pollingIntervals.current.forEach((handle) => {
      if (handle && typeof handle.close === "function") handle.close(); // EventSource
      else clearInterval(handle);
    });
    pollingIntervals.current.clear();
    localStorage.removeItem("genly_token");
    localStorage.removeItem("genly_user");
    setToken(null);
    setUser(null);
    navigate(reason === "expired" ? "/login" : "/");
  }, [navigate]);

  // Sync logout across multiple browser tabs: when genly_token is removed
  // in another tab, log out this tab too so stale sessions don't linger.
  useEffect(() => {
    const onStorage = (e) => {
      if (e.key === "genly_token" && e.newValue === null && token) {
        handleLogout("expired");
      }
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, [token, handleLogout]);

  // Proactively refresh the JWT when it has less than 1 day left, so users
  // with active sessions never hit a sudden 401 mid-session. Runs once per
  // token value (i.e. on load and whenever a fresh token is stored).
  useEffect(() => {
    if (!token) return;
    const exp = getTokenExp(token);
    if (!exp) return;
    const secondsLeft = exp - Math.floor(Date.now() / 1000);
    if (secondsLeft > 86400) return; // more than 1 day left — no action needed
    authFetch(`${API}/auth/refresh`, { method: "POST" })
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (data?.token) {
          localStorage.setItem("genly_token", data.token);
          setToken(data.token);
        }
      })
      .catch(() => {});
  }, [token]);

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
        if (res.status === 401) { handleLogout("expired"); return; }
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
    // Use SSE when available; fall back to 3 s polling for proxies that buffer
    // text/event-stream (some corporate HTTPS interceptors).
    const TERMINAL = new Set(["done", "pending_review", "error", "validation_failed"]);

    return new Promise((resolve) => {
      const token = getToken();
      if (!token) { resolve("aborted"); return; }

      // --- SSE path ---
      let es;
      try {
        // Append the auth token as a query param — EventSource doesn't support
        // custom headers; the backend's get_current_user_from_token_param dep
        // handles token= on GET endpoints.
        es = new EventSource(`${API}/events/${jobId}?token=${encodeURIComponent(token)}`);
      } catch {
        es = null;
      }

      if (es) {
        const cleanup = () => { es.close(); pollingIntervals.current.delete(es); };
        pollingIntervals.current.add(es);
        es.onmessage = (e) => {
          try {
            const data = JSON.parse(e.data);
            setJobs((prev) => prev.map((j) =>
              j.job_id === jobId
                ? { ...j, status: data.status, current_step: data.current_step,
                    progress: data.progress, error: data.error,
                    created_at: data.created_at ?? j.created_at,
                    completed_at: data.completed_at ?? j.completed_at }
                : j
            ));
            if (TERMINAL.has(data.status)) {
              cleanup();
              fetchHistory();
              resolve(data.status);
            }
          } catch {}
        };
        es.onerror = () => {
          // SSE connection dropped (e.g. proxy buffering). Fall through to polling.
          cleanup();
          startPolling();
        };
        return;
      }

      // --- Polling fallback ---
      function startPolling() {
        const iv = setInterval(async () => {
          if (typeof document !== "undefined" && document.hidden) return;
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
              handleLogout("expired");
              resolve("unauthorized");
              return;
            }
            if (!res.ok) return;
            const data = await res.json();
            setJobs((prev) => prev.map((j) =>
              j.job_id === jobId
                ? { ...j, status: data.status, current_step: data.current_step,
                    progress: data.progress, error: data.error,
                    created_at: data.created_at ?? j.created_at,
                    completed_at: data.completed_at ?? j.completed_at }
                : j
            ));
            if (TERMINAL.has(data.status)) {
              clearInterval(iv);
              pollingIntervals.current.delete(iv);
              fetchHistory();
              resolve(data.status);
            }
          } catch {}
        }, 3000);
        pollingIntervals.current.add(iv);
      }
      startPolling();
    });
  }, [fetchHistory, handleLogout]);

  useEffect(() => () => {
    pollingIntervals.current.forEach((handle) => {
      if (handle && typeof handle.close === "function") handle.close();
      else clearInterval(handle);
    });
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
        const res = await authFetchWithRetryOn503(`${API}/transcribe-uploaded`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            job_id: jobId,
            language: entry.language || "",
            artist: entry.artist || "",
            title: (entry.songTitle || "").trim(),
          }),
        }, { maxRetries: 3 });
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
        textContrast: entry.textContrast || "medium",
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
      transcribeRes = await authFetchWithRetryOn503(`${API}/transcribe-uploaded`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          job_id: uploadJobId,
          language: entry.language || "",
          artist: entry.artist || "",
          title: (entry.songTitle || "").trim(),
        }),
      }, {
        maxRetries: 3,
        onRetry: ({ attempt, waitS }) => {
          // Surface to UI so the operator sees we're retrying, not stuck.
          setTranscribeProgress({
            phase: "transcribing",
            loaded: 0,
            total: 0,
            retryAttempt: attempt,
            retryWaitS: waitS,
          });
        },
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
        textContrast: entry.textContrast || "medium",
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
      // err.response carries the actual HTTP response when uploadFileToR2
      // (or apiPost inside it) threw — transcribeRes is null in that case.
      const reason = await describeFetchError(err, transcribeRes ?? err?.response ?? null, t);
      transcribeRetryCtx.current = { queue, idx };
      setTranscribeError(reason);
    }
  };

  // Autosave segments to the backend while the user is editing a lyric.
  // Two reasons:
  //   1. Reaper anchor — POST /jobs/{id}/save-segments bumps
  //      last_user_activity_at, so a 90-min batch-edit session won't get
  //      reaped at the 30-min mark (incident 2026-05-14, Agus, 5 jobs
  //      deleted mid-batch).
  //   2. Cross-device recovery — segments live in the DB, not just in
  //      sessionStorage, so if the tab dies we don't lose corrections.
  // Errors are swallowed: this is a best-effort autosave, the real
  // commit still happens at POST /generate.
  const persistSegmentsToBackend = useCallback(async (jobId, segments) => {
    if (!jobId || !Array.isArray(segments) || segments.length === 0) return;
    try {
      const res = await authFetch(`${API}/jobs/${jobId}/save-segments`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ segments }),
      });
      if (!res.ok && res.status !== 404) {
        // 404 means the job was already reaped — nothing to save against.
        // We log it as a soft warning; the user will see the real error
        // when they click "Crear videos" and /generate returns 404.
        console.warn("[autosave] /save-segments failed", res.status);
      }
    } catch (err) {
      console.warn("[autosave] /save-segments network error", err);
    }
  }, []);

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
      textContrast: r.textContrast || "medium",
      segments: editedSegments,
      transcribeJobId: r.transcribeJobId || null,
    }];
    setApprovedJobs(newApproved);
    setCurrentReview(null);

    // Fire-and-forget commit of the just-approved segments to the backend.
    // Bumps last_user_activity_at and persists segments_json so the reaper
    // won't barre the job before the operator hits "Crear videos" on the
    // next song. See persistSegmentsToBackend comment for context.
    if (r.transcribeJobId) {
      persistSegmentsToBackend(r.transcribeJobId, editedSegments);
    }

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
      textContrast: a.textContrast || "medium",
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
        formData.append("text_contrast", jobList[i].textContrast || "medium");
        if (animateImage && backgroundFile) formData.append("animate_image", "true");
        formData.append("match_lyrics", String(!!inspiredByLyrics));
        formData.append("segments_json", JSON.stringify(jobList[i].segments));
        formData.append("delivery_profile", delivery.delivery_profile);
        if (delivery.delivery_profile !== "youtube") {
          formData.append("umg_frame_size", delivery.umg_frame_size);
          formData.append("umg_fps", String(delivery.umg_fps));
          formData.append("umg_prores_profile", String(delivery.umg_prores_profile));
        }
        if (backgroundId) {
          formData.append("background_id", backgroundId);
          formData.append("background_mode", backgroundMode);
        } else if (backgroundFile) formData.append("background_file", backgroundFile);

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
            // 404 here means the transcribed job was reaped before we got
            // to /generate. Surface a clear message instead of the raw
            // "Job not found." so the operator knows it's a session-expired
            // issue, not a corrupt video.
            const reason = (res.status === 404)
              ? (t("generate.session_expired")
                 || "La sesión expiró antes de generar. Re-subí el audio para regenerar.")
              : (data.detail || await describeFetchError(null, res, t));
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
        generateBody.append("text_contrast", jobList[i].textContrast || "medium");
        if (animateImage && backgroundFile) generateBody.append("animate_image", "true");
        generateBody.append("match_lyrics", String(!!inspiredByLyrics));
        if (backgroundId) {
          generateBody.append("background_id", backgroundId);
          generateBody.append("background_mode", backgroundMode);
        } else if (backgroundFile) generateBody.append("background_file", backgroundFile);

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
            // Same session-expired handling as the legacy /generate path.
            const reason = (genRes.status === 404)
              ? (t("generate.session_expired")
                 || "La sesión expiró antes de generar. Re-subí el audio para regenerar.")
              : (data.detail || await describeFetchError(null, genRes, t));
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
        textContrast: last.textContrast || "medium",
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

  // Resume banner shown on /new and /review when sessionStorage has a
  // pending batch from a prior visit. Lets the operator restore their
  // approved-jobs + current-review (segments included) or drop the
  // snapshot. Hidden once they're actively working again — only meant
  // to bridge the "I navigated away and came back" gap.
  const resumeBanner = resumableWizard
    ? (() => {
        const s = wizardPersistence.summarize(resumableWizard);
        return (
          <div className="mb-6 rounded-card bg-amber-500/[0.08] ring-1 ring-amber-500/30 px-4 py-3 flex items-start gap-3 animate-fade-in">
            <svg className="w-5 h-5 text-amber-400 shrink-0 mt-0.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
              <circle cx="12" cy="12" r="10" />
              <path d="M12 7v5l3 2" strokeLinecap="round" />
            </svg>
            <div className="flex-1 min-w-0">
              <p className="text-sm font-semibold text-white">
                {t("wizard.resume_title") || "Tenés un batch sin terminar"}
              </p>
              <p className="text-xs text-ink-secondary mt-0.5">
                {s.approved > 0 ? `${s.approved} canción${s.approved === 1 ? "" : "es"} aprobada${s.approved === 1 ? "" : "s"}` : "Sin aprobaciones"}
                {s.inProgress > 0 && " · 1 en edición"}
                {s.total > 0 && ` · ${s.total} en el lote`}
                {" · "}hace {s.mins} min
              </p>
              {s.songNames.length > 0 && (
                <p className="text-[11px] text-gray-500 mt-1 truncate">
                  {s.songNames.join(" · ")}{s.songNames.length < s.total ? " · …" : ""}
                </p>
              )}
            </div>
            <div className="flex gap-2 shrink-0">
              <button
                onClick={resumeWizard}
                className="btn-primary text-xs h-9 px-3"
              >
                {t("wizard.resume_continue") || "Continuar"}
              </button>
              <button
                onClick={discardResumable}
                className="text-xs h-9 px-3 rounded-lg text-gray-400 hover:text-white hover:bg-white/[0.04] ring-1 ring-white/[0.06]"
              >
                {t("wizard.resume_discard") || "Descartar"}
              </button>
            </div>
          </div>
        );
      })()
    : null;

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

      {resumeBanner}

      <UploadZone
        files={files}
        onFiles={setFiles}
        delivery={delivery}
        onDeliveryChange={setDelivery}
        style={style}
        onStyleChange={setStyle}
        backgroundFile={backgroundFile}
        onBackgroundFile={setBackgroundFile}
        backgroundId={backgroundId}
        onBackgroundId={setBackgroundId}
        backgroundMode={backgroundMode}
        onBackgroundMode={setBackgroundMode}
        animateImage={animateImage}
        onAnimateImage={setAnimateImage}
        inspiredByLyrics={inspiredByLyrics}
        onInspiredByLyricsChange={setInspiredByLyrics}
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
            <div className="mt-3 flex items-center justify-center gap-4">
              {transcribeRetryCtx.current && (
                <button
                  onClick={() => {
                    const ctx = transcribeRetryCtx.current;
                    setTranscribeError(null);
                    transcribeRetryCtx.current = null;
                    transcribeNext(ctx.queue, ctx.idx);
                  }}
                  className="text-xs text-brand hover:text-brand-light transition-colors font-medium"
                >
                  {t("upload.retry") || "Reintentar"}
                </button>
              )}
              <button onClick={() => { setTranscribeError(null); navigate("/new"); }}
                className="text-xs text-gray-400 hover:text-white transition-colors underline">
                {t("detail.back")}
              </button>
            </div>
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
            transcribeJobId={currentReview.transcribeJobId || null}
            onPersistSegments={persistSegmentsToBackend}
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
            textContrast={currentReview.textContrast || "medium"}
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
      <RootEffects setUser={setUser} setResetToken={setResetToken} setBillingSuccess={setBillingSuccess} />
      {billingSuccess && <BillingSuccessToast onDismiss={() => setBillingSuccess(false)} />}
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
              onNewBatch={() => {
                // Guard the "Nuevo batch" CTA — clicking it while a
                // batch is in progress used to silently wipe everything
                // (setFiles([]) + navigate). Confirm first, then clear
                // both in-memory state AND the persisted snapshot so
                // the resume banner doesn't immediately reappear.
                const hasState =
                  files.length > 0 ||
                  approvedJobs.length > 0 ||
                  currentReview !== null ||
                  reviewQueue.length > 0;
                if (hasState) {
                  const msg =
                    t("wizard.confirm_discard_batch") ||
                    "Vas a empezar un batch nuevo y perdés el progreso actual (lyrics corregidas, canciones aprobadas). ¿Seguro?";
                  if (!window.confirm(msg)) return;
                }
                setFiles([]);
                setApprovedJobs([]);
                setCurrentReview(null);
                setReviewQueue([]);
                wizardPersistence.clear();
                navigate("/new");
              }}
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
