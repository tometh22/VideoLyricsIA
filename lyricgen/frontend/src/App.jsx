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
import SearchPalette from "./components/SearchPalette";
import UploadZone from "./components/UploadZone";
import LyricsEditor from "./components/LyricsEditor";
import BatchProgress from "./components/BatchProgress";
import TranscribingProgress from "./components/TranscribingProgress";
import JobDetail from "./components/JobDetail";
import Settings from "./components/Settings";
import AdminPanel from "./components/AdminPanel";
import { useAlert } from "./components/AlertProvider";
import { useBackgroundPreview } from "./hooks/useBackgroundPreview";
import { useMediaUrl } from "./mediaUrl";
import { submitLyricsEdit } from "./lib/lyricsEditSubmit";

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

// Deep-link adapter for /videos/:id/edit-lyrics. Bootstrappea
// currentReview con los datos del job (segments, render_params, URLs
// firmadas de audio/waveform/background) y renderiza el mismo
// wizardScreen del flow nuevo — el operador edita lyrics post-render
// dentro del Studio Console en vez de un modal separado con UX distinta.
// Pasos 1, 2, 3, 5 quedan lockeados desde App (vía currentReview.
// editingJobId) y el preview central muestra el MP4 ya renderizado.
function EditLyricsRoute({ setCurrentReview, wizardScreen, t }) {
  const { id } = useParams();
  const navigate = useNavigate();
  // status: "loading" | "ready" | "no_segments" | "not_editable" |
  //         "not_found" | "error". Loading hasta que tanto el job como
  // las URLs firmadas aterricen; ready hace montar el wizardScreen.
  const [state, setState] = useState({ status: "loading" });

  useEffect(() => {
    let alive = true;
    setState({ status: "loading" });

    // Re-hidrate from snapshot when refreshing mid-edit: el operador
    // ya tenía edits in-flight, el snapshot persistido por wizardPersistence
    // los preserva. Las URLs firmadas (audio/bg) sí re-fetchean porque
    // expiran (~5min).
    const snap = wizardPersistence.load();
    const reusableSnap =
      snap?.currentReview?.editingJobId === id &&
      Array.isArray(snap.currentReview.segments) &&
      snap.currentReview.segments.length > 0;

    (async () => {
      try {
        const [statusRes, audioRes, waveformRes, bgRes] = await Promise.all([
          authFetch(`${API}/status/${id}`),
          authFetch(`${API}/jobs/${id}/source-audio-url`),
          authFetch(`${API}/jobs/${id}/waveform`),
          authFetch(`${API}/jobs/${id}/background-url`),
        ]);
        if (!alive) return;

        if (statusRes.status === 404) { setState({ status: "not_found" }); return; }
        if (!statusRes.ok) { setState({ status: "error" }); return; }
        const job = await statusRes.json();

        // Solo pending_review/done/rejected son editables (mismo gating
        // que canEditLyrics en JobDetail). Editing/queued/processing →
        // bail-out con redirect: no tiene sentido abrir el editor sobre
        // un render en curso.
        const editable =
          job.status === "pending_review" ||
          job.status === "done" ||
          job.status === "rejected";
        if (!editable) { setState({ status: "not_editable", jobStatus: job.status }); return; }

        // Sin segments_json no hay nada que editar — mismo banner amber
        // que mostraba el modal anterior, sin redirect silencioso.
        if (!Array.isArray(job.segments_json) || job.segments_json.length === 0) {
          setState({ status: "no_segments" });
          return;
        }

        const audioData = audioRes.ok ? await audioRes.json() : null;
        const waveformData = waveformRes.ok ? await waveformRes.json() : null;
        const bgData = bgRes.ok ? await bgRes.json() : null;
        const params = job.render_params || {};

        // Reuso del snapshot: si el operador refrescó con edits in-flight,
        // los segments del snapshot ganan sobre job.segments_json (que es
        // lo último que el autosave guardó pero podría no incluir el
        // último cambio uncommitted). La baseline para "unchanged" sigue
        // siendo lo que el job tiene RENDERIZADO (job.segments_json).
        const segmentsFromSnap = reusableSnap ? snap.currentReview.segments : job.segments_json;

        setCurrentReview({
          editingJobId: id,
          segments: segmentsFromSnap,
          openSnapshotSegments: JSON.parse(JSON.stringify(job.segments_json)),
          filename: job.filename || job.artist || "lyrics",
          file: null,
          audioUrl: audioData?.url || null,
          waveform: waveformData || null,
          bgUrl: bgData?.url || null,
          font: (reusableSnap && snap.currentReview.font) || params.font || "",
          textCase: (reusableSnap && snap.currentReview.textCase) || params.text_case || "upper",
          textContrast: (reusableSnap && snap.currentReview.textContrast) || params.text_contrast || "medium",
          fontScale: String((reusableSnap && snap.currentReview.fontScale) || params.font_scale || "1.0"),
          lyricsAnimation: (reusableSnap && snap.currentReview.lyricsAnimation) || params.lyrics_animation || "none",
          lineTransition: (reusableSnap && snap.currentReview.lineTransition) || params.line_transition || "none",
          // Empty queue: this isn't a batch, it's a one-off edit.
          queue: [],
          queueIdx: 0,
          transcribeJobId: null,
          referenceLyrics: "",
        });
        setState({ status: "ready" });
      } catch (e) {
        if (alive) setState({ status: "error" });
      }
    })();

    return () => { alive = false; };
    // setCurrentReview is stable via useState; only re-bootstrap on id change.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  // Cleanup en unmount: si el operador navega lejos sin aprobar (back-button,
  // sidebar, etc.), borrar el editingJobId del currentReview para que un
  // siguiente /new arranque limpio y no resuma sobre el edit a medias. Los
  // edits no se pierden — el autosave del LyricsEditor los persiste a
  // /save-segments cada 3s, y la próxima visita a /edit-lyrics los re-fetchea.
  useEffect(() => {
    return () => {
      setCurrentReview((r) => (r?.editingJobId ? null : r));
      wizardPersistence.clear();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (state.status === "loading") {
    return <div className="w-12 h-12 mx-auto mt-16 border-2 border-brand border-t-transparent rounded-full animate-spin" />;
  }
  if (state.status === "not_found") {
    return (
      <div className="text-center mt-16">
        <p className="text-gray-500 mb-4">{t("detail.not_found") || "No se encontró el video."}</p>
        <button onClick={() => navigate("/dashboard")} className="btn-secondary">
          {t("detail.back") || "Volver"}
        </button>
      </div>
    );
  }
  if (state.status === "not_editable") {
    return (
      <div className="text-center mt-16 max-w-md mx-auto px-4">
        <div className="w-14 h-14 mx-auto mb-4 rounded-2xl bg-amber-500/10 flex items-center justify-center">
          <svg className="w-7 h-7 text-amber-400" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
            <circle cx="12" cy="12" r="10" />
            <path d="M12 8v4M12 16h.01" strokeLinecap="round" />
          </svg>
        </div>
        <h2 className="text-xl font-bold mb-2">
          {t("edit.not_editable_title") || "No se puede editar ahora"}
        </h2>
        <p className="text-sm text-gray-500 mb-6">
          {t("edit.not_editable_subtitle") ||
            `Este video está en estado "${state.jobStatus}". Esperá a que termine el render o que pase a revisión.`}
        </p>
        <button onClick={() => navigate(`/videos/${id}`)} className="btn-secondary">
          {t("detail.back") || "Volver al video"}
        </button>
      </div>
    );
  }
  if (state.status === "no_segments") {
    return (
      <div className="text-center mt-16 max-w-md mx-auto px-4">
        <div className="w-14 h-14 mx-auto mb-4 rounded-2xl bg-amber-500/10 flex items-center justify-center">
          <svg className="w-7 h-7 text-amber-400" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
            <circle cx="12" cy="12" r="10" />
            <path d="M12 8v4M12 16h.01" strokeLinecap="round" />
          </svg>
        </div>
        <h2 className="text-xl font-bold mb-2">
          {t("edit.lyrics_no_segments_title") || "Este video no tiene letras guardadas"}
        </h2>
        <p className="text-sm text-gray-500 mb-6">
          {t("edit.lyrics_no_segments") ||
            "Este job no tiene letras guardadas. Esto pasa con jobs muy viejos. Subí la canción de nuevo para editar letras."}
        </p>
        <button onClick={() => navigate(`/videos/${id}`)} className="btn-secondary">
          {t("detail.back") || "Volver al video"}
        </button>
      </div>
    );
  }
  if (state.status === "error") {
    return (
      <div className="text-center mt-16">
        <p className="text-gray-500 mb-4">{t("detail.load_error") || "No pudimos cargar el video."}</p>
        <button onClick={() => navigate(`/videos/${id}`)} className="btn-secondary">
          {t("detail.back") || "Volver"}
        </button>
      </div>
    );
  }
  return wizardScreen;
}

export default function App() {
  const { t } = useI18n();
  const navigate = useNavigate();
  const { alert } = useAlert();

  const [token, setToken] = useState(getToken());
  const [user, setUser] = useState(getUser());
  const [files, setFiles] = useState([]);
  // Ref que espeja `files` para que callbacks sin dependencias (ej.
  // onAutoTranscribe en un setTimeout) lean el estado actual sin re-render
  // loops. Sync con un useEffect debajo.
  const filesRef = useRef(files);
  const [delivery, setDelivery] = useState({
    delivery_profile: "youtube",
    umg_frame_size: "HD",
    umg_fps: 24,
    umg_prores_profile: 3,
  });
  const [style, setStyle] = useState("auto");
  // Custom palette (hex/names, comma-sep) used when style === "custom".
  const [customColors, setCustomColors] = useState("");

  const [reviewQueue, setReviewQueue] = useState([]);
  const [currentReview, setCurrentReview] = useState(null);
  const [approvedJobs, setApprovedJobs] = useState([]);
  const [transcribing, setTranscribing] = useState(false);
  const [transcribeError, setTranscribeError] = useState(null);

  // Phase C 2026-05-25: ref-based playback tick para que el WizardLivePreview
  // central pueda renderizar la línea activa con word-jump real (sincronizado
  // al audio) SIN causar re-renders del tree de UploadZone a 60fps. El ref
  // se actualiza desde el rAF loop de LyricsEditor; WizardLivePreview lo
  // lee con su propio rAF.
  const playbackTickRef = useRef({ activeLine: "", activeStart: 0, activeEnd: 0, currentTime: 0 });
  const handlePlaybackTick = useCallback((line, start, end, time) => {
    playbackTickRef.current = { activeLine: line, activeStart: start, activeEnd: end, currentTime: time };
  }, []);

  // Phase 2 (2026-05-25): sync de typography settings cuando el operador
  // cambia font/case/animation desde el paso 4 del wizard MIENTRAS está
  // en review (paso 6 inactivo). updateBatchDefault en UploadZone fanea
  // a files[*] pero NO toca currentReview — sin este effect, el editor
  // se queda con la font vieja al volver a paso 6.
  useEffect(() => {
    if (!currentReview) return;
    const match = files.find(
      (f) => f?.file?.name === currentReview.file?.name,
    );
    if (!match) return;
    // Audit fix 2026-05-25: extender los fields que sincronizan. Antes
    // sólo cubría typography (font/case/scale/contrast/animation/transition).
    // Si el operador volvía al paso 3 a cambiar movementStyle/effect/
    // concept/genre/backgroundHint/bgVerbatim, esos cambios NO llegaban a
    // currentReview → el video se generaba con la elección STALE de cuando
    // se inició el transcribe. Crítico para UMG: si cambian movement durante
    // review, el render usa el viejo.
    const fields = [
      "font", "textCase", "fontScale", "textContrast",
      "lyricsAnimation", "lineTransition",
      "lyricColor", "lyricSungColor",
      "movementStyle", "effect", "concept", "genre",
      "backgroundHint", "bgVerbatim",
    ];
    const drift = fields.some((k) => (match[k] ?? "") !== (currentReview[k] ?? ""));
    if (!drift) return;
    setCurrentReview((r) => {
      if (!r) return r;
      const next = { ...r };
      for (const k of fields) {
        if (match[k] !== undefined) next[k] = match[k];
      }
      return next;
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [files, currentReview?.file?.name]);
  // Capa B 2026-05-24 — wizardStage es la única fuente de verdad de qué muestra
  // el wizard. Reemplaza el `navigate("/review")` que disparaba el flash a
  // dashboard. URL se queda en /new mientras el operador transita upload →
  // review → ready_to_generate. La navegación a /generating sigue siendo
  // legítima (pantalla dedicada de progreso del batch). Valores:
  //   "upload"            → UploadZone (drop archivos + opciones).
  //   "review"            → spinner de transcribiendo / LyricsEditor inline.
  //   "ready_to_generate" → resumen + botón "Crear N videos".
  const [wizardStage, setWizardStage] = useState("upload");
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
  // 2026-05-25 PR-2 — Command palette ⌘K. Estado global así el listener
  // de teclado funciona desde cualquier ruta (Dashboard/Historial/Editor).
  const [searchOpen, setSearchOpen] = useState(false);
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
  // R-FRONT-5 (Frontend specialist 2026-05-24): isMountedRef previene
  // setState-on-unmounted warnings + memory leaks cuando el operador
  // navega away durante un SSE/polling en curso. Cada callback async
  // chequea esto antes de tocar state.
  const isMountedRef = useRef(true);
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
    // Capa B 2026-05-24: persist wizardStage para que un refresh durante
    // review NO te tire de vuelta al state "upload". El snap.load() lo
    // rehidrata en el useEffect de mount.
    // Audit fix 2026-05-25: agregamos TODO el state top-level que faltaba
    // (delivery → delivery_profile UMG, style/customColors/etc.) para que
    // un refresh durante un batch UMG no caiga silently a youtube.
    wizardPersistence.save({
      files, approvedJobs, currentReview, reviewQueue, wizardStage,
      style, customColors, delivery, backgroundId, backgroundMode,
      animateImage, inspiredByLyrics,
    });
  }, [
    files, approvedJobs, currentReview, reviewQueue, wizardStage,
    style, customColors, delivery, backgroundId, backgroundMode,
    animateImage, inspiredByLyrics,
  ]);

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

  // 2026-05-25 — Resume desde el historial. JobDetail enlaza a
  // /new?resume=<jobId> cuando el operador clickea "Editar lyrics y
  // generar" sobre una card en estado `transcribed`. Sin handler, el
  // wizard caía en pantalla de upload (bug reportado durante UMG
  // dry-run: "los Sin generar cuando abrís te devuelve a Crear el
  // video"). Implementación: fetch del job + segments + audio URL,
  // construir currentReview SIN File (lo seteamos `null` y pasamos
  // `audioUrl` al LyricsEditor que ya acepta el prop), setear
  // wizardStage="review". El approve flow ya soporta retomar via
  // `transcribeJobId` — el backend skipea file upload y reusa R2.
  const resumeJobAttemptedRef = useRef(false);
  useEffect(() => {
    const params = new URLSearchParams(location.search);
    const resumeJobId = params.get("resume");
    if (!resumeJobId) return;
    if (resumeJobAttemptedRef.current) return;
    resumeJobAttemptedRef.current = true;

    let cancelled = false;
    (async () => {
      try {
        const statusRes = await authFetch(`${API}/status/${resumeJobId}`);
        if (!statusRes.ok) throw new Error(`status ${statusRes.status}`);
        const job = await statusRes.json();
        if (cancelled) return;
        // El audio URL se pide a un endpoint dedicado (presigned R2 o
        // streaming via /media-token). LyricsEditor lo carga en su
        // <audio> sin necesitar el File en memoria.
        let audioUrl = null;
        try {
          const audioRes = await authFetch(`${API}/jobs/${resumeJobId}/source-audio-url`);
          if (audioRes.ok) {
            const audioJson = await audioRes.json();
            audioUrl = audioJson.url || audioJson.audio_url || null;
          }
        } catch (_) { /* audioUrl queda null — editor de texto funciona igual */ }

        const segments = job.segments || job.segments_json || [];
        setCurrentReview({
          file: null,                            // no tenemos el File original
          filename: job.filename || `${job.song_title || job.artist || "audio"}.wav`,
          audioUrl,                              // LyricsEditor lo acepta directamente
          artist: job.artist || "",
          songTitle: job.song_title || "",
          language: job.language || "es",
          genre: job.genre || "",
          font: job.font || "",
          concept: job.concept || "",
          movementStyle: job.movement_style || "",
          effect: job.effect || "",
          textCase: job.text_case || "upper",
          fontScale: String(job.font_scale || "1.0"),
          lyricsAnimation: job.lyrics_animation || "none",
          lineTransition: job.line_transition || "none",
          lyricColor: job.lyric_color || "#FFFFFF",
          lyricSungColor: job.lyric_sung_color || "#FFFFFF",
          textContrast: job.text_contrast || "medium",
          segments,
          referenceLyrics: job.reference_lyrics || "",
          coverageWarning: !!job.coverage_warning,
          recoverySource: job.recovery_source || "",
          transcribeJobId: resumeJobId,           // backend reusa R2 audio
          queueIdx: 0,
          queue: [{ filename: job.filename || "audio.wav" }],
        });
        setWizardStage("review");
        // Limpiar el query param sin agregar a history (replace).
        navigate("/new", { replace: true });
      } catch (err) {
        console.warn("[RESUME] no pude cargar el job:", err);
        resumeJobAttemptedRef.current = false;   // permitir reintento si el operador cambia URL
        // Fallback honesto: si el resume falla (auth no lista, red, 4xx),
        // mandar al JobDetail en vez de dejar al usuario varado en /new
        // con el wizard vacío — que parece "crear video nuevo".
        navigate(`/videos/${resumeJobId}`, { replace: true });
      }
    })();
    return () => { cancelled = true; };
  }, [location.search, navigate]);

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
      // Audit fix 2026-05-25: restaurar state top-level (delivery / style /
      // backgroundMode / etc.) que ANTES se perdía silently. Lo más crítico
      // para UMG: delivery_profile/umg_frame_size/umg_fps/umg_prores_profile
      // — sin esto un refresh durante batch UMG cae a youtube y se rendea
      // sin ProRes master.
      if (snap.topLevel) {
        if (snap.topLevel.style != null) setStyle(snap.topLevel.style);
        if (snap.topLevel.customColors != null) setCustomColors(snap.topLevel.customColors);
        if (snap.topLevel.delivery) setDelivery(snap.topLevel.delivery);
        if (snap.topLevel.backgroundId != null) setBackgroundId(snap.topLevel.backgroundId);
        if (snap.topLevel.backgroundMode != null) setBackgroundMode(snap.topLevel.backgroundMode);
        if (typeof snap.topLevel.animateImage === "boolean") setAnimateImage(snap.topLevel.animateImage);
        if (typeof snap.topLevel.inspiredByLyrics === "boolean") setInspiredByLyrics(snap.topLevel.inspiredByLyrics);
      }
      // Capa B 2026-05-24: restaurar wizardStage para que /new renderice
      // el reviewScreen content si el operador estaba mid-review al refresh.
      // Default "upload" si el snap es viejo (sin wizardStage) o si no hay
      // currentReview/approved (sólo files staged).
      const resumedStage = snap.wizardStage
        || ((snap.currentReview || (snap.approvedJobs?.length || 0) > 0) ? "review" : "upload");
      setWizardStage(resumedStage);
      setResumableWizard(null);
      // Capa B: una sola ruta destino — /new — con wizardStage indicando
      // qué content mostrar inline. Antes navegábamos a /review cuando había
      // currentReview/approved, ahora /new lo hace todo via wizardScreen.
      navigate("/new");
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
  //
  // INCIDENT (audit 2026-05-24): the previous code had two silent-failure
  // modes:
  //   (1) An already-expired token (secondsLeft < 0) bypassed the
  //       `> 86400` early-return (negative is < 86400 → falls through
  //       to refresh), but if `/auth/refresh` then 401'd, the bare
  //       `.catch(() => {})` swallowed it. The user kept typing into a
  //       dead session — every autosave 401'd silently, every "Generar"
  //       click failed with no clear cause.
  //   (2) Same shape on a network failure: refresh fails, no logout, no
  //       toast, user stranded.
  //
  // Fix: if the token is already expired OR refresh fails (any reason),
  // force a clean logout so the login screen renders and the user knows
  // what to do. Network blips during refresh-while-still-valid still get
  // a silent retry (the existing 401 interceptors handle the in-flight
  // requests).
  useEffect(() => {
    if (!token) return;
    const exp = getTokenExp(token);
    if (!exp) return;
    const secondsLeft = exp - Math.floor(Date.now() / 1000);
    const alreadyExpired = secondsLeft <= 0;
    if (!alreadyExpired && secondsLeft > 86400) return;
    authFetch(`${API}/auth/refresh`, { method: "POST" })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`refresh ${r.status}`))))
      .then((data) => {
        if (data?.token) {
          localStorage.setItem("genly_token", data.token);
          setToken(data.token);
        } else {
          throw new Error("refresh response missing token");
        }
      })
      .catch((err) => {
        if (alreadyExpired) {
          // Hard logout — the session is unrecoverable.
          console.warn("[auth] token expired and refresh failed — logging out:", err?.message);
          handleLogout();
        } else {
          // Token still valid for now; log the failure so it shows up in
          // devtools but don't disrupt the session.
          console.warn("[auth] preemptive refresh failed (will retry on next mount):", err?.message);
        }
      });
  }, [token, handleLogout]);

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
          if (!isMountedRef.current) { cleanup(); return; }
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
              if (isMountedRef.current) fetchHistory();
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
          if (!isMountedRef.current) {
            clearInterval(iv);
            pollingIntervals.current.delete(iv);
            resolve("aborted");
            return;
          }
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
            if (!isMountedRef.current) {
              clearInterval(iv);
              pollingIntervals.current.delete(iv);
              resolve("aborted");
              return;
            }
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
              if (isMountedRef.current) fetchHistory();
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
    // R-FRONT-5: marca unmounted ANTES de cerrar handles para que
    // cualquier callback async en flight (SSE messages bufferadas, polls
    // ya disparados) salga temprano vía el guard sin tocar state.
    isMountedRef.current = false;
    pollingIntervals.current.forEach((handle) => {
      if (handle && typeof handle.close === "function") handle.close();
      else clearInterval(handle);
    });
  }, []);

  // Sync filesRef con files para que callbacks asincrónicos vean el state actual.
  useEffect(() => { filesRef.current = files; }, [files]);

  // Estado per-row visible en UploadZone — { [stableKey]: "uploading" | "queued" |
  // "transcribing" | "done" | "error" }. Stable key = file.name + file.lastModified.
  // Sirve para mostrar el status badge en cada fila del wizard mientras la
  // transcripción corre en background (2026-05-23 refactor).
  const [transcribeStatusByFile, setTranscribeStatusByFile] = useState({});
  const fileKey = (f) => `${f.name}__${f.lastModified}__${f.size}`;
  const setRowStatus = (file, status, extra = {}) => {
    const k = fileKey(file);
    setTranscribeStatusByFile((prev) => ({ ...prev, [k]: { status, ...extra } }));
  };

  // Polls /transcription-status hasta que el job terminó. Devuelve los datos
  // con segments + reference_lyrics, o null si falló. Backoff 1.5s → 5s.
  // 2026-05-23: necesario por el nuevo backend async que devuelve 202+job_id
  // al POST /transcribe-uploaded en vez de los segments inline.
  const pollUntilTranscribed = useCallback(async (jobId, file) => {
    let delay = 1500;
    const start = Date.now();
    // INCIDENT 2026-05-24: previous TIMEOUT_MS was 5 min "igual que
    // job_timeout backend". PR #295 raised the backend RQ timeout to
    // 30 min because the post-PR-G pipeline (demucs + FA + whisperX +
    // fallbacks) legitimately takes 8-12 min for long WAVs. The
    // frontend was left at 5 min — users saw "La transcripción falló"
    // even though the backend was still processing successfully (two
    // jobs in DB completed at progress=70 after the frontend already
    // gave up).
    //
    // Bumped to 20 min — covers the legitimate worst case (~12 min)
    // with margin, but bails well before the backend's 30 min hard
    // cap so we still distinguish "stuck" from "legitimate slow".
    const TIMEOUT_MS = 20 * 60 * 1000;   // 20 min — was 5 min, see above
    while (Date.now() - start < TIMEOUT_MS) {
      try {
        const res = await authFetchWithRetryOn503(
          `${API}/transcription-status/${jobId}`,
          { method: "GET" },
          { maxRetries: 2 },
        );
        if (res.ok) {
          const data = await res.json();
          if (data.status === "transcribed") {
            if (file) setRowStatus(file, "done");
            return data;
          }
          if (data.status === "transcription_failed") {
            if (file) setRowStatus(file, "error", { error: data.error });
            return null;
          }
          if (file) setRowStatus(file, data.status === "transcribing_queued" ? "queued" : "transcribing", {
            current_step: data.current_step ?? null,
            progress: data.progress ?? null,
          });
        }
      } catch {
        // Transient errors — keep polling.
      }
      await new Promise((r) => setTimeout(r, delay));
      delay = Math.min(delay * 1.2, 5000);
    }
    // 20 min sin respuesta — el backend tiene 30 min de RQ timeout, así
    // que si llegamos acá el job casi seguro está stuck o el worker
    // murió. Mensaje claro al usuario + job sigue procesándose en
    // background (puede volver desde el Historial cuando termine).
    if (file) setRowStatus(file, "error", {
      error: "Esto está tardando más de lo esperado. Tu transcripción sigue procesándose — volvé al Historial en unos minutos para ver el resultado.",
    });
    return null;
  }, []);

  // Pre-upload + transcribe songs at indices fromIdx..queue.length-1 in the
  // background while the user is actively reviewing a different song (o ahora
  // también mientras está en la pantalla de upload eligiendo opciones).
  // Resultados van a prefetchCache.current[idx] para que transcribeNext los
  // sirva instant en vez de hacer al usuario esperar el round-trip.
  //
  // 2026-05-23: refactor a backend async. La respuesta del POST es ahora
  // {job_id, status: "transcribing_queued"} — hay que pollear /status hasta
  // que llegue a "transcribed" para obtener segments + reference_lyrics.
  // R-FRONT-3 (review specialist 2026-05-24): cost-leak prevention en
  // handleReset. Sin esto, las transcripciones encoladas en background
  // seguían drenando Whisper + R2 cuando el operador cancelaba el batch.
  // Conservador: solo abortamos LOOP ITERATIONS (no requests en progreso —
  // requeriría signal en uploadFileToR2 + authFetchWithRetryOn503, que es
  // refactor invasivo). El próximo iteration ve aborted=true y rompe.
  const prefetchAbortRef = useRef(null);
  if (prefetchAbortRef.current === null) {
    prefetchAbortRef.current = new AbortController();
  }

  const prefetchRemaining = useCallback(async (queue, fromIdx) => {
    // Snapshot del controller actual al arrancar el loop. Si handleReset
    // crea uno nuevo entremedio, esta closure sigue revisando el viejo
    // (que YA está abortado) y rompe limpio.
    const controller = prefetchAbortRef.current;
    for (let idx = fromIdx; idx < queue.length; idx++) {
      if (controller && controller.signal.aborted) {
        // handleReset disparó abort — paramos el prefetch loop. Los
        // requests en flight se completan (sin signal) pero el siguiente
        // iter NO arranca.
        break;
      }
      if (prefetchCache.current[idx]) continue;
      prefetchCache.current[idx] = { status: "loading" };
      const entry = queue[idx];
      const file = entry.file;
      try {
        setRowStatus(file, "uploading");
        const { jobId } = await uploadFileToR2(file, {
          meta: { artist: entry.artist || "", title: (entry.songTitle || "").trim() },
          // R-FRONT-3 end-to-end: si handleReset abort, la upload se corta
          // en la mitad del multipart en vez de seguir hasta terminar.
          signal: controller && controller.signal,
        });
        // BUG FIX 2026-05-25 (job duplication): guardar el jobId del upload
        // en el cache YA, antes del polling. Así transcribeNext (si el
        // operador clickea "Revisar" antes de que el prefetch termine) ve
        // el jobId y puede reusar este job en vez de crear uno nuevo.
        prefetchCache.current[idx] = { status: "loading", jobId };
        setRowStatus(file, "queued");
        const res = await authFetchWithRetryOn503(`${API}/transcribe-uploaded`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            job_id: jobId,
            language: entry.language || "",
            artist: entry.artist || "",
            title: (entry.songTitle || "").trim(),
          }),
          signal: controller && controller.signal,
        }, { maxRetries: 3 });
        if (!res.ok) {
          prefetchCache.current[idx] = { status: "error" };
          setRowStatus(file, "error");
          continue;
        }
        const initial = await res.json();
        // Backward compat: if backend returned segments inline (legacy sync
        // path con ASYNC_TRANSCRIBE_ENABLED=0), salteamos el polling.
        if (initial.segments) {
          prefetchCache.current[idx] = { status: "ready", data: initial, jobId };
          setRowStatus(file, "done");
          continue;
        }
        // Async path — pollear hasta transcribed.
        const data = await pollUntilTranscribed(initial.job_id || jobId, file);
        if (data) {
          prefetchCache.current[idx] = { status: "ready", data, jobId };
        } else {
          prefetchCache.current[idx] = { status: "error" };
        }
      } catch (err) {
        // R-FRONT-3 e2e: handleReset disparó abort. Salimos clean del
        // loop sin marcar "error" (no es un fallo real — es cancelación).
        if (err && (err.name === "AbortError" || (controller && controller.signal.aborted))) {
          prefetchCache.current[idx] = { status: "aborted" };
          break;
        }
        prefetchCache.current[idx] = { status: "error" };
        setRowStatus(file, "error");
      }
    }
  }, [pollUntilTranscribed]);

  // 2026-05-23: trigger de transcripción auto en el drop del archivo. Antes
  // se difería hasta el click en "Revisar", bloqueando al usuario ~15-20s en
  // un loader. Ahora arranca al instante en background mientras el operador
  // elige movement/effect/font/paleta. Cuando llega a "Revisar", los segments
  // están cacheados → editor abre instant.
  const onAutoTranscribe = useCallback((newFiles) => {
    // newFiles llega desde UploadZone.addFiles con el shape que ya conocemos.
    // Lo agregamos a la cola de prefetch. prefetchRemaining respeta serial
    // (1 a la vez) para no saturar el backend pool — para parallelismo
    // estricto refactorear con semáforo en v2.
    // Pasamos los files actuales + los nuevos para que prefetch use el
    // índice global del array en files (no del slice nuevo).
    setTimeout(() => {
      // Defer 1 tick para que setFiles haya commiteado antes del prefetch.
      const merged = filesRef.current || [];
      const startIdx = Math.max(0, merged.length - newFiles.length);
      prefetchRemaining(merged, startIdx);
    }, 0);
  }, [prefetchRemaining]);

  // --- Review flow ---
  // 2026-05-23: NO limpia más el prefetchCache. Si onAutoTranscribe ya cargó
  // transcripciones en background, las reusamos. El cache queda mapeado por
  // índice en `files`, así que es válido siempre que `files` no haya cambiado
  // de orden — y no lo cambiamos entre upload y review.
  const handleStartReview = async () => {
    if (!files.length || !files.every((f) => f.artist.trim())) return;
    // Capa B 2026-05-24 — antes navegábamos a /review (que disparaba un flash
    // a dashboard por una race con el guard del fallback). Capa A lo
    // mitigó con setTranscribing(true) sync, pero el URL change seguía
    // sucediendo (visualmente "salta" de wizard a otra pantalla aunque
    // el chrome sea igual). Capa B: no navegamos — wizardStage="review"
    // hace que /new renderice el reviewScreen content INLINE. El operador
    // ve transición continua, no jump de ruta.
    setReviewQueue([...files]);
    setTranscribing(true);
    setTranscribeError(null);
    setWizardStage("review");
    transcribeNext([...files], 0);
  };

  const handleGenerateDirect = () => {
    if (!files.length || !files.every((f) => f.artist.trim())) return;
    const jobList = files.map((f) => ({
      filename: f.file.name, _file: f.file, artist: f.artist.trim(),
      songTitle: (f.songTitle || "").trim(),
      language: f.language, genre: f.genre || "", font: f.font || "",
      concept: f.concept || "", movementStyle: f.movementStyle || "", effect: f.effect || "",
      backgroundHint: f.backgroundHint || "", bgVerbatim: !!f.bgVerbatim,
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
        concept: entry.concept || "", movementStyle: entry.movementStyle || "", effect: entry.effect || "",
        backgroundHint: entry.backgroundHint || "", bgVerbatim: !!entry.bgVerbatim,
        textCase: entry.textCase || "upper",
        fontScale: entry.fontScale || "1.0",
        textContrast: entry.textContrast || "medium",
        // Audit fix 2026-05-25: ANTES estos dos fields no se inicializaban.
        // El drift sync (App.jsx:396) los terminaba sincronizando por
        // accidente, pero si alguien borra ese effect el flow se rompe
        // silently y los videos UMG salen con animation='none' en vez
        // del batchDefault del operador. Init explícito acá.
        lyricsAnimation: entry.lyricsAnimation || "none",
        lineTransition: entry.lineTransition || "none",
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

    // BUG FIX 2026-05-25 (job duplication): si el prefetch del auto-transcribe
    // YA arrancó (status="loading") y todavía no terminó, NO crear un job
    // nuevo — esperar al existente. Sin este check, el operador clickeaba
    // "Revisar lyrics" mientras el prefetch corría → caía al slow path →
    // segundo uploadFileToR2 → SEGUNDO job creado para el mismo audio.
    // DB confirma: pares de jobs con MISMO filename, mismo user, ~121s
    // apart (el tiempo típico de wizard antes de clickear Revisar).
    if (cached?.status === "loading" && cached.jobId) {
      setTranscribing(true);
      setTranscribeError(null);
      setTranscribeProgress({
        phase: "transcribing",
        loaded: 0,
        total: 0,
        jobId: cached.jobId,
        fileName: entry.file?.name || "",
      });
      try {
        const data = await pollUntilTranscribed(cached.jobId, entry.file);
        if (data) {
          prefetchCache.current[idx] = { status: "ready", data, jobId: cached.jobId };
          setTranscribing(false);
          setTranscribeProgress(null);
          setCurrentReview({
            file: entry.file, artist: entry.artist, language: entry.language,
            songTitle: entry.songTitle || "",
            genre: entry.genre || "", font: entry.font || "",
            concept: entry.concept || "", movementStyle: entry.movementStyle || "", effect: entry.effect || "",
            backgroundHint: entry.backgroundHint || "", bgVerbatim: !!entry.bgVerbatim,
            textCase: entry.textCase || "upper",
            fontScale: entry.fontScale || "1.0",
            textContrast: entry.textContrast || "medium",
            // Audit fix 2026-05-25: ver comentario en setCurrentReview de
            // arriba (~línea 1163). Init explícito de los 2 ejes libass.
            lyricsAnimation: entry.lyricsAnimation || "none",
            lineTransition: entry.lineTransition || "none",
            segments: data.segments, referenceLyrics: data.reference_lyrics || "",
            coverageWarning: !!data.coverage_warning,
            recoverySource: data.recovery_source || "",
            transcribeJobId: data.job_id || cached.jobId,
            queueIdx: idx, queue,
          });
          prefetchRemaining(queue, idx + 1);
          return;
        }
        // pollUntilTranscribed returned null → prefetch failed. Caer al
        // slow path (que SÍ crea un job nuevo) — operador prefiere
        // un retry sobre "queda colgado forever".
        prefetchCache.current[idx] = { status: "error" };
      } catch (err) {
        // Si el poll falló transient, igual caemos al slow path.
        prefetchCache.current[idx] = { status: "error" };
      }
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
      // Carry jobId + fileName so the TranscribingProgress component can
      // open SSE on /events/{jobId} and render the modern stepper that
      // reads `current_step` emitted by `_step()` in main.py.
      setTranscribeProgress({
        phase: "transcribing",
        loaded: 0,
        total: 0,
        jobId: uploadJobId,
        fileName: entry.file?.name || "",
      });
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
      let data = await transcribeRes.json();
      // 2026-05-23 — si el backend respondió 202 sin segments (path async),
      // pollear /transcription-status hasta que termine. Backward compat:
      // si vinieron segments inline (ASYNC_TRANSCRIBE_ENABLED=0), seguir.
      if (!data.segments) {
        setTranscribeProgress({ phase: "transcribing", loaded: 0, total: 0 });
        const polled = await pollUntilTranscribed(data.job_id || uploadJobId, entry.file);
        if (!polled) {
          setTranscribing(false);
          setTranscribeProgress(null);
          setTranscribeError("La transcripción falló. Reintentá.");
          return;
        }
        data = polled;
      }
      setTranscribing(false);
      setTranscribeProgress(null);
      setCurrentReview({
        file: entry.file, artist: entry.artist, language: entry.language,
        songTitle: entry.songTitle || "",
        genre: entry.genre || "", font: entry.font || "",
        concept: entry.concept || "", movementStyle: entry.movementStyle || "", effect: entry.effect || "",
        backgroundHint: entry.backgroundHint || "", bgVerbatim: !!entry.bgVerbatim,
        textCase: entry.textCase || "upper",
        fontScale: entry.fontScale || "1.0",
        textContrast: entry.textContrast || "medium",
        // Audit fix 2026-05-25: init explícito de los 2 ejes libass.
        lyricsAnimation: entry.lyricsAnimation || "none",
        lineTransition: entry.lineTransition || "none",
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

  const handleApproveLyrics = async (editedSegments) => {
    const r = currentReview;
    if (!r) return;

    // Post-render edit branch: currentReview.editingJobId está set →
    // estamos editando lyrics de un job ya renderizado desde la ruta
    // /videos/:id/edit-lyrics. En vez de pushear al batch queue, postear
    // al endpoint /edit/:id con edit_type=lyrics y navegar de vuelta al
    // JobDetail (donde el polling de status retoma).
    if (r.editingJobId) {
      const result = await submitLyricsEdit({
        jobId: r.editingJobId,
        segments: editedSegments,
        baselineSegments: r.openSnapshotSegments || r.segments,
        font: r.font,
        textCase: r.textCase,
        textContrast: r.textContrast,
        lyricsAnimation: r.lyricsAnimation,
        lineTransition: r.lineTransition,
        t,
      });
      if (result.cancelled) return;
      if (result.unchanged) {
        alert({
          title: t("edit.stale_rerender_title") || "No detectamos cambios nuevos",
          description: t("edit.stale_rerender_prompt") ||
            "Las letras ya están guardadas y no detectamos cambios nuevos. Si el video todavía muestra la versión vieja, podés re-renderizarlo igual desde el video.",
          tone: "warning",
        });
        return;
      }
      if (result.error) {
        alert({
          title: t("edit.error_title") || "No pudimos aplicar el edit",
          description: result.error,
          tone: "error",
        });
        return;
      }
      const editedJobId = r.editingJobId;
      setCurrentReview(null);
      wizardPersistence.clear();
      navigate(`/videos/${editedJobId}`, { replace: true });
      return;
    }

    const newApproved = [...approvedJobs, {
      file: r.file, artist: r.artist, language: r.language,
      songTitle: r.songTitle || "",
      genre: r.genre || "", font: r.font || "", concept: r.concept || "",
      movementStyle: r.movementStyle || "", effect: r.effect || "",
      backgroundHint: r.backgroundHint || "", bgVerbatim: !!r.bgVerbatim,
      textCase: r.textCase || "upper",
      fontScale: r.fontScale || "1.0",
      // lyricTransition + textMotion: deprecados 2026-05-23.
      lyricsAnimation: r.lyricsAnimation || "none",
      lineTransition: r.lineTransition || "none",
      textContrast: r.textContrast || "medium",
      segments: editedSegments,
      transcribeJobId: r.transcribeJobId || null,
      // Capa C 2026-05-24: bgCacheKey viene del useBackgroundPreview hook
      // que corrió durante review. Si null = no se hizo pre-gen (free-tier
      // o params no estables); pipeline corre Veo/Imagen como siempre.
      bgCacheKey: r.bgCacheKey || null,
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
      concept: a.concept || "", movementStyle: a.movementStyle || "", effect: a.effect || "",
      backgroundHint: a.backgroundHint || "", bgVerbatim: !!a.bgVerbatim,
      textCase: a.textCase || "upper",
      fontScale: a.fontScale || "1.0",
      // lyricTransition + textMotion: deprecados 2026-05-23.
      lyricsAnimation: a.lyricsAnimation || "none",
      lineTransition: a.lineTransition || "none",
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
        if (style === "custom" && customColors.trim()) formData.append("custom_colors", customColors.trim());
        if (jobList[i].language) formData.append("language", jobList[i].language);
        if (jobList[i].genre) formData.append("genre", jobList[i].genre);
        if (jobList[i].font) formData.append("font", jobList[i].font);
        if (jobList[i].concept) formData.append("concept", jobList[i].concept);
        if (jobList[i].movementStyle) formData.append("movement_style", jobList[i].movementStyle);
        if (jobList[i].effect) formData.append("effect", jobList[i].effect);
        if ((jobList[i].backgroundHint || "").trim()) {
          formData.append("background_hint", jobList[i].backgroundHint.trim());
          if (jobList[i].bgVerbatim) formData.append("bg_verbatim", "true");
        }
        formData.append("text_case", jobList[i].textCase || "upper");
        formData.append("font_scale", String(jobList[i].fontScale || "1.0"));
        // lyric_transition + text_motion: deprecados 2026-05-23 (no se envían).
        formData.append("lyrics_animation", jobList[i].lyricsAnimation || "none");
        formData.append("line_transition", jobList[i].lineTransition || "none");
        formData.append("lyric_color", jobList[i].lyricColor || "#FFFFFF");
        formData.append("lyric_sung_color", jobList[i].lyricSungColor || "#FFFFFF");
        formData.append("text_contrast", jobList[i].textContrast || "medium");
        if (animateImage && backgroundFile) formData.append("animate_image", "true");
        formData.append("match_lyrics", String(!!inspiredByLyrics));
        // Capa C 2026-05-24 — si el operador hizo pre-gen del background
        // mientras editaba lyrics (POST /generate-preview), el hash del
        // cache va acá. Backend skip Veo/Imagen si el cache hit.
        if (jobList[i].bgCacheKey) {
          formData.append("bg_cache_key", jobList[i].bgCacheKey);
        }
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
        if (style === "custom" && customColors.trim()) generateBody.append("custom_colors", customColors.trim());
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
        if (jobList[i].effect) generateBody.append("effect", jobList[i].effect);
        if ((jobList[i].backgroundHint || "").trim()) {
          generateBody.append("background_hint", jobList[i].backgroundHint.trim());
          if (jobList[i].bgVerbatim) generateBody.append("bg_verbatim", "true");
        }
        generateBody.append("text_case", jobList[i].textCase || "upper");
        generateBody.append("font_scale", String(jobList[i].fontScale || "1.0"));
        // lyric_transition + text_motion: deprecados 2026-05-23 (no se envían).
        generateBody.append("lyrics_animation", jobList[i].lyricsAnimation || "none");
        generateBody.append("line_transition", jobList[i].lineTransition || "none");
        generateBody.append("lyric_color", jobList[i].lyricColor || "#FFFFFF");
        generateBody.append("lyric_sung_color", jobList[i].lyricSungColor || "#FFFFFF");
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
    // R-FRONT-3: abortamos el prefetch loop (el siguiente iter se rompe).
    // Después creamos un controller fresco para el próximo batch del operador.
    try { prefetchAbortRef.current && prefetchAbortRef.current.abort(); } catch {}
    prefetchAbortRef.current = new AbortController();
    setFiles([]); setJobs([]); setBackgroundFile(null); setBackgroundId(null);
    setReviewQueue([]); setCurrentReview(null); setApprovedJobs([]);
    setTranscribing(false); setReadyToGenerate(false); setTranscribeError(null);
    // Capa B 2026-05-24: el wizard descartó todo → vuelve al upload state.
    setWizardStage("upload");
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
        movementStyle: last.movementStyle || "", effect: last.effect || "",
        textCase: last.textCase || "upper",
        fontScale: last.fontScale || "1.0",
        // lyricTransition + textMotion: deprecados 2026-05-23.
        lyricsAnimation: last.lyricsAnimation || "none",
        lineTransition: last.lineTransition || "none",
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
    // Capa B 2026-05-24: la primer canción canceló review → vuelve al upload
    // INLINE (no navega). El operador ve la file list de nuevo, conserva su
    // configuración. Si quería tirar todo, usa Cancelar (handleReset).
    setWizardStage("upload");
  };

  const handleGenerateBatch = () => {
    setReadyToGenerate(false);
    // No tocamos wizardStage acá — startGenerationWithSegments navega a
    // /generating (pantalla dedicada de progreso). El wizard queda
    // "stale" pero handleReset lo limpia cuando el operator vuelve.
    startGenerationWithSegments(approvedJobs);
  };

  const handleSelectJob = (jobId, status) => {
    // `transcribed` = transcripción lista, el operador todavía no dio
    // "Generar". El badge "Listo p/ editar" promete 1-click → editor;
    // cortocircuitamos /videos/<id> y vamos directo al resume flow.
    if (status === "transcribed") {
      navigate(`/new?resume=${encodeURIComponent(jobId)}`);
      return;
    }
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
        alert({
          title: "No se pudo eliminar el video",
          description: data.detail || "Probá de nuevo en un momento.",
          tone: "error",
        });
        return;
      }
      // Optimistically drop from local list so the row disappears immediately.
      setHistory((prev) => prev.filter((j) => j.job_id !== jobId));
    } catch {
      alert({
        title: "No se pudo eliminar el video",
        description: "Hubo un problema de red. Revisá tu conexión y probá de nuevo.",
        tone: "error",
      });
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
        alert({
          title: "No se pudieron eliminar los videos",
          description: data.detail || "Probá de nuevo en un momento.",
          tone: "error",
        });
        return;
      }
      const data = await res.json().catch(() => ({ deleted: [], skipped: {} }));
      const deletedSet = new Set(data.deleted || []);
      setHistory((prev) => prev.filter((j) => !deletedSet.has(j.job_id)));
      const skippedCount = Object.keys(data.skipped || {}).length;
      if (skippedCount > 0) {
        alert({
          title: `${data.deleted.length} videos eliminados`,
          description: `${skippedCount} no se pudieron eliminar (estaban protegidos o ya no existían).`,
          tone: "warning",
        });
      }
    } catch {
      alert({
        title: "No se pudieron eliminar los videos",
        description: "Hubo un problema de red. Revisá tu conexión y probá de nuevo.",
        tone: "error",
      });
    }
  };

  const allHaveArtist = files.length > 0 && files.every((f) => f.artist.trim());

  // Capa C 2026-05-24 — dispara pre-gen del background apenas el operador
  // entra a review (transcribiendo o editando lyrics), con debounce 2s
  // sobre cambios de los params. Cuando termina, persiste bgCacheKey en
  // currentReview; el handleApproveLyrics lo pasa a approvedJobs; el
  // POST /generate lo manda como Form field.
  // Sólo cuando hay currentReview Y artist+songTitle filled.
  // Forwarded params usan style/customColors globales del wizard (no per-file).
  const previewEntry = currentReview ? {
    ...currentReview,
    style,                   // batch-level
    customColors,            // batch-level
    backgroundMode: "veo",
    animateImage: animateImage && !!backgroundFile,
    matchLyrics: inspiredByLyrics,
  } : null;
  // bgPreview se invoca por side-effect (POST + polling). El status/error
  // está en el return por si en el futuro mostramos un badge "Fondo:
  // generando…" en el editor. Hoy se persiste sólo via onCacheKey →
  // currentReview.bgCacheKey, y el handleApproveLyrics lo copia a
  // approvedJobs para mandarlo al POST /generate.
  // bgPreview alimenta:
  //   - onCacheKey → currentReview.bgCacheKey + approvedJobs (race fix R-FRONT-2).
  //   - bgPreview.status → chip subtle "Fondo: generando…" en LyricsEditor
  //     (UX specialist 2026-05-24, cierra el mental-model gap de pre-gen invisible).
  const bgPreview = useBackgroundPreview(previewEntry, {
    enabled: !!currentReview,
    api: API,
    authHeaders,
    onCacheKey: (key) => {
      // Update currentReview si aún estamos editando ese file.
      setCurrentReview((r) => (r ? { ...r, bgCacheKey: key } : r));
      // R-FRONT-2 (2026-05-24): si el operador aprobó ANTES que el preview
      // terminara (review rápido < 30s), currentReview ya es null. El cache
      // key se hubiera perdido y POST /generate correría Veo de vuelta.
      // Actualizamos approvedJobs también, matcheando por filename.
      setApprovedJobs((prev) => {
        if (!prev || prev.length === 0) return prev;
        const target = previewEntry?.file?.name;
        if (!target) return prev;
        let changed = false;
        const next = prev.map((j) => {
          if (j.bgCacheKey === key) return j;
          if (j.file && j.file.name === target) {
            changed = true;
            return { ...j, bgCacheKey: key };
          }
          return j;
        });
        return changed ? next : prev;
      });
    },
  });

  // --- Per-route screens (kept inline so they share App-level state) ---

  // Post-render edit: cuando currentReview.editingJobId está set, fetch
  // la URL firmada del MP4 ya renderizado para que el WizardLivePreview
  // central lo muestre. useMediaUrl maneja el caché + refresh del token
  // (5min ttl, refresh ~30s antes de expirar). El hook devuelve "" antes
  // de la primera respuesta — el preview cae a su modo legacy hasta que
  // la URL aterriza.
  const _editingJobId = currentReview?.editingJobId || null;
  const editingRenderedVideoUrl = useMediaUrl(_editingJobId, "video", "preview");

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
    <div className="w-full max-w-[1700px] mx-auto animate-fade-in">
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
        customColors={customColors}
        onCustomColorsChange={setCustomColors}
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
        // 2026-05-23: auto-transcribe en background al dropear.
        onAutoTranscribe={onAutoTranscribe}
        transcribeStatusByFile={transcribeStatusByFile}
        // Phase 2 (2026-05-25): el wizard ahora abarca la review (paso 6).
        // hasReviewableContent prende cuando arranca el transcribe o existe
        // currentReview/readyToGenerate — UploadZone avanza el stepper a 6
        // automáticamente. renderStep6 es el contenido completo de review
        // (mismo JSX que la pantalla separada anterior) inyectado en la
        // columna derecha del wizard.
        hasReviewableContent={
          !!currentReview || transcribing || !!transcribeError || readyToGenerate
        }
        renderStep6={() => reviewScreen}
        // Phase 3: pasar segments al WizardLivePreview central para que
        // muestre una línea real de la canción que se está revisando.
        reviewSegments={currentReview?.segments || null}
        // Phase C 2026-05-25: ref-based tick para que el WizardLivePreview
        // central renderice la línea ACTIVA (no la primera) con word-jump
        // sincronizado al audio. Sin re-renders en App.jsx — el preview lee
        // el ref con su propio rAF loop.
        playbackTickRef={playbackTickRef}
        // Post-render edit (EditLyricsRoute): cuando currentReview viene
        // con editingJobId, el wizard cambia a modo "editar job existente":
        // pasos 1, 2, 3, 5 lockeados (esos cambios requieren regenerar
        // fondo y los cubre el modo "background" de EditRequestPanel), el
        // preview central muestra el MP4 ya renderizado en vez de la
        // simulación de karaoke.
        lockedSteps={currentReview?.editingJobId ? [1, 2, 3, 5] : []}
        renderedVideoUrl={editingRenderedVideoUrl || null}
        // UI F5 (2026-05-26): le pasamos el bgStatus al wizard para que
        // UploadZone pueda derivar `placeholderBg` cuando montamos el
        // preview en paso 6. "done" = fondo final listo, todo lo demás
        // = muestra. El badge del preview cambia de "EN VIVO" a
        // "(muestra)" en consecuencia.
        bgStatus={bgPreview.status}
      />
    </div>
  );

  // /review handles three sub-states (transcribing spinner, LyricsEditor,
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
      // Upload progress = before the job_id exists; keep the simple bar.
      if (phase === "uploading") {
        const pct = total > 0 ? Math.round((loaded / total) * 100) : null;
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
            <h2 className="text-xl font-bold mb-2">{t("transcribe.uploading")}</h2>
            {pct !== null && (
              <p className="text-gray-500 text-sm">{t("transcribe.uploading_progress", { pct })}</p>
            )}
          </div>
        );
      }
      // Transcribing — modern stepper that reads backend current_step + progress
      // emitted by `_step()` in main.py. SSE-driven via useJobProgress.
      const currentJobId = transcribeProgress?.jobId;
      const fileName = transcribeProgress?.fileName || "";
      return (
        <TranscribingProgress
          jobId={currentJobId}
          api={API}
          token={token}
          t={t}
          fileName={fileName}
          queueIndex={approvedJobs.length + 1}
          queueTotal={reviewQueue.length}
        />
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
            //
            // 2026-05-25: tolera resume desde historial — en ese path
            // `currentReview.file` es null (el File del upload no se
            // restaura desde R2) y la key/filename caen al campo
            // `filename` que el resume handler popula del job DB.
            key={`${currentReview.file?.name || currentReview.filename || "resume"}:${currentReview.queueIdx}`}
            // Clear the app's own sticky top bar (~72px) so the editor's
            // sticky CTA header isn't hidden behind it in the wizard.
            stickyHeaderTop={72}
            segments={currentReview.segments}
            filename={currentReview.file?.name || currentReview.filename || ""}
            audioFile={currentReview.file}
            audioUrl={currentReview.audioUrl || null}
            referenceLyrics={currentReview.referenceLyrics || ""}
            coverageWarning={currentReview.coverageWarning}
            recoverySource={currentReview.recoverySource}
            onApprove={handleApproveLyrics}
            onBack={handleBackInReview}
            // Post-render edit: cuando editingJobId está set, el autosave
            // de /save-segments va al job real (no al transcribeJob, que
            // en este flow es null). Orden importante: editingJobId gana.
            transcribeJobId={currentReview.editingJobId || currentReview.transcribeJobId || null}
            onPersistSegments={persistSegmentsToBackend}
            isBatch={currentReview.queue.length > 1}
            batchProgress={currentReview.queue.length > 1
              ? `${currentReview.queueIdx + 1} ${t("editor.song_of")} ${currentReview.queue.length}`
              : ""}
            user={user}
            font={currentReview.font || ""}
            textCase={currentReview.textCase || "upper"}
            fontScale={parseFloat(currentReview.fontScale || "1.0")}
            textContrast={currentReview.textContrast || "medium"}
            // 2026-05-23: lyricTransition + textMotion deprecados. Ahora
            // el editor expone lyrics_animation + line_transition (libass,
            // paridad con el wizard).
            lyricsAnimation={currentReview.lyricsAnimation || "none"}
            lineTransition={currentReview.lineTransition || "none"}
            // Typography is now chosen LIVE in the editor preview (not in the
            // upload step). Thread the operator's choices back into
            // currentReview so handleApproveLyrics carries them to generate.
            onFontChange={(c) => setCurrentReview((r) => (r ? { ...r, font: c } : r))}
            onCaseChange={(c) => setCurrentReview((r) => (r ? { ...r, textCase: c } : r))}
            onContrastChange={(c) => setCurrentReview((r) => (r ? { ...r, textContrast: c } : r))}
            onAnimationChange={(c) => setCurrentReview((r) => (r ? { ...r, lyricsAnimation: c } : r))}
            onLineTransitionChange={(c) => setCurrentReview((r) => (r ? { ...r, lineTransition: c } : r))}
            // UX specialist 2026-05-24: chip de status del pre-gen del
            // fondo. Status posibles: "idle" | "queued" | "generating" |
            // "done" | "error" | "disabled" (free-tier plan-tier guard).
            bgStatus={bgPreview.status}
            // Phase 2 (2026-05-25): el editor se monta DENTRO del paso 6
            // del wizard que ya tiene los controles tipográficos en el
            // paso 4 ("Animación") y el WizardLivePreview en el centro.
            // No duplicar la columna izquierda del editor — el operador
            // navega al paso 4 desde el stepper si quiere cambiar font/
            // animation/contrast. Layout colapsa a 1 columna (timeline +
            // lista a ancho completo).
            hideTypographyControls={true}
            // Phase C 2026-05-25: callback que sincroniza el preview central
            // con la línea que está sonando ahora. Actualiza un ref para no
            // disparar re-renders a 60fps.
            onPlaybackTick={handlePlaybackTick}
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
    // Empty state — el operador llegó a /review sin estado (deep-link, refresh
    // sin sessionStorage, o transición rota). En vez de redirigir silencioso a
    // dashboard (race condition reportada 2026-05-24: el redirect se disparaba
    // por el primer render asíncrono de handleStartReview), mostramos un
    // fallback explícito con CTA para que el operador sepa qué pasó.
    return (
      <div className="w-full max-w-md mx-auto animate-fade-in text-center py-16">
        <div className="w-14 h-14 mx-auto mb-4 rounded-2xl bg-amber-500/10 flex items-center justify-center">
          <svg className="w-7 h-7 text-amber-400" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
            <circle cx="12" cy="12" r="10" />
            <path d="M12 8v4M12 16h.01" strokeLinecap="round" />
          </svg>
        </div>
        <h2 className="text-xl font-bold mb-2">{t("review.empty_title") || "No hay sesión activa"}</h2>
        <p className="text-sm text-gray-500 mb-6">
          {t("review.empty_subtitle") ||
            "Probablemente refrescaste la página o el enlace es directo. Volvé al panel para empezar de nuevo."}
        </p>
        <button onClick={() => navigate("/dashboard")} className="btn-primary">
          {t("review.empty_cta") || "Volver al panel"}
        </button>
      </div>
    );
  })();

  // Capa B 2026-05-24 + Phase 2 2026-05-25 — wizardScreen siempre es
  // newBatchScreen (UploadZone). El layout de 3 columnas del wizard
  // (stepper + WizardLivePreview + contenido) abarca ahora también la
  // review (paso 6): el contenido de reviewScreen se inyecta como render
  // prop en UploadZone y aparece en la columna derecha del wizard. El
  // operador NUNCA cambia de layout durante el flow — el preview central
  // y el stepper persisten desde el drop del audio hasta "Crear videos".
  // wizardStage queda como flag de back-compat (sessionStorage, /review
  // como ruta legacy) pero NO controla qué pantalla se renderiza.
  const wizardScreen = newBatchScreen;

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
              onOpenSearch={() => setSearchOpen(true)}
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
          {/* Capa B 2026-05-24 — /new y /review renderizan el MISMO content
              (wizardScreen) que conmuta upload ↔ review ↔ ready_to_generate
              vía wizardStage. /review se mantiene como ruta válida sólo para
              compat con bookmarks viejos; URL nueva canónica es /new. */}
          <Route path="/new" element={wizardScreen} />
          <Route path="/review" element={wizardScreen} />
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
          {/* Post-render edit: monta el mismo Studio Console que /new,
              pre-seeded con los segments/render_params del job. Stepper
              con pasos 1, 2, 3, 5 lockeados (esos cambios requieren
              regenerar fondo y los cubre el modo "background" de
              EditRequestPanel); pasos 4 (typography) y 6 (lyrics)
              editables. Centro muestra MP4 ya renderizado. Aprobar
              dispara /edit/:id con edit_type=lyrics y navega de vuelta
              a /videos/:id. */}
          <Route path="/videos/:id/edit-lyrics" element={
            <EditLyricsRoute
              setCurrentReview={setCurrentReview}
              wizardScreen={wizardScreen}
              t={t}
            />
          } />
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

      {/* 2026-05-25 PR-2 — Command palette ⌘K. Renderizado fuera de
          <Routes> para que sobreviva navegación entre rutas. El listener
          de teclado global vive en el GlobalSearchKeybinding helper. */}
      <SearchPalette
        isOpen={searchOpen}
        onClose={() => setSearchOpen(false)}
        jobs={history}
        onSelectJob={handleSelectJob}
      />
      <GlobalSearchKeybinding onOpen={() => setSearchOpen(true)} />
    </>
  );
}

// Listener global ⌘K / Ctrl+K para abrir el SearchPalette. Componente
// separado para no agregar otro useEffect al gigante de App. Solo
// monta el listener; el state vive en App.
function GlobalSearchKeybinding({ onOpen }) {
  useEffect(() => {
    const handler = (e) => {
      // ⌘K (mac) / Ctrl+K (windows/linux) — patrón Linear/Notion/Vercel
      if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        e.preventDefault();
        onOpen();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onOpen]);
  return null;
}
