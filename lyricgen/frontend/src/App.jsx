import { useState, useRef, useCallback, useEffect, lazy, Suspense } from "react";
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
import Sidebar from "./components/Sidebar";
import GlobalTopbar from "./components/GlobalTopbar";
import TitleCardPreview from "./components/TitleCardPreview";
// 2026-05-27 Phase-2 audit: LyricsEditor (~85 KB), AdminPanel (~50 KB)
// and Settings (~30 KB) lazy-load so the main bundle drops below
// 500 KB on the first paint. The editor in particular is only entered
// after the operator decides to review/edit a song — saving its bytes
// from the cold start shaves seconds on slow networks.
//
// 2026-05-30 perf: extended the lazy set to Landing (449 lines, only
// rendered when there's NO token — most operators land already
// logged in and never need this code), OnboardingTour (452 lines,
// only fires once per operator), and JobDetail (1772 lines, the
// single heaviest non-editor component). For JobDetail we tolerate
// a ~50-100 ms perceived delay on the FIRST /videos/:id open of
// a session in exchange for a much faster cold start everywhere
// else; subsequent opens are instant because the chunk is cached.
const LyricsEditor = lazy(() => import("./components/LyricsEditor"));
const AdminPanel = lazy(() => import("./components/admin/AdminPanel"));
const Settings = lazy(() => import("./components/Settings"));
const Landing = lazy(() => import("./components/Landing"));
const JobDetail = lazy(() => import("./components/JobDetail"));
const HistoryView = lazy(() => import("./components/HistoryView"));
const Dashboard = lazy(() => import("./components/Dashboard"));
const UploadZone = lazy(() => import("./components/UploadZone"));
const SearchPalette = lazy(() => import("./components/SearchPalette"));
import BatchProgress from "./components/BatchProgress";
import TranscribingProgress from "./components/TranscribingProgress";
import WhatsNewModal from "./components/WhatsNew/WhatsNewModal";
import GiftCreditsBanner from "./components/GiftCreditsBanner";
import { useAlert } from "./components/AlertProvider";
import {
  shouldEnableBackgroundPreview,
  useBackgroundPreview,
} from "./hooks/useBackgroundPreview";
import { useMediaUrl, clearMediaCache } from "./mediaUrl";
import { translateBackendError } from "./lib/lyricsEditSubmit";
import { mergeEditedSegments } from "./lib/reviewSegments";
import { sanitizeSegmentsForSave, findSanitizedDiffs } from "./lib/sanitizeSegments";
import { appendBackgroundFields } from "./lib/bgPayload";
import { computeFieldDiff, buildEditPayloads } from "./lib/editWizardDiff";
import { prefetchKey } from "./lib/prefetchKey";
import { anchorLyricsForEntry } from "./lib/anchorPayload";
import { track } from "./lib/telemetryTrack";

const API = import.meta.env.VITE_API_URL || "";

// 2026-05-27 Phase-2 — fallbacks shown for the brief window between
// "user navigated to a lazy route" and "the chunk has been parsed".
// Pure CSS, no fetches; mirrors the surrounding glass aesthetic so the
// swap to the real component doesn't reflow the viewport.
function RouteSuspenseFallback() {
  return (
    <div className="w-full max-w-3xl mx-auto px-4 py-12">
      <div className="h-8 w-48 rounded bg-surface-2/60 animate-pulse mb-6" />
      <div className="space-y-3">
        {Array.from({ length: 5 }).map((_, i) => (
          <div key={i} className="h-12 rounded-card bg-surface-2/40 ring-1 ring-white/[0.03] animate-pulse" />
        ))}
      </div>
    </div>
  );
}

function EditorSuspenseFallback() {
  // Editor wraps in a 980 px column; keep the placeholder the same
  // width so the wizard layout doesn't shift when the chunk lands.
  return (
    <div className="w-full max-w-[980px] mx-auto px-4 py-6">
      <div className="h-10 w-64 rounded bg-surface-2/60 animate-pulse mb-4" />
      <div className="h-20 rounded-card bg-surface-2/40 ring-1 ring-white/[0.03] animate-pulse mb-3" />
      <div className="space-y-2">
        {Array.from({ length: 8 }).map((_, i) => (
          <div key={i} className="h-10 rounded-card bg-surface-2/40 ring-1 ring-white/[0.03] animate-pulse" />
        ))}
      </div>
    </div>
  );
}

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
  if (res.status === 401) return t("batch.error_session_expired");
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
    // Email CTAs (billing lifecycle, dunning) link to /?view=settings&tab=...
    // — route them to the in-app Settings, preserving the requested tab so the
    // Facturación deep-link lands correctly.
    if (params.get("view") === "settings") {
      const tab = params.get("tab");
      navigate(tab ? `/account?tab=${tab}` : "/account", { replace: true });
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

  // --- Heartbeat de telemetría de sesiones (tiempo-en-app) ---
  // Manda 1 ping/min a /telemetry/heartbeat mientras haya sesión iniciada
  // y la pestaña esté visible. Alimenta el "tiempo en la app" y el "en
  // línea ahora" del tab Actividad del AdminPanel.
  //
  // Best-effort por contrato: los errores se tragan (sin Sentry, sin
  // toasts) — la telemetría jamás puede molestar al usuario. El gate real
  // vive en el server (TELEMETRY_ENABLED); features.telemetry === false
  // solo evita gastar requests cuando sabemos que está apagada. Si el
  // user cacheado es viejo y no trae el campo, mandamos igual y el server
  // responde recorded:false (inofensivo).
  useEffect(() => {
    const beat = () => {
      if (typeof document !== "undefined" && document.hidden) return;
      if (!getToken()) return;
      const u = getUser();
      if (u?.features && u.features.telemetry === false) return;
      authFetch(`${API}/telemetry/heartbeat`, { method: "POST" }).catch(() => {});
    };
    beat(); // primer beat al montar (si la pestaña está visible)
    const iv = setInterval(beat, 60_000);
    // Beat inmediato al volver el foco — así "en línea" se refleja al toque
    // y no hasta 60 s después.
    const onVis = () => {
      if (typeof document !== "undefined" && !document.hidden) beat();
    };
    document.addEventListener("visibilitychange", onVis);
    return () => {
      clearInterval(iv);
      document.removeEventListener("visibilitychange", onVis);
    };
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
// Global dunning banner. Shows when Stripe has flagged the account
// `past_due` (a charge failed and Smart Retries are in flight). The user
// keeps access during the grace period — this is a nudge, not a wall —
// so it's amber, not red, and the CTA drops straight into the Stripe
// Customer Portal to update the card (no card entry in our app — PCI).
// Recovery is automatic: once a retry succeeds, invoice.paid flips
// billing_status back to "active" and /auth/me clears the banner.
function PastDueBanner({ user }) {
  const { t } = useI18n();
  const [busy, setBusy] = useState(false);
  if (user?.billing_status !== "past_due") return null;

  const openPortal = async () => {
    setBusy(true);
    try {
      const res = await authFetch(`${API}/billing/portal`, { method: "POST" });
      const data = await res.json().catch(() => ({}));
      if (data.portal_url) {
        window.location.href = data.portal_url;
        return;
      }
    } catch { /* fall through to the in-app billing tab */ }
    setBusy(false);
    window.location.assign("/account?tab=facturacion");
  };

  return (
    <div className="relative z-10 px-4 md:px-8 pt-4">
      <div className="flex flex-col sm:flex-row sm:items-center gap-3 px-4 py-3 rounded-card bg-amber-500/10 ring-1 ring-amber-500/30">
        <svg className="w-5 h-5 text-amber-400 shrink-0" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m0 3.75h.008M10.34 3.94l-7.5 12.99A1.5 1.5 0 004.14 19.5h15.72a1.5 1.5 0 001.3-2.57l-7.5-12.99a1.5 1.5 0 00-2.6 0z" />
        </svg>
        <div className="flex-1 text-sm text-amber-100/90">
          <span className="font-semibold text-amber-100">
            {t("billing.past_due_title") || "Tu último pago falló."}
          </span>{" "}
          {t("billing.past_due_body") || "Actualizá tu medio de pago para no perder acceso a la generación de videos."}
        </div>
        <button
          onClick={openPortal}
          disabled={busy}
          className="shrink-0 px-4 py-2 rounded-lg text-sm font-semibold bg-amber-500 text-black hover:bg-amber-400 disabled:opacity-60 transition-colors"
        >
          {busy
            ? (t("common.opening") || "Abriendo…")
            : (t("billing.update_payment") || "Actualizar medio de pago")}
        </button>
      </div>
    </div>
  );
}

// Soft upgrade nudge (Fase 2.5). Shows BEFORE the hard 402 wall: when a
// finite-plan, non-overage user crosses 80% of their monthly quota we offer a
// self-serve upgrade instead of letting them slam into the limit and email
// support. Distinct from PastDueBanner (a payment FAILED) — this is purely a
// growth nudge, so it's dismissible per billing-month and never blocks. Hidden
// for: unlimited, allow_overage (no wall), past_due (that banner wins), and
// >=100% (the Dashboard limit UX already owns the wall itself).
function nudgeDismissKey() {
  const d = new Date();
  return `upgrade_nudge_dismissed_${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}

function UpgradeNudge({ user }) {
  const { t } = useI18n();
  const navigate = useNavigate();
  const [usage, setUsage] = useState(null);
  const [dismissed, setDismissed] = useState(() => localStorage.getItem(nudgeDismissKey()) === "1");

  useEffect(() => {
    let alive = true;
    authFetch(`${API}/usage`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => { if (alive && d) setUsage(d); })
      .catch(() => { /* degrade silently — never block the shell on /usage */ });
    return () => { alive = false; };
  }, [user?.plan]);

  if (dismissed) return null;
  if (user?.billing_status === "past_due") return null;   // PastDueBanner owns this
  if (user?.allow_overage) return null;                   // no wall to hit
  if (!usage || usage.plan === "unlimited") return null;
  if (!usage.alert_80 || usage.alert_100) return null;    // strictly the 80–99% band

  const remaining = Math.max(0, (usage.limit ?? 0) - (usage.used ?? 0));
  const dismiss = () => { localStorage.setItem(nudgeDismissKey(), "1"); setDismissed(true); };
  const goUpgrade = () => navigate("/account?tab=facturacion");

  return (
    <div className="relative z-10 px-4 md:px-8 pt-4">
      <div className="flex flex-col sm:flex-row sm:items-center gap-3 px-4 py-3 rounded-card bg-amber-500/[0.08] ring-1 ring-amber-500/25">
        <svg className="w-5 h-5 text-amber-300 shrink-0" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" d="M13 10V3L4 14h7v7l9-11h-7z" />
        </svg>
        <div className="flex-1 text-sm text-amber-100/90">
          <span className="font-semibold text-amber-100">
            {(t("billing.nudge_title") || "Te quedan {n} videos este mes").replace("{n}", String(remaining))}
          </span>{" "}
          {t("billing.nudge_body") || "Mejorá tu plan para no frenarte cuando llegues al tope."}
        </div>
        <button
          onClick={goUpgrade}
          className="shrink-0 px-4 py-2 rounded-lg text-sm font-semibold bg-amber-500 text-black hover:bg-amber-400 transition-colors"
        >
          {t("billing.nudge_cta") || "Mejorar plan"}
        </button>
        <button
          onClick={dismiss}
          aria-label={t("common.dismiss") || "Descartar"}
          className="shrink-0 text-amber-200/60 hover:text-amber-100 transition-colors p-1"
        >
          <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" /></svg>
        </button>
      </div>
    </div>
  );
}

function AppShell({ user, history, sidebarOpen, setSidebarOpen, onLogout, onOpenSearch, onStartNewBatch }) {
  const { t } = useI18n();
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const activeView =
    (pathname === "/new" || pathname === "/review" || pathname === "/generating") ? "new" :
    (pathname === "/videos" || pathname.startsWith("/videos/")) ? "history" :
    pathname === "/account" ? "settings" :
    pathname === "/admin" ? "admin" :
    "dashboard";

  useEffect(() => {
    const mobile = window.matchMedia("(max-width: 767px)");
    const closeOnMobileEntry = (event) => { if (event.matches) setSidebarOpen(false); };
    mobile.addEventListener?.("change", closeOnMobileEntry);
    return () => mobile.removeEventListener?.("change", closeOnMobileEntry);
  }, [setSidebarOpen]);

  useEffect(() => {
    if (!sidebarOpen || !window.matchMedia("(max-width: 767px)").matches) return undefined;
    const sidebar = document.querySelector(".app-sidebar");
    const focusable = () => [...(sidebar?.querySelectorAll('a[href], button:not([disabled])') || [])];
    requestAnimationFrame(() => focusable()[0]?.focus());
    const handleKeyDown = (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        setSidebarOpen(false);
        requestAnimationFrame(() => document.querySelector('.global-topbar__icon')?.focus());
      } else if (event.key === "Tab") {
        const items = focusable();
        if (!items.length) return;
        const first = items[0];
        const last = items[items.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
      }
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [sidebarOpen, setSidebarOpen]);

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
          className="fixed inset-0 bg-black/60 z-[35] md:hidden"
          onClick={() => setSidebarOpen(false)}
          aria-hidden="true"
        />
      )}

      <div className={`flex-1 min-h-screen transition-all duration-300 ${sidebarOpen ? "md:ml-60" : "md:ml-[72px]"}`}>
        <GlobalTopbar
          user={user}
          activeRenders={(history || []).filter((job) => ["processing", "queued", "editing", "transcribing", "transcribing_queued"].includes(job.status)).length}
          onSearch={onOpenSearch}
          onCreate={onStartNewBatch}
          onNavigate={handleNav}
          onLogout={onLogout}
          onToggleNavigation={() => setSidebarOpen(!sidebarOpen)}
          navigationOpen={sidebarOpen}
        />

        {/* Dunning banner — sits above content, below the top bar */}
        <PastDueBanner user={user} />
        <UpgradeNudge user={user} />

        {/* Content */}
        <main className="relative z-10 px-4 md:px-8 pt-6 pb-24">
          <GiftCreditsBanner user={user} />
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
    // authFetchWithTimeout (not bare authFetch): a hung/slow /status — e.g.
    // when the api's DB pool is briefly exhausted under a polling burst
    // (QueuePool timeout ~30s) — must surface as the error state, NOT a
    // permanent spinner. 2026-06-09: an operator hit an infinite spinner
    // opening a transcribed_pending job during exactly that condition.
    // Mirrors EditLyricsRoute, which already caps /status at 10s.
    authFetchWithTimeout(`${API}/status/${id}`, {}, 10_000)
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
    return (
      <div className="flex items-center justify-center min-h-[50vh]">
        <div className="w-12 h-12 border-2 border-brand border-t-transparent rounded-full animate-spin" />
      </div>
    );
  }
  return (
    <div className="flex justify-center">
      {/* 2026-05-30 perf: JobDetail is lazy-loaded. The Suspense fallback
          matches the spinner shown when the job is still being fetched,
          so the visual transition stays continuous. */}
      <Suspense
        fallback={
          <div className="flex items-center justify-center min-h-[50vh]">
            <div className="w-12 h-12 border-2 border-brand border-t-transparent rounded-full animate-spin" />
          </div>
        }
      >
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
      </Suspense>
    </div>
  );
}

// Deep-link adapter for /videos/:id/edit-lyrics. Bootstrappea
// currentReview con los datos del job (segments, render_params, URLs
// QA fix 2026-05-28 (audit P0 #75): panel reutilizable cuando el job
// no es editable. Para `status=editing` (re-renderizando un edit
// anterior), pollea /status cada 5s y muestra progress + estado.
// Cuando el job vuelve a editable (done/pending_review/rejected),
// recarga la ruta para que el editor monte solo. Para otros estados
// no-editable (queued, processing, error), muestra el mensaje estático
// de antes con un botón "Volver al video".
function EditingNotEditablePanel({ jobId, jobStatus, isRendering, onBack, t }) {
  const [polledStatus, setPolledStatus] = useState(jobStatus);
  const [polledProgress, setPolledProgress] = useState(null);
  const [polledStep, setPolledStep] = useState(null);

  useEffect(() => {
    if (!isRendering) return undefined;
    let cancelled = false;
    const tick = async () => {
      try {
        const res = await authFetch(`${API}/status/${jobId}`, {});
        if (!res.ok || cancelled) return;
        const data = await res.json();
        if (cancelled) return;
        const newStatus = data.status;
        setPolledStatus(newStatus);
        if (typeof data.progress === "number") setPolledProgress(data.progress);
        if (typeof data.current_step === "string") setPolledStep(data.current_step);
        // Transition a un estado editable → recargá la página para que
        // EditLyricsRoute corra su bootstrap de nuevo y monte el editor.
        const editable = ["done", "pending_review", "rejected"].includes(newStatus);
        if (editable) {
          // [editor-reload-loop] capture (P0 UMG Chile 2026-06-16). This reload
          // re-runs EditLyricsRoute's bootstrap. If the job keeps flipping back
          // to a rendering/editing status (so this panel re-mounts and reloads
          // again), the page reloads on a ~5s cadence — looks like the editor
          // "freezing and re-laying-out in a loop". Count reloads per job in
          // sessionStorage; a burst means we found the cycle.
          try {
            const k = `editreload:${jobId}`;
            const prev = JSON.parse(sessionStorage.getItem(k) || "null");
            const now = Date.now();
            const recent = prev && now - prev.first < 60000 ? prev.count + 1 : 1;
            sessionStorage.setItem(k, JSON.stringify({ first: recent === 1 ? now : prev.first, count: recent }));
            if (recent >= 3) {
              // eslint-disable-next-line no-console
              console.warn("[editor-reload-loop] job reloaded the editor repeatedly", {
                jobId, reloadsInWindow: recent, fromStatus: jobStatus, toStatus: newStatus,
              });
            }
          } catch { /* sessionStorage unavailable — proceed with the reload */ }
          window.location.reload();
        }
      } catch {
        // Silent — siguiente tick reintenta.
      }
    };
    const iv = setInterval(tick, 5000);
    tick(); // primero tick inmediato
    return () => { cancelled = true; clearInterval(iv); };
  }, [jobId, isRendering]);

  if (isRendering) {
    const pct = Math.max(3, Math.min(100, polledProgress || 0));
    return (
      <div className="text-center mt-16 max-w-md mx-auto px-4">
        <div className="w-14 h-14 mx-auto mb-4 rounded-2xl bg-brand/10 ring-1 ring-brand/30 flex items-center justify-center">
          <span className="w-6 h-6 border-2 border-brand-light border-t-transparent rounded-full animate-spin" />
        </div>
        <h2 className="text-xl font-bold mb-2">
          {t("edit.editing_in_progress_title") || "El video se está re-renderizando"}
        </h2>
        <p className="text-sm text-gray-500 mb-4">
          {t("edit.editing_in_progress_subtitle") ||
            "Estamos aplicando los cambios del edit anterior. Volveremos a abrir el editor automáticamente cuando termine."}
        </p>
        <div className="mt-3 h-1.5 rounded-full bg-surface-3/60 overflow-hidden max-w-xs mx-auto">
          <div
            className="h-full bg-gradient-to-r from-brand to-brand-light transition-[width] duration-700 ease-out"
            style={{ width: `${pct}%` }}
          />
        </div>
        <p className="text-[10px] text-gray-500 mt-2 font-mono">
          {polledStep || "video"} · {polledProgress || 0}%
        </p>
        <button onClick={onBack} className="btn-secondary mt-6">
          {t("detail.back") || "Volver al video"}
        </button>
      </div>
    );
  }

  // Other non-editable states: queued / processing / error / etc. No
  // hace falta polling porque para estos estados no tiene mucho sentido
  // esperar — el operador volvería al video y desde ahí ve el estado real.
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
          `Este video está en estado "${polledStatus}". Esperá a que termine el render o que pase a revisión.`}
      </p>
      <button onClick={onBack} className="btn-secondary">
        {t("detail.back") || "Volver al video"}
      </button>
    </div>
  );
}

// firmadas de audio/waveform/background) y renderiza el mismo
// wizardScreen del flow nuevo — el operador edita lyrics post-render
// dentro del Studio Console en vez de un modal separado con UX distinta.
// Pasos 1, 2, 3, 5 quedan lockeados desde App (vía currentReview.
// editingJobId) y el preview central muestra el MP4 ya renderizado.
function EditLyricsRoute({ setCurrentReview, setWizardStage, wizardScreen, t }) {
  const { id } = useParams();
  const navigate = useNavigate();
  // status: "loading" | "ready" | "no_segments" | "not_editable" |
  //         "not_found" | "error". Loading hasta que tanto el job como
  // las URLs firmadas aterricen; ready hace montar el wizardScreen.
  const [state, setState] = useState({ status: "loading" });

  useEffect(() => {
    let alive = true;
    setState({ status: "loading" });
    track("edit.entered", { job_id: id });

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
      // Two-phase bootstrap (PR fix/edit-lyrics-fast-mount, 2026-05-27).
      //
      // Pre-fix: 4 fetches en paralelo bloqueantes (Promise.all). El editor
      // no monta hasta que TODOS responden, y `/jobs/:id/waveform` puede
      // tardar 5-30s en cold cache (librosa.load + R2 download). Operador
      // reportó "no me deja corregir lyrics" cuando en realidad estaba
      // esperando 30-60s viendo un spinner sin layout.
      //
      // Pre-fix también sin timeout en NINGÚN fetch: si /waveform se cuelga
      // (libosa OOM, threadpool agotado), el spinner es literalmente
      // perpetuo. Ahora cada fetch tiene cap propio vía authFetchWithTimeout.
      //
      // Estrategia post-fix:
      //   Fase A (crítica, bloqueante): solo /status — Postgres query
      //     rápido. Define si el job es editable y tiene segments. Sin
      //     /status no hay UI posible (necesitamos saber el shape del job).
      //   Fase B (enhancement, fire-and-forget): /source-audio-url,
      //     /waveform, /background-url corren en paralelo SIN bloquear
      //     el mount del editor. El editor monta inmediato con
      //     audio/waveform/bg = null; cada fetch que resuelve patchea el
      //     campo correspondiente en currentReview. LyricsEditor maneja
      //     los nulls graciosamente (timeline sin waveform pintada,
      //     preview sin bg), así que la edición de texto/timing funciona
      //     desde el primer ms.

      // Fase A — /status: critical path, blocking.
      let statusRes;
      try {
        statusRes = await authFetchWithTimeout(`${API}/status/${id}`, {}, 10_000);
      } catch (e) {
        // TimeoutError o network error. /status es rápido en condiciones
        // normales (~50ms); un timeout de 10s implica DB stuck u otra
        // patología seria — fall back a state="error" con botón "Volver".
        if (alive) setState({ status: "error" });
        return;
      }
      if (!alive) return;
      if (statusRes.status === 404) { setState({ status: "not_found" }); return; }
      if (!statusRes.ok) { setState({ status: "error" }); return; }

      let job;
      try {
        job = await statusRes.json();
      } catch (jerr) {
        if (alive) setState({ status: "error" });
        return;
      }

      // Solo pending_review/done/rejected son editables (mismo gating
      // que canEditLyrics en JobDetail). Editing/queued/processing →
      // bail-out: no tiene sentido abrir el editor sobre un render en curso.
      const editable =
        job.status === "pending_review" ||
        job.status === "done" ||
        job.status === "rejected";
      if (!editable) {
        setState({ status: "not_editable", jobStatus: job.status });
        return;
      }

      // Sin segments_json no hay nada que editar — mismo banner amber
      // que mostraba el modal anterior, sin redirect silencioso.
      if (!Array.isArray(job.segments_json) || job.segments_json.length === 0) {
        setState({ status: "no_segments" });
        return;
      }

      const params = job.render_params || {};
      // Reuso del snapshot: si el operador refrescó con edits in-flight,
      // los segments del snapshot ganan sobre job.segments_json (que es
      // lo último que el autosave guardó pero podría no incluir el
      // último cambio uncommitted). La baseline para "unchanged" sigue
      // siendo lo que el job tiene RENDERIZADO (job.segments_json).
      const segmentsFromSnap = reusableSnap ? snap.currentReview.segments : job.segments_json;

      // Edit-wizard mode (PR feat/edit-wizard-mode, 2026-05-27):
      // pre-llenamos TODOS los campos editables del wizard desde la row del
      // Job para que el operador pueda corregir cualquier cosa post-render
      // (no solo lyrics). La baseline congela el snapshot a la entrada del
      // edit; el submit calcula el diff contra la baseline y emite N POSTs
      // /edit (uno por edit_type: metadata / typography / lyrics / background).
      //
      // Resilience: si el snap tiene fields editados in-flight (el operador
      // refrescó mid-edit), esos ganan. Si no, los valores actuales del job.
      const snapR = reusableSnap ? snap.currentReview : null;
      const pickSnapOr = (snapKey, fallback) =>
        (snapR && snapR[snapKey] != null && snapR[snapKey] !== "")
          ? snapR[snapKey]
          : fallback;

      const initialFields = {
        artist: pickSnapOr("artist", job.artist || ""),
        songTitle: pickSnapOr("songTitle", job.song_title || ""),
        font: pickSnapOr("font", params.font || ""),
        textCase: pickSnapOr("textCase", params.text_case || "upper"),
        textContrast: pickSnapOr("textContrast", params.text_contrast || "medium"),
        fontScale: String(pickSnapOr("fontScale", params.font_scale || "1.0")),
        lyricsAnimation: pickSnapOr("lyricsAnimation", params.lyrics_animation || "none"),
        lineTransition: pickSnapOr("lineTransition", params.line_transition || "none"),
        lyricColor: pickSnapOr("lyricColor", params.lyric_color || "#FFFFFF"),
        lyricSungColor: pickSnapOr("lyricSungColor", params.lyric_sung_color || "#FFFFFF"),
        movementStyle: pickSnapOr("movementStyle", params.movement_style || ""),
        effect: pickSnapOr("effect", params.effect || ""),
        backgroundHint: pickSnapOr("backgroundHint", params.background_hint || ""),
        bgVerbatim: snapR?.bgVerbatim != null ? !!snapR.bgVerbatim : !!params.bg_verbatim,
        backgroundMode: pickSnapOr("backgroundMode", params.background_mode || ""),
        // Title card customization (Full Rotor v1).
        titleTemplate: pickSnapOr("titleTemplate", params.title_template || "auto"),
        titleSize: String(pickSnapOr("titleSize", params.title_size || "1.0")),
        titleArtistFont: pickSnapOr("titleArtistFont", params.title_artist_font || ""),
        titleSongFont: pickSnapOr("titleSongFont", params.title_song_font || ""),
        // UI v1.1 (2026-05-30): manual song-title line break — "" = auto.
        titleSongBreak: pickSnapOr("titleSongBreak", params.title_song_break || ""),
      };

      // Baseline: snapshot inmutable de cómo está RENDERIZADO el video
      // ahora — el diff del submit compara contra esto, NO contra el snap
      // del autosave. Si el operador edita y vuelve atrás un campo al valor
      // original, el diff lo descarta (no manda al backend, no cuesta un
      // re-render por nada).
      const baseline = {
        artist: job.artist || "",
        songTitle: job.song_title || "",
        font: params.font || "",
        textCase: params.text_case || "upper",
        textContrast: params.text_contrast || "medium",
        fontScale: String(params.font_scale || "1.0"),
        lyricsAnimation: params.lyrics_animation || "none",
        lineTransition: params.line_transition || "none",
        lyricColor: params.lyric_color || "#FFFFFF",
        lyricSungColor: params.lyric_sung_color || "#FFFFFF",
        movementStyle: params.movement_style || "",
        effect: params.effect || "",
        backgroundHint: params.background_hint || "",
        bgVerbatim: !!params.bg_verbatim,
        backgroundMode: params.background_mode || "",
        // Title card customization (Full Rotor v1) — baseline for the diff.
        titleTemplate: params.title_template || "auto",
        titleSize: String(params.title_size || "1.0"),
        titleArtistFont: params.title_artist_font || "",
        titleSongFont: params.title_song_font || "",
        // UI v1.1: baseline for the manual break.
        titleSongBreak: params.title_song_break || "",
        segments: JSON.parse(JSON.stringify(job.segments_json || [])),
      };

      // Mount the editor NOW with audio/waveform/bg as null. The LyricsEditor
      // handles these as optional — timeline renders without waveform fill,
      // preview without bg image. Operator can edit text/timing immediately.
      setCurrentReview({
        editingJobId: id,
        // editMode + baseline son la API del flow edit-wizard. App.jsx los
        // lee en handleApproveLyrics para emitir POSTs /edit con el diff
        // contra baseline. UploadZone los lee para mostrar UIs de edición
        // de metadata y desbloquear los pasos editables.
        editMode: true,
        baseline,
        // jobStatus drives which edit_types are valid: typography +
        // background are pending_review-only at the backend (see
        // main.py:7444). The wizard surface reads this to keep the
        // dialog honest about what will actually re-render.
        jobStatus: job.status,
        // Read-only context — solo display, no editable post-render.
        deliveryProfile: job.delivery_profile || "youtube",
        style: job.style || "",
        // job.song_title puede llegar null para jobs viejos sin metadata
        // explícito; filename queda como fallback display sólo.
        segments: segmentsFromSnap,
        openSnapshotSegments: JSON.parse(JSON.stringify(job.segments_json)),
        filename: job.filename || job.artist || "lyrics",
        file: null,
        audioUrl: null,           // populated by Phase B
        waveform: null,           // populated by Phase B
        bgUrl: null,              // populated by Phase B
        ...initialFields,
        // Empty queue: this isn't a batch, it's a one-off edit.
        queue: [],
        queueIdx: 0,
        transcribeJobId: null,
        referenceLyrics: "",
      });
      // CRITICAL FIX 2026-05-27 (fix/edit-lyrics-set-wizard-stage): el
      // wizardScreen lee wizardStage para decidir qué renderear (upload
      // = UploadZone "Crear videos"; review = panel con LyricsEditor).
      // Sin esta llamada, currentReview.editingJobId queda set pero
      // wizardStage permanece en "upload" → el operador ve "Crear videos"
      // en vez del editor. Bug reproducido en multi-browser/multi-user;
      // logs [WIZARD-RENDER] del PR #406 confirmaron wizardStage=upload
      // post setCurrentReview.
      setWizardStage("review");
      setState({ status: "ready" });

      // Fase B — enhancements: fire-and-forget. Each fetch has a 15 s cap
      // (timeout); on success it patches the corresponding field into
      // currentReview, on failure / timeout it silently leaves the field
      // null and the editor keeps working in its degraded-visual mode.
      //
      // The setCurrentReview guard (editingJobId === id) prevents writes
      // racing against an operator who navigated to a different job before
      // the slow fetch landed.
      // `retries`: the source audio is ESSENTIAL for editing TIMING — without
      // it the timeline opens muted ("audio no disponible"). It used to be a
      // silent fire-and-forget like waveform/bg, so a single transient 500 (a
      // momentary DB hiccup on /source-audio-url — observed in prod on job
      // 0d05c360895a, 2026-06-03) or timeout left the operator unable to fix
      // timing. Retry it with backoff; waveform/bg stay best-effort.
      const enhanceField = async (url, key, extractor, { retries = 0 } = {}) => {
        for (let attempt = 0; attempt <= retries; attempt++) {
          try {
            const res = await authFetchWithTimeout(url, {}, 15_000);
            if (!alive) return;
            if (res.ok) {
              const data = await res.json();
              if (!alive) return;
              const value = extractor(data);
              setCurrentReview((prev) => {
                if (!prev || prev.editingJobId !== id) return prev;
                return { ...prev, [key]: value };
              });
              return;  // success
            }
            // non-ok (e.g. the transient-DB-retry middleware gave up with a
            // 500) — fall through to retry if attempts remain.
          } catch {
            // Timeout / network — fall through to retry if attempts remain.
          }
          if (attempt < retries) {
            await new Promise((r) => setTimeout(r, 400 * 2 ** attempt));  // 0.4/0.8/1.6s
            if (!alive) return;
          }
        }
        // Exhausted: leave the field unset; text editing still works and the
        // operator can reopen the editor to retry the audio fetch.
      };
      enhanceField(`${API}/jobs/${id}/source-audio-url`, "audioUrl", (d) => d?.url || null, { retries: 3 });
      enhanceField(`${API}/jobs/${id}/waveform`, "waveform", (d) => d);
      enhanceField(`${API}/jobs/${id}/background-url`, "bgUrl", (d) => d?.url || null);
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
  //
  // RACE GUARD 2026-05-27 (fix/edit-lyrics-bootstrap-race): bug reportado en
  // prod donde /edit-lyrics renderea "Crear videos" en lugar del editor.
  // Hipótesis: en React 18 con StrictMode + concurrent rendering, el cleanup
  // del useEffect con `[]` deps puede correr en momentos inesperados (e.g.
  // entre el setCurrentReview de Phase A y el siguiente render) y deja
  // currentReview=null. Resultado: wizardScreen renderea UploadZone con
  // hasReviewableContent=false, mostrando paso 1 ("Crear videos").
  //
  // Fix: deps `[id]` para que el cleanup capture el `myId` actual y SOLO
  // limpie si el currentReview todavía refleja ESE id específico — evita
  // clobber si por algún ciclo el setCurrentReview ya cambió a otro
  // editingJobId. También evita el caso de "navigate /edit-lyrics/X →
  // /edit-lyrics/Y" donde el cleanup con id viejo no debe tocar el
  // currentReview con id nuevo.
  useEffect(() => {
    const myId = id;
    return () => {
      let didClear = false;
      setCurrentReview((r) => {
        if (!r) return r;
        if (r.editingJobId !== myId) return r;
        didClear = true;
        return null;
      });
      // Solo resetear wizardStage si efectivamente limpiamos NUESTRO
      // currentReview. Si el cleanup corre tarde y el currentReview ya
      // refleja otro id (operador navegó a otro /edit-lyrics), no tocar
      // el wizardStage de la sesión nueva.
      if (didClear) {
        setWizardStage("upload");
        wizardPersistence.clear();
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  if (state.status === "loading") {
    return (
      <div className="flex items-center justify-center min-h-[50vh]">
        <div className="w-12 h-12 border-2 border-brand border-t-transparent rounded-full animate-spin" />
      </div>
    );
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
    // QA fix 2026-05-28 (audit P0 #75): cuando el job está en
    // status=editing (re-renderizando), el operador no debería tener
    // que refrescar manualmente. Pollea /status cada 5s y recarga
    // cuando el job vuelve a editable.
    const isRendering = state.jobStatus === "editing";
    return (
      <EditingNotEditablePanel
        jobId={id}
        jobStatus={state.jobStatus}
        isRendering={isRendering}
        onBack={() => navigate(`/videos/${id}`)}
        t={t}
      />
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
  // Audit 2026-05-26: the App body previously referenced a bare `location`
  // identifier which resolved to `window.location` via JS global lookup.
  // It "worked" only because window.location has .search/.pathname — but
  // useEffect deps on `location.search` never re-fire when navigate() updates
  // the URL via History API (React doesn't re-render on window.location).
  // The /new?resume=... flow is fragile against that. Use useLocation() so
  // React subscribes to URL changes correctly.
  const location = useLocation();

  const [token, setToken] = useState(getToken());
  const [user, setUser] = useState(getUser());
  const [files, setFiles] = useState([]);
  // Ref que espeja `files` para que callbacks sin dependencias (ej.
  // handleUploadAdvance en un setTimeout) lean el estado actual sin re-render
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
  // Add-on premium "Escenas" (multi-escena): decisión global de look para el
  // batch, igual que `style`. El toggle sólo se muestra a usuarios elegibles
  // (user.features.scenes); el backend re-valida has_scenes_access igual.
  const [enableScenes, setEnableScenes] = useState(false);
  // Art track ("official audio"): tipo de video = master audio + cover, sin
  // letra. Cuando está activo, el wizard fuerza la subida del cover, oculta
  // los controles de letra y saltea el editor/transcripción — se genera
  // directo con art_track=true.
  const [artTrack, setArtTrack] = useState(false);

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
  const playbackTickRef = useRef({ activeLine: "", activeStart: 0, activeEnd: 0, currentTime: 0, words: null });
  const handlePlaybackTick = useCallback((line, start, end, time, words) => {
    playbackTickRef.current = { activeLine: line, activeStart: start, activeEnd: end, currentTime: time, words: words || null };
  }, []);
  // 2026-07-16 (idea de Tomi): slot DOM bajo el video (col central del wizard)
  // donde LyricsEditor portalea su player bar, para que la columna de la letra
  // quede full. UploadZone attachea el elemento vía callback ref (setter estable
  // de useState) → re-render → LyricsEditor recibe el elemento y portalea. En
  // el /edit modal no se pasa nada → el player va inline como siempre.
  const [playerSlotEl, setPlayerSlotEl] = useState(null);

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

  // 2026-06-04 — stale-preview fix: limpiar el tick de playback en vivo cuando
  // cambia la canción previsualizada (nuevo upload o cambio de review). El ref
  // persiste mientras App está montada, así que sin este reset la última línea
  // reproducida de la canción ANTERIOR seguía mostrándose en el preview de la
  // nueva. Bug: terminé "Me Gustas" → subí "Nada" → el preview mostraba "será
  // que me gustas tanto" bajo "Línea actual: Nada". El reset deja que el
  // preview caiga al sample hasta que el operador reproduzca la nueva canción.
  useEffect(() => {
    playbackTickRef.current = { activeLine: "", activeStart: 0, activeEnd: 0, currentTime: 0 };
  }, [currentReview?.file?.name, files[0]?.file?.name]);
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
  // Bug cliente 2026-06-09 (Ana M.): qué fuente de fondo usar es decisión
  // del operador y vive ACÁ, no en UploadZone. Antes el tab (auto/library/
  // custom) era useState local de UploadZone: al desmontar/remontar el
  // wizard volvía visualmente a "Generar con IA" pero backgroundFile/
  // backgroundId seguían seteados en App — y /generate los mandaba igual.
  // Resultado: un batch nuevo de 3 audios salió con la imagen custom de un
  // video anterior mientras la UI prometía fondo IA. El envío ahora se
  // gatea por este modo: sólo se manda lo que el tab activo dice.
  const [bgSelectMode, setBgSelectMode] = useState("auto"); // auto | library | custom
  // Art tracks ALWAYS use the uploaded cover (bgSelectMode "custom"). The
  // wizard-restore path flips a restored "custom" back to "auto" (an
  // uploaded File can't survive serialization), which for an art track
  // wrongly swaps the cover uploader for the AI-background controls and
  // hides the cover step. Self-heal: whenever art track is on, force custom.
  useEffect(() => {
    if (artTrack && bgSelectMode !== "custom") setBgSelectMode("custom");
  }, [artTrack, bgSelectMode]);
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
  // Audit 2026-05-26 (#388 wizard-duplicate-jobs): lock against
  // double-fire of the "Generar" button. Without this, a fast double-
  // click (operator hovers Generate, clicks twice) or a React StrictMode
  // double-invoke in dev runs startGenerationWithSegments twice for the
  // same approvedJobs → two POST /generate against the same job_id →
  // backend race where the second call lands while the first is still
  // executing the worker update_job(progress=N) path, leaving the row
  // in an inconsistent (status=queued, progress=N) state that confuses
  // every subsequent reader. Mirror the approveLockRef pattern from
  // JobDetail.jsx:348 — set on entry, clear on completion of the async
  // dance kicked off by startGenerationWithSegments.
  const generateLockRef = useRef(false);
  // QA fix 2026-05-28: guard contra doble-click en "Aprobar y generar"
  // dentro del edit-wizard. El submit consolida todos los cambios en UN
  // único POST /edit. Sin este lock, un doble-click rápido dispara dos
  // POSTs paralelos — el primero gana el row lock del backend y flippea
  // status a "editing", el segundo trip el status gate con un 400 muy
  // confuso. setCurrentReview(null) recién se ejecuta después del POST
  // exitoso, así que React-state-based guards no atrapan el caso. Ref
  // sincrónico (set ANTES del primer await) lo cierra.
  const editSubmitLockRef = useRef(false);
  // 2 concurrent workers: enough to keep the queue fed without spiking
  // the API with 5 simultaneous upload-url+generate calls from one user.
  const PARALLEL_WORKERS = 2;

  // ─── Wizard persistence ──────────────────────────────────────────────
  // Snapshot of any pending batch found in sessionStorage at mount time.
  // Drives the resume banner. Cleared when the operator clicks
  // Continuar/Descartar or starts a fresh batch.
  //
  // HOTFIX 2026-05-29: if a snapshot exists but is no longer resumable
  // (post-refresh, only file STUBS without real Blobs), eagerly clear it
  // on mount. Without this the autosave would just keep overwriting
  // sessionStorage with the same skeletal state until the operator
  // does something fresh — and any pre-existing crash path that reads
  // `entry.file` on a stub keeps firing on every render. The user
  // reported "Algo salió mal" → reload → same screen, that's the loop
  // this prevents. Compatible with the previous resume behaviour:
  // if files ARE replayable (i.e. user navigated within the SPA without
  // a refresh), the snapshot still resumes normally.
  const [resumableWizard, setResumableWizard] = useState(() => {
    const snap = wizardPersistence.load();
    if (!snap) return null;
    if (wizardPersistence.hasResumableContent(snap)) return snap;
    // Skeletal snapshot. Wipe it so the autosave doesn't immediately
    // re-persist and so future renders don't see ghost state.
    try { wizardPersistence.clear(); } catch { /* noop */ }
    return null;
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
      // Audit 2026-05-26: while the resume banner is offering an unfinished
      // batch (resumableWizard non-null, "Tenés un batch sin terminar"),
      // the wizard state is still empty (user hasn't clicked Continuar
      // yet). Without this guard, that empty state hits the `!anyState`
      // branch on the SAME render the banner is rendering, and we wipe
      // the snapshot we're about to offer. Banner then shows but Continuar
      // has nothing to restore. Hold off until the banner is dismissed
      // — at that point resumableWizard is null again (the user either
      // accepted, which transitioned state to non-empty by then, or
      // rejected and explicitly cleared via the discard button).
      if (resumableWizard) return;
      // Fresh wizard / cleared explicitly → blow away the snapshot too.
      // Clear is rare (logout / discard); leave it sync.
      wizardPersistence.clear();
      return;
    }
    // Capa B 2026-05-24: persist wizardStage para que un refresh durante
    // review NO te tire de vuelta al state "upload". El snap.load() lo
    // rehidrata en el useEffect de mount.
    // Audit fix 2026-05-25: agregamos TODO el state top-level que faltaba
    // (delivery → delivery_profile UMG, style/customColors/etc.) para que
    // un refresh durante un batch UMG no caiga silently a youtube.
    //
    // 2026-05-27 perf audit (UMG micro-freezes): `wizardPersistence.save()`
    // wraps a synchronous `localStorage.setItem(JSON.stringify(...))`
    // that blocks the main thread for ~5-20 ms on a typical snapshot.
    // This effect re-runs on 12 different dep changes, many of which
    // fire during the poll loop. Defer to `requestIdleCallback` (with
    // setTimeout fallback) so the save happens during idle frames
    // instead of blocking renders. We also cancel any pending write
    // when the effect re-runs to coalesce rapid mutations.
    const snapshot = {
      files, approvedJobs, currentReview, reviewQueue, wizardStage,
      style, customColors, enableScenes, delivery, backgroundId, backgroundMode,
      bgSelectMode, animateImage, inspiredByLyrics,
    };
    const schedule = typeof requestIdleCallback !== "undefined"
      ? (cb) => requestIdleCallback(cb, { timeout: 1500 })
      : (cb) => setTimeout(cb, 0);
    const cancel = typeof cancelIdleCallback !== "undefined"
      ? (id) => cancelIdleCallback(id)
      : (id) => clearTimeout(id);
    const id = schedule(() => {
      try { wizardPersistence.save(snapshot); } catch (_) {}
    });
    return () => cancel(id);
  }, [
    files, approvedJobs, currentReview, reviewQueue, wizardStage,
    style, customColors, enableScenes, delivery, backgroundId, backgroundMode,
    bgSelectMode, animateImage, inspiredByLyrics,
    resumableWizard,
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
        // Audit adversarial 2026-06-09: este flujo entra DIRECTO a review —
        // las tabs de fondo del upload stage nunca se ven. Una selección
        // custom/library residual de un batch anterior se mandaría en
        // silencio (la variante "resume" del bug de Ana M.). El job
        // resumido no trae fondo propio (transcribed, pre-/generate), así
        // que IA es el default correcto.
        setBackgroundFile(null); setBackgroundId(null);
        setBgSelectMode("auto"); setAnimateImage(false); setEnableScenes(false);
        setArtTrack(false);
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
        if (snap.topLevel.enableScenes != null) setEnableScenes(!!snap.topLevel.enableScenes);
        if (snap.topLevel.delivery) setDelivery(snap.topLevel.delivery);
        if (snap.topLevel.backgroundId != null) setBackgroundId(snap.topLevel.backgroundId);
        if (snap.topLevel.backgroundMode != null) setBackgroundMode(snap.topLevel.backgroundMode);
        // bgSelectMode: snaps nuevos lo traen explícito; para snaps viejos
        // lo inferimos — si había backgroundId era el tab Library, si no,
        // auto. backgroundFile nunca sobrevive el snapshot (File no es
        // serializable), así que "custom" sin file degrada a auto y el
        // backend genera con IA — el fallback inofensivo.
        if (snap.topLevel.bgSelectMode != null) {
          setBgSelectMode(snap.topLevel.bgSelectMode === "custom" ? "auto" : snap.topLevel.bgSelectMode);
        } else if (snap.topLevel.backgroundId != null) {
          setBgSelectMode("library");
        }
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
    } catch (err) {
      // 2026-05-31 hotfix (Agus): si CUALQUIER paso del rehydrate falla
      // (snapshot de una versión vieja del bundle, JSON corrupto, shape
      // inválido en File stub, etc.) antes teníamos un try/finally sin
      // catch — la excepción burbujeaba al GlobalErrorBoundary o se
      // perdía y dejaba al usuario en `/new` con state vacío SIN alerta.
      // Síntoma reportado: "puse para generar las lyrics y me apareció
      // esto" + screenshot de la pantalla de upload vacía.
      // Ahora limpiamos persistence + state + mostramos alert + redirect
      // explícito a /new. Sentry breadcrumb para triage futuro.
      console.error("[wizard] resume failed", err);
      try {
        if (typeof window !== "undefined" && window.Sentry?.captureException) {
          window.Sentry.captureException(err, {
            tags: { feature: "wizard-resume" },
            extra: { snapKeys: Object.keys(snap || {}) },
          });
        }
      } catch { /* Sentry path itself must not throw */ }
      wizardPersistence.clear();
      setCurrentReview(null);
      setApprovedJobs([]);
      setReviewQueue([]);
      setFiles([]);
      setResumableWizard(null);
      setWizardStage("upload");
      alert({
        title: t("wizard.resume_failed_title") || "No pudimos retomar tu sesión",
        description: t("wizard.resume_failed_desc") ||
          "El estado guardado no es compatible con esta versión. Empezamos limpio.",
        tone: "warning",
      });
      navigate("/new", { replace: true });
    } finally {
      // Defer flag flip past the React commit so the persistence useEffect
      // runs once with the FULLY restored state and writes a fresh snapshot.
      setTimeout(() => { restoringRef.current = false; }, 0);
    }
  }, [navigate, alert, t]);

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

  // Self-heal: if we have a valid token but the user object is missing
  // (genly_user got cleared, localStorage got partially purged across
  // tabs, an old build saved only the token, etc.), the UI ends up in
  // a broken half-state where the sidebar's `{user && ...}` blocks
  // don't render — meaning no plan badge, no username, and NO logout
  // button. This left agus.cafisi stranded 2026-05-27: he could
  // operate the app but couldn't log out.
  //
  // Recovery: ask /auth/me for the canonical user record, save it
  // back to localStorage, and unblock the UI. If /auth/me returns
  // 401 the token is also dead, so we drop into the regular logout
  // path. Runs once per page load.
  const authMeRefetchedRef = useRef(false);
  useEffect(() => {
    if (!token || user || authMeRefetchedRef.current) return;
    authMeRefetchedRef.current = true;
    authFetch(`${API}/auth/me`)
      .then((r) => {
        if (r.status === 401) {
          handleLogout("expired");
          return null;
        }
        if (!r.ok) return null;
        return r.json();
      })
      .then((data) => {
        if (data && data.username) {
          localStorage.setItem("genly_user", JSON.stringify(data));
          setUser(data);
        }
      })
      .catch(() => { /* swallow; next page load will retry */ });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, user]);

  // Freshen the cached user once per load when it IS present (the recovery
  // effect above covers the missing case). `plan` and `billing_status` are
  // driven server-side by Stripe webhooks, so this is what lets a user who
  // just went past_due — or whose plan changed — see it reflected without
  // re-login. MERGE rather than replace: /auth/me carries fewer fields than
  // the login payload, so a blind overwrite would drop e.g. email_verified.
  // Best-effort: a 401/network failure leaves the cached user untouched
  // (the dedicated refresh/401 effects own session liveness).
  const userFreshenedRef = useRef(false);
  useEffect(() => {
    if (!token || !user || userFreshenedRef.current) return;
    userFreshenedRef.current = true;
    authFetch(`${API}/auth/me`)
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (!data || !data.id) return;
        setUser((prev) => {
          // Deep-merge en `features`: un shallow spread reemplazaría todo el
          // objeto, así un /auth/me que (durante un deploy) no traiga
          // features.art_track lo borraría y ocultaría la feature a un usuario
          // elegible. Preservar las flags previas y pisar solo las nuevas.
          const merged = {
            ...(prev || {}), ...data,
            features: { ...(prev?.features || {}), ...(data.features || {}) },
          };
          localStorage.setItem("genly_user", JSON.stringify(merged));
          return merged;
        });
      })
      .catch(() => { /* keep cached user; retries next load */ });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, user]);

  // reason="expired" → /login so the user can re-authenticate immediately.
  // reason="manual" (default) → / (landing page) for intentional logouts.
  //
  // Audit 2026-05-26: handleLogout previously only cleared token+user
  // state. On a shared machine (common with UMG operators), the wizard
  // state of User A — files, approvedJobs, currentReview, history,
  // jobs queue, persisted wizard snapshot, library batch defaults, and
  // media-token cache — survived logout and was visible to User B
  // when they logged in next on the same tab. Now we wipe all
  // session-scoped state + caches + storage keys atomically.
  const handleLogout = useCallback((reason = "manual") => {
    // Stop every active poll / SSE stream BEFORE clearing the token.
    pollingIntervals.current.forEach((handle) => {
      if (handle && typeof handle.close === "function") handle.close(); // EventSource
      else clearInterval(handle);
    });
    pollingIntervals.current.clear();

    // Identity / auth.
    localStorage.removeItem("genly_token");
    localStorage.removeItem("genly_user");

    // Wizard / session caches. wizardPersistence stores a TTL-bounded
    // snapshot of an in-progress wizard; the library batch defaults
    // remember the operator's last picked font/color/movement preset
    // for the next batch (UploadZone.BATCH_DEFAULTS_STORAGE_KEY).
    try { wizardPersistence.clear(); } catch { /* best effort */ }
    try { localStorage.removeItem("genly:wizardBatchDefaultsV1"); } catch { /* */ }

    // Short-lived media tokens (preview/download URLs scoped to
    // job+filetype). Without this, User B sees /preview URLs that
    // 401 or — worse, if the token is still inside its 5 min TTL —
    // serve User A's content for ~5 min.
    try { clearMediaCache(); } catch { /* */ }

    // 2026-05-30 perf: drop the previous operator's cached /usage
    // payload — same logic as clearMediaCache: User B should never
    // briefly see User A's quota counter on the same machine, even
    // if the badge re-fetches a moment later. We can't enumerate
    // localStorage entries safely here, so we wipe by known prefix.
    try {
      for (let i = localStorage.length - 1; i >= 0; i--) {
        const k = localStorage.key(i);
        if (k && k.startsWith("cache:usage:")) localStorage.removeItem(k);
      }
    } catch { /* */ }

    // React state. Reset every collection that holds user-derived
    // content; keep purely-UX state (sidebar, theme) alone.
    setToken(null);
    setUser(null);
    setFiles([]);
    setReviewQueue([]);
    setCurrentReview(null);
    setApprovedJobs([]);
    setJobs([]);
    setHistory([]);
    setHistoryError(false);
    setHistoryLoaded(false);
    setTranscribeStatusByFile({});
    setTranscribing(false);
    setTranscribeError(null);
    setTranscribeProgress(null);
    setReadyToGenerate(false);
    setSearchOpen(false);
    setResumableWizard(null);

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
        // Watchdog: an EventSource can OPEN successfully but then stay silent
        // — a buffering proxy, or (local dev) an SSE reader that doesn't see
        // the worker's writes. In that case neither onmessage nor onerror ever
        // fires and the progress screen hangs forever. If no event arrives
        // shortly after connecting, fall back to polling /status. Cleared as
        // soon as the first event lands.
        let sawEvent = false;
        let silentTimer = setTimeout(() => {
          if (!sawEvent) { cleanup(); startPolling(); }
        }, 6000);
        const cleanup = () => {
          clearTimeout(silentTimer);
          es.close();
          pollingIntervals.current.delete(es);
        };
        pollingIntervals.current.add(es);
        es.onmessage = (e) => {
          sawEvent = true;
          clearTimeout(silentTimer);
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
          cleanup();
          startPolling();
        };
        // QA fix 2026-05-28 (audit P0 #73): backend emite un evento
        // `unauthorized` named cuando detecta token expirado o tenant
        // cambiado mid-stream. EventSource trata estos como un canal
        // separado de `data` — sin addEventListener específico, los
        // eventos named se ignoran silenciosamente y el SSE queda
        // abierto colgado. Acá los capturamos, cerramos la conexión
        // y caemos a polling autenticado — el siguiente authFetch
        // pegará 401 y dispara el logout flow (clearToken + redirect
        // a login).
        es.addEventListener("unauthorized", (e) => {
          let reason = "expired";
          try { reason = JSON.parse(e.data || "{}").reason || reason; } catch {}
          console.warn(`[SSE] unauthorized event from backend (${reason}) — falling back to polling`);
          cleanup();
          startPolling();
        });
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

  useEffect(() => {
    // Set true on (re)mount so React 18 StrictMode's dev-only
    // setup→cleanup→setup cycle doesn't leave the ref stuck at `false` — that
    // would make every SSE/polling guard below bail and freeze the progress
    // screen (the "Armando el video" hang). Prod has no double-invoke, but
    // setting it here is correct in both.
    isMountedRef.current = true;
    return () => {
      // R-FRONT-5: marca unmounted ANTES de cerrar handles para que
      // cualquier callback async en flight (SSE messages bufferadas, polls
      // ya disparados) salga temprano vía el guard sin tocar state.
      isMountedRef.current = false;
      pollingIntervals.current.forEach((handle) => {
        if (handle && typeof handle.close === "function") handle.close();
        else clearInterval(handle);
      });
    };
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
  // Resultados van a prefetchCache.current[key] para que transcribeNext los
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
      const entry = queue[idx];
      const file = entry.file;
      // Key by FILE IDENTITY, not array index — removeFile re-packs `files`,
      // so a re-uploaded file at a freed index must NOT inherit a stale
      // transcription (incident 2026-06-01: prev song's lyrics on new audio).
      const key = prefetchKey(file);
      if (prefetchCache.current[key]) continue;
      prefetchCache.current[key] = { status: "loading" };
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
        prefetchCache.current[key] = { status: "loading", jobId };
        setRowStatus(file, "queued");
        // Versión B (letra anclada): re-leer la entry FRESCA por identidad
        // de archivo antes del POST. Con el trigger movido al avance de paso
        // el estado ya está resuelto al arrancar el prefetch, pero la subida
        // a R2 tarda decenas de segundos; si el operador vuelve al paso
        // "Subí" y cambia la fuente/letra DURANTE esa ventana, esta
        // re-lectura hace que el POST use el estado vigente (y complementa la
        // invalidación de cache de onInvalidatePrefetch para el caso ya
        // posteado). Solo viaja con lyricsSource="official" (el selector
        // manda, no el contenido del textarea).
        const fresh = (filesRef.current || [])
          .find((e) => e?.file && prefetchKey(e.file) === key) || entry;
        const res = await authFetchWithRetryOn503(`${API}/transcribe-uploaded`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            job_id: jobId,
            language: entry.language || "",
            artist: entry.artist || "",
            title: (entry.songTitle || "").trim(),
            live: !!entry.live,
            anchor_lyrics: anchorLyricsForEntry(fresh),
          }),
          signal: controller && controller.signal,
        }, { maxRetries: 3 });
        if (!res.ok) {
          prefetchCache.current[key] = { status: "error" };
          setRowStatus(file, "error");
          continue;
        }
        const initial = await res.json();
        // Backward compat: if backend returned segments inline (legacy sync
        // path con ASYNC_TRANSCRIBE_ENABLED=0), salteamos el polling.
        if (initial.segments) {
          prefetchCache.current[key] = { status: "ready", data: initial, jobId };
          setRowStatus(file, "done");
          continue;
        }
        // Async path — pollear hasta transcribed.
        const data = await pollUntilTranscribed(initial.job_id || jobId, file);
        if (data) {
          prefetchCache.current[key] = { status: "ready", data, jobId };
        } else {
          prefetchCache.current[key] = { status: "error" };
        }
      } catch (err) {
        // R-FRONT-3 e2e: handleReset disparó abort. Salimos clean del
        // loop sin marcar "error" (no es un fallo real — es cancelación).
        if (err && (err.name === "AbortError" || (controller && controller.signal.aborted))) {
          prefetchCache.current[key] = { status: "aborted" };
          break;
        }
        prefetchCache.current[key] = { status: "error" };
        setRowStatus(file, "error");
      }
    }
  }, [pollUntilTranscribed]);

  // Prefetch de transcripción al AVANZAR del paso "Subí", no al soltar el
  // archivo. Historia: el trigger vivía en el drop (2026-05-23) para que el
  // editor abriera instant, pero eso disparaba la transcripción cuando la
  // fuente de letra todavía era el default "IA de Genly" y la letra oficial
  // no estaba pegada → el job salía con anchor vacío y, aunque el operador
  // después eligiera "Tengo la letra oficial", servía Versión A (bug
  // staging job e77f84aefe33). Moviendo el disparo al avance de paso, la
  // fuente de letra + la letra oficial ya están resueltas por canción, así
  // que el POST /transcribe-uploaded sale con el anchor_lyrics correcto de
  // una, sin carrera ni parche. El operador igual gana el prefetch: la
  // transcripción corre en background mientras elige Modo/Movimiento/etc.
  const handleUploadAdvance = useCallback(() => {
    // Defer 1 tick por si el avance coincide con un setState de files.
    setTimeout(() => {
      const queue = filesRef.current || [];
      if (queue.length) prefetchRemaining(queue, 0);
    }, 0);
  }, [prefetchRemaining]);

  // Edge case (a): si el operador vuelve al paso "Subí" DESPUÉS de avanzar
  // (el prefetch ya salió) y cambia la fuente de letra o edita la letra
  // oficial de una canción, el job cacheado quedó desalineado con la nueva
  // elección. Descartamos su entrada de cache para que transcribeNext caiga
  // al slow path y re-transcriba con el estado actual.
  const invalidatePrefetchForFile = useCallback((file) => {
    if (!file) return;
    const key = prefetchKey(file);
    if (prefetchCache.current[key]) delete prefetchCache.current[key];
  }, []);

  // --- Review flow ---
  // 2026-05-23: NO limpia más el prefetchCache. Si el prefetch (disparado al
  // avanzar del paso "Subí") ya cargó transcripciones en background, las
  // reusamos. El cache queda mapeado por
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
    // Guard audit 2026-06-11: el tab "Upload" activo sin archivo real
    // (nunca eligió uno, o quedó un stub post-refresh) generaba con
    // fondo IA EN SILENCIO — el usuario creía que iba su archivo.
    // Mismo principio que el guard de audio stub más abajo: avisar y
    // abortar, nunca degradar en silencio.
    if (bgSelectMode === "custom" && (!backgroundFile || typeof backgroundFile.slice !== "function")) {
      alert({
        title: t("wizard.custom_bg_missing_title") || "Falta el fondo",
        description: t("wizard.custom_bg_missing_desc") ||
          "Elegiste \"Upload\" como fondo pero no hay ningún archivo cargado. Subí tu imagen o video en el paso Modo, o cambiá a \"Generar con IA\".",
        tone: "warning",
      });
      return;
    }

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
    // Mismo cleanup que startGenerationWithSegments: el batch ya está en
    // jobList; dejar `files` staged duplica renders en el próximo batch.
    setFiles([]);
    processQueueDirect(jobList);
  };

  const transcribeNext = async (queue, idx, reuseJobId = null) => {
    if (idx >= queue.length) return;
    const entry = queue[idx];
    // Cache key is the file identity (see prefetchKey), NOT the queue index,
    // so a removed+re-uploaded file can never serve another song's segments.
    const key = prefetchKey(entry.file);

    // Fast path: a background prefetch already finished for this file.
    let cached = prefetchCache.current[key];
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
        frameFormat: entry.frameFormat || "full",
        fontScale: entry.fontScale || "1.0",
        textContrast: entry.textContrast || "medium",
        // Audit fix 2026-05-25: ANTES estos dos fields no se inicializaban.
        // El drift sync (App.jsx:396) los terminaba sincronizando por
        // accidente, pero si alguien borra ese effect el flow se rompe
        // silently y los videos UMG salen con animation='none' en vez
        // del batchDefault del operador. Init explícito acá.
        lyricsAnimation: entry.lyricsAnimation || "none",
        lineTransition: entry.lineTransition || "none",
        // Title card customization (Full Rotor v1).
        titleTemplate: entry.titleTemplate || "auto",
        titleSize: entry.titleSize || "1.0",
        titleArtistFont: entry.titleArtistFont || "",
        titleSongFont: entry.titleSongFont || "",
        titleSongBreak: entry.titleSongBreak || "",
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

    // Audit 2026-07-02 (doble subida): si el prefetch está SUBIENDO
    // todavía (status="loading" sin jobId — el jobId se setea recién
    // cuando uploadFileToR2 resuelve), esperar acá a que el upload
    // avance en vez de caer al slow path. Antes, con subidas de varios
    // minutos (150 MB), clickear "Revisar" en esa ventana disparaba una
    // SEGUNDA subida del mismo archivo y supersede_sibling_drafts
    // borraba el job del prefetch en pleno multipart (part-url 404).
    if (cached?.status === "loading" && !cached.jobId && !reuseJobId) {
      setTranscribing(true);
      setTranscribeError(null);
      setTranscribeProgress({
        phase: "uploading", loaded: 0, total: entry.file.size,
        fileName: entry.file?.name || "",
      });
      // Techo generoso (60 min > peor caso de 150 MB en uplink lento +
      // reintentos); si el cache no progresa para entonces, algo está
      // realmente trabado y el slow path de abajo ES el retry correcto.
      const deadline = Date.now() + 60 * 60 * 1000;
      while (Date.now() < deadline) {
        await new Promise((r) => setTimeout(r, 750));
        const cur = prefetchCache.current[key];
        if (!cur || cur.status !== "loading" || cur.jobId) break;
      }
      cached = prefetchCache.current[key];
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
          prefetchCache.current[key] = { status: "ready", data, jobId: cached.jobId };
          setTranscribing(false);
          setTranscribeProgress(null);
          setCurrentReview({
            file: entry.file, artist: entry.artist, language: entry.language,
            songTitle: entry.songTitle || "",
            genre: entry.genre || "", font: entry.font || "",
            concept: entry.concept || "", movementStyle: entry.movementStyle || "", effect: entry.effect || "",
            backgroundHint: entry.backgroundHint || "", bgVerbatim: !!entry.bgVerbatim,
            textCase: entry.textCase || "upper",
            frameFormat: entry.frameFormat || "full",
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
        prefetchCache.current[key] = { status: "error" };
      } catch (err) {
        // Si el poll falló transient, igual caemos al slow path.
        prefetchCache.current[key] = { status: "error" };
      }
    }

    // Slow path: upload + transcribe now (first song, or prefetch missed).
    setTranscribing(true);
    setTranscribeError(null);
    setTranscribeProgress({ phase: "uploading", loaded: 0, total: entry.file.size });

    let transcribeRes = null;
    let uploadJobId = reuseJobId || null;
    try {
      // Step 1: stream the audio body straight to R2 via a presigned URL.
      // The API container never sees the bytes — that's the whole point
      // of the v2 flow. uploadFileToR2 picks single-PUT or multipart
      // automatically based on file size. Con reuseJobId (retry tras
      // fallo de /transcribe-uploaded) el audio YA está en R2 — saltear
      // la subida entera.
      if (!uploadJobId) ({ jobId: uploadJobId } = await uploadFileToR2(entry.file, {
        meta: {
          artist: entry.artist || "",
          title: (entry.songTitle || "").trim(),
        },
        onProgress: (loaded, total) => {
          setTranscribeProgress({ phase: "uploading", loaded, total });
        },
      }));

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
          live: !!entry.live,
          // Versión B: letra oficial pegada en el wizard → el backend la
          // ancla al motor CTC (flag ANCHOR_LYRICS_ENABLED; vacía = no-op).
          // Gated por el selector — volver a "IA de Genly" desactiva el
          // anclado aunque el textarea conserve texto (ver anchorPayload).
          anchor_lyrics: anchorLyricsForEntry(entry),
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
        if (transcribeRes.status === 401) {
          setTranscribing(false);
          setTranscribeProgress(null);
          handleLogout("expired");
          return;
        }
        if (reuseJobId && (transcribeRes.status === 404 || transcribeRes.status === 409)) {
          // El job reusado ya no está (reaper) o cambió de estado —
          // reintento limpio con subida fresca.
          return transcribeNext(queue, idx);
        }
        const reason = await describeFetchError(null, transcribeRes, t);
        setTranscribing(false);
        setTranscribeProgress(null);
        // El audio ya está en R2: el Retry reusa el job en vez de
        // volver a subir 150 MB (antes este branch ni seteaba el ctx,
        // así que el botón Reintentar directamente no aparecía).
        transcribeRetryCtx.current = { queue, idx, reuseJobId: uploadJobId };
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
          // QA fix 2026-05-28 (audit P0 #77): pre-fix mensaje opaco +
          // sin Retry button (transcribeRetryCtx no se seteaba). Ahora
          // distinguimos el caso de timeout (job sigue procesándose en
          // background — link a /dashboard) del de transcription_failed
          // (worker reportó error — Retry funciona) Y siempre seteamos
          // el retry context para que el botón aparezca.
          setTranscribing(false);
          setTranscribeProgress(null);
          transcribeRetryCtx.current = { queue, idx };
          // pollUntilTranscribed retorna null en dos casos:
          // - transcription_failed (data.error tiene el detalle del worker)
          // - timeout 20 min sin terminar (background sigue procesando)
          // En ambos casos el job está en DB y el operador puede ir a
          // /dashboard a esperarlo o reintentar.
          const errMsg = (t("transcribe.failed_retry") ||
            "No pudimos transcribir esta canción. Probá reintentar, " +
            "o si el problema persiste vení al historial en unos minutos " +
            "— el job sigue procesándose en segundo plano.");
          setTranscribeError(errMsg);
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
        frameFormat: entry.frameFormat || "full",
        fontScale: entry.fontScale || "1.0",
        textContrast: entry.textContrast || "medium",
        // Audit fix 2026-05-25: init explícito de los 2 ejes libass.
        lyricsAnimation: entry.lyricsAnimation || "none",
        lineTransition: entry.lineTransition || "none",
        // Title card customization (Full Rotor v1).
        titleTemplate: entry.titleTemplate || "auto",
        titleSize: entry.titleSize || "1.0",
        titleArtistFont: entry.titleArtistFont || "",
        titleSongFont: entry.titleSongFont || "",
        titleSongBreak: entry.titleSongBreak || "",
        segments: data.segments, referenceLyrics: data.reference_lyrics || "",
        coverageWarning: !!data.coverage_warning,
        recoverySource: data.recovery_source || "",
        transcribeJobId: data.job_id || uploadJobId,
        queueIdx: idx, queue,
      });
      // Phase B: fetch signed R2 audio URL in background so LyricsEditor
      // can use a durable server-side URL instead of the ephemeral blob.
      // Fire-and-forget — editor already works with the blob URL; this just
      // upgrades it to avoid ERR_FILE_NOT_FOUND when the blob gets revoked.
      const _newJobId = data.job_id || uploadJobId;
      if (_newJobId) {
        authFetch(`${API}/jobs/${_newJobId}/source-audio-url`)
          .then((r) => r.ok ? r.json() : null)
          .then((d) => {
            const url = d?.url || null;
            if (url) setCurrentReview((prev) => prev?.transcribeJobId === _newJobId ? { ...prev, audioUrl: url } : prev);
          })
          .catch(() => { /* blob URL still works as fallback */ });
      }
      // Kick off background upload+transcription for songs idx+1..N-1
      // while the user is reading/editing the current song's lyrics.
      prefetchRemaining(queue, idx + 1);
    } catch (err) {
      setTranscribing(false);
      setTranscribeProgress(null);
      // JWT died mid-flow and the proactive refresh didn't save us (e.g.
      // tab open >24 h with refresh also expired) → same treatment as the
      // dashboard 401 interceptors: clean logout to the login screen
      // instead of an ambiguous banner over a dead session.
      const status = err?.status ?? err?.response?.status;
      if (status === 401) {
        handleLogout("expired");
        return;
      }
      // err.response carries the actual HTTP response when uploadFileToR2
      // (or apiPost inside it) threw — transcribeRes is null in that case.
      const reason = await describeFetchError(err, transcribeRes ?? err?.response ?? null, t);
      transcribeRetryCtx.current = { queue, idx, reuseJobId: uploadJobId || null };
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
  // QA fix 2026-05-28 (audit P0 #74): retornar { ok, reason } en lugar
  // de void/swallow silente. Operadores reportaban "guardé y cuando
  // aprobé se perdieron los cambios" — la red caía mid-autosave, el
  // catch se tragaba el error, el operador no se enteraba y al
  // apretar Aprobar el último estado conocido del backend era el
  // anterior al cambio. Ahora el caller (LyricsEditor) usa el
  // resultado para mover saveStatus a "error" y mostrar un banner +
  // bloquear el botón Aprobar.
  const persistSegmentsToBackend = useCallback(async (jobId, segments, opts = {}) => {
    if (!jobId || !Array.isArray(segments) || segments.length === 0) {
      return { ok: false, reason: "no-data" };
    }
    // [drag-persist] diagnostic — remove after 2026-07-01 if reseed-storm
    // stays silent (fix landed in #724). Kept one extra week to confirm.
    // Sanitise to the backend contract BEFORE the POST. A single out-of-contract
    // segment (NaN/inverted/out-of-range from a drag or manual time edit) used to
    // 400 the whole save → saveStatus stuck on "error" → "Aprobar" blocked for the
    // job (incident 2026-06-26, Universal AR). We clamp so a stray bad value
    // degrades to "saved, fixable" instead of blocking everything.
    const safeSegments = sanitizeSegmentsForSave(segments);
    const _fixed = findSanitizedDiffs(segments, safeSegments);
    if (_fixed.length) {
      // Still surface WHAT was out of contract (Sentry [autosave] tag) even
      // though we no longer fail on it — so we can trace the source edit path.
      console.warn("[autosave] sanitized out-of-contract segment(s) before save", {
        jobId, count: _fixed.length, sample: _fixed.slice(0, 3),
      });
    }
    const _sample = safeSegments.slice(0, 2).map((s) => ({
      start: Math.round((s.start || 0) * 1000) / 1000,
      end: Math.round((s.end || 0) * 1000) / 1000,
    }));
    console.warn("[drag-persist] POST", { jobId, count: safeSegments.length, sample: _sample });
    try {
      const res = await authFetch(`${API}/jobs/${jobId}/save-segments`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ segments: safeSegments }),
        // keepalive lets this POST survive a page unload (refresh / tab close).
        // The unload flush in LyricsEditor sets it so the operator's last,
        // not-yet-debounced edits aren't canceled mid-flight by the browser
        // (reporte Gaby 2026-06-24: refrescó para salir del titileo y perdió
        // TODO). Body cap is 64KB — fine for a lyrics segment list.
        keepalive: opts.keepalive === true,
      });
      if (!res.ok) {
        if (res.status === 400) {
          // Post-sanitise this should be unreachable for segment-shape errors,
          // but if the backend rejects for a reason we don't mirror, capture the
          // exact `detail` (e.g. "segments[7] start/end out of range") instead of
          // a bare status — that's what we lacked on 2026-06-26.
          let detail = "";
          try { detail = (await res.clone().json())?.detail || ""; } catch { /* non-JSON body */ }
          console.warn("[autosave] /save-segments rejected 400", detail);
        } else if (res.status !== 404) {
          // 404 means the job was already reaped — nothing to save against.
          // We log it as a soft warning; the user will see the real error
          // when they click "Crear videos" and /generate returns 404.
          console.warn("[autosave] /save-segments failed", res.status);
        }
        // QA fix audit P0 #74: bubble error to caller for saveStatus chip.
        return {
          ok: false,
          reason: res.status === 404 ? "job-gone" : `http-${res.status}`,
          status: res.status,
        };
      }
      // Auditoría 2026-06-10 (reporte operadora "se mueve y pierde
      // cambios"): acá vivía el ECO del autosave — tras el 200,
      // setCurrentReview({...prev, segments}) escribía de vuelta el
      // snapshot ENVIADO. Cuando se agregó (2026-05-26) era la única
      // forma de mantener currentReview al día; desde que el mirror
      // sincrónico onEditedChange (handleEditedChange) propaga CADA
      // edición al instante, el eco solo puede ser igual o MÁS VIEJO
      // que currentReview. Si la operadora editaba mientras el POST
      // volaba, el eco pisaba esas ediciones y el prop-sync de
      // LyricsEditor reseedeaba `edited` hacia atrás: snap-back visual
      // ("empieza como a moverse"), drag en curso muerto (las _id se
      // reasignan y React remonta el bloque) y, con POSTs fuera de
      // orden o un unmount de por medio, pérdida real de trabajo.
      // El eco se ELIMINA: el mirror es la única fuente de frescura.
      return { ok: true };
    } catch (err) {
      console.warn("[autosave] /save-segments network error", err);
      // QA fix audit P0 #74: bubble network error to caller.
      return { ok: false, reason: "network", error: String(err) };
    }
  }, []);

  // Versión B, parte 2: re-anclar el timing con el texto YA corregido por
  // el operador. El backend toma segments_json (el autosave de arriba lo
  // dejó fresco), usa el texto como ancla del motor CTC y persiste el
  // timing re-anclado respetando las líneas `locked`. Devuelve el payload
  // del endpoint ({ok, count, review_count, segments}) para que el
  // LyricsEditor refresque su estado y muestre el toast de resultado.
  const reanchorSegmentsOnBackend = useCallback(async (jobId) => {
    if (!jobId) return { ok: false, reason: "no-job" };
    try {
      const res = await authFetch(`${API}/jobs/${jobId}/reanchor`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
      });
      if (!res.ok) {
        let detail = "";
        try { detail = (await res.clone().json())?.detail || ""; } catch { /* non-JSON body */ }
        console.warn("[reanchor] failed", res.status, detail);
        return { ok: false, reason: `http-${res.status}`, status: res.status, detail };
      }
      return await res.json();
    } catch (err) {
      console.warn("[reanchor] network error", err);
      return { ok: false, reason: "network", error: String(err) };
    }
  }, []);

  const handleApproveLyrics = async (editedSegments) => {
    const r = currentReview;
    if (!r) return;

    // Edit-wizard mode (PR feat/edit-wizard-mode, 2026-05-27):
    // diff cualquier campo editable del wizard contra el baseline congelado
    // en EditLyricsRoute y firea UN ÚNICO POST /edit consolidando todos los
    // cambios.
    //
    // QA fix 2026-05-28 (status conflict): la versión anterior posteaba N
    // veces (uno por bucket). La primera POST flippea el job a status=
    // editing — la segunda POST en la secuencia trip el status gate del
    // backend ("Lyrics edit requires the job to be done, pending_review,
    // or rejected (current: editing)"). Reproducible cuando el operador
    // cambia más de un tipo de campo en una sesión. Ahora consolidamos:
    // un único POST con el edit_type de mayor prioridad y TODOS los campos
    // editados como propiedades. Backend aplica los campos ungated
    // (font/text_case/effect/lyrics_animation/line_transition + segments
    // si vienen) en cualquier edit_type. artist/song_title también
    // ungated en backend post-fix 2026-05-28.
    if (r.editMode || r.editingJobId) {
      const editedJobId = r.editingJobId;

      // Double-click guard: si el operador hace doble-click en "Aprobar y
      // generar" mientras la primera POST está in-flight, sin esto dos
      // POSTs paralelos hitten al backend simultáneamente — la primera
      // gana el row lock, la segunda ve status=editing y 400.
      if (editSubmitLockRef.current) return;
      editSubmitLockRef.current = true;

      try {
        // Snapshot del estado actual del wizard. editedSegments viene del
        // LyricsEditor con el último drag aplicado — gana sobre r.segments
        // por si el autosave todavía no lo persistió.
        const current = {
          artist: r.artist,
          songTitle: r.songTitle,
          font: r.font,
          fontScale: r.fontScale,
          textCase: r.textCase,
          textContrast: r.textContrast,
          lyricsAnimation: r.lyricsAnimation,
          lineTransition: r.lineTransition,
          effect: r.effect,
          backgroundHint: r.backgroundHint,
          bgVerbatim: r.bgVerbatim,
          backgroundMode: r.backgroundMode,
          movementStyle: r.movementStyle,
          segments: editedSegments,
        };

        const diff = computeFieldDiff(r.baseline, current);
        const presentBuckets = Object.keys(diff);

        if (presentBuckets.length === 0) {
          alert({
            title: t("edit.no_changes_title") || "No cambiaste nada",
            description: t("edit.no_changes_subtitle") ||
              "No detectamos diferencias contra el video actual. Modificá algún campo y volvé a intentar.",
            tone: "warning",
          });
          return;
        }

        // Pick edit_type by priority — el más complejo de los presentes.
        // background regen Veo (paid), lyrics re-renderiza con nuevos
        // segments, metadata sólo title card, typography el último.
        const PRIORITY = ["background", "lyrics", "metadata", "typography"];
        let chosenType = PRIORITY.find((k) => diff[k]);

        // Backend gates: typography y background solo aceptan jobs en
        // pending_review (main.py:7444). Para done/rejected:
        //   - bg standalone: backend rechaza. Surface error claro.
        //   - typography standalone: piggyback en una lyrics edit usando
        //     los segments actuales (font/case/etc son ungated, se
        //     aplican en cualquier edit_type).
        const isPendingReview = r.jobStatus === "pending_review";
        if (!isPendingReview) {
          if (chosenType === "background" && presentBuckets.length === 1) {
            alert({
              title: t("edit.bg_locked_done_title") || "No se puede regenerar el fondo",
              description: t("edit.bg_locked_done_desc") ||
                "El fondo de un video ya aprobado no se puede regenerar — para cambiarlo, generá un video nuevo.",
              tone: "warning",
            });
            return;
          }
          if (chosenType === "background") {
            // bg + otra cosa: bajar a la siguiente prioridad permitida.
            chosenType = PRIORITY.slice(1).find((k) => diff[k] && k !== "background");
          }
          if (chosenType === "typography") {
            // Convertir a lyrics edit. Necesita segments — usar el snapshot
            // actual (editedSegments) aunque no haya cambiado, para que el
            // backend acepte el payload.
            chosenType = "lyrics";
            const normalized = (editedSegments || []).map((s) => ({
              start: Number(s.start) || 0,
              end: Number(s.end) || 0,
              text: String(s.text || ""),
              ...(s.locked ? { locked: true } : {}),
              ...(s.pos && typeof s.pos.x === "number" ? { pos: { x: s.pos.x, y: s.pos.y } } : {}),
              ...(typeof s.scale === "number" && s.scale !== 1 ? { scale: s.scale } : {}),
              ...(typeof s.rot === "number" && s.rot !== 0 ? { rot: s.rot } : {}),
            }));
            diff.lyrics = diff.lyrics || { segments: normalized };
          }
        }

        // Construir UN único payload con todos los campos cambiados.
        // Backend aplica los ungated en cualquier edit_type; los gated
        // (background_*, segments para lyrics) sólo se aplican si el
        // edit_type matchea.
        track("edit.submitted", { job_id: editedJobId, fields: presentBuckets });
        const payload = { edit_type: chosenType };
        if (diff.metadata) Object.assign(payload, diff.metadata);
        if (diff.typography) Object.assign(payload, diff.typography);
        if (diff.lyrics) Object.assign(payload, diff.lyrics);
        if (diff.background) Object.assign(payload, diff.background);

        const doPost = async (body) => {
          const res = await authFetch(`${API}/edit/${editedJobId}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
          });
          let data = {};
          try { data = await res.json(); } catch { /* empty body */ }
          return { res, data };
        };

        let { res, data } = await doPost(payload);

        // YouTube already-published 409 retry.
        if (res.status === 409 && data?.detail?.code === "youtube_already_published") {
          const url = data.detail.youtube_url;
          const msg = (t("edit.youtube_drift_confirm") ||
            "Este video ya está publicado en YouTube. La re-sincronización actualizará el archivo en la plataforma pero NO reemplazará el video en YouTube (la API de YouTube no permite reemplazar archivos, solo metadata).\n\n¿Continuar igual?")
            + (url ? `\n\nYouTube: ${url}` : "");
          if (!window.confirm(msg)) return;
          ({ res, data } = await doPost({ ...payload, allow_youtube_drift: true }));
        }

        if (!res.ok) {
          const friendly = translateBackendError(data?.detail, t) || `Error ${res.status}`;
          alert({
            title: t("edit.error_title") || "No pudimos aplicar el edit",
            description: friendly,
            tone: "error",
          });
          console.warn("[edit-wizard] /edit failed", { status: res.status, detail: data });
          return;
        }

        setCurrentReview(null);
        wizardPersistence.clear();
        navigate(`/videos/${editedJobId}`, { replace: true });
        return;
      } finally {
        editSubmitLockRef.current = false;
      }
    }

    // 2026-06-04 — settings-loss fix: currentReview puede no tener los picks
    // del operador (movement/effect/bg/typo) si el sync file→review no corrió
    // para esta canción (p.ej. los eligió después del transcribe, o subió la
    // canción heredando batchDefaults que nunca llegaron a currentReview). El
    // FILE entry SIEMPRE refleja los batch picks vigentes (updateBatchDefault
    // fanea a files[*]), así que caemos a él cuando currentReview viene vacío.
    // Bug UMG: eligió Foto fija + Bokeh pero el render salió con
    // movement_style='' + effect='' → fondo de VIDEO en vez de foto + sin
    // efecto. Cubre TODOS los efectos (mismo campo) y TODOS los movement styles.
    const _fm = files.find((f) => f?.file?.name === r.file?.name) || {};
    const _rf = (k) => r[k] || _fm[k] || "";
    const newApproved = [...approvedJobs, {
      file: r.file, artist: r.artist, language: r.language,
      songTitle: r.songTitle || "",
      genre: _rf("genre"), font: _rf("font"), concept: _rf("concept"),
      movementStyle: _rf("movementStyle"), effect: _rf("effect"),
      backgroundHint: _rf("backgroundHint"), bgVerbatim: !!r.bgVerbatim,
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
    track("wizard.approve_lyrics", { segments: (editedSegments || []).length });
    setApprovedJobs(newApproved);
    setCurrentReview(null);

    // Fire-and-forget commit of the just-approved segments to the backend.
    // Bumps last_user_activity_at and persists segments_json so the reaper
    // won't barre the job before the operator hits "Crear videos" on the
    // next song. See persistSegmentsToBackend comment for context.
    if (r.transcribeJobId) {
      persistSegmentsToBackend(r.transcribeJobId, editedSegments);
    }

    // HOTFIX 2026-05-29 (#473.2): wrap el switch final en try/catch.
    // startGenerationWithSegments puede crashear si el state está corrupto
    // (stub file post-refresh). Sin este wrapper, el error burbujea al
    // GlobalErrorBoundary y el usuario ve "Algo salió mal" sin recovery
    // claro — termina en /new pensando que tiene que subir todo de nuevo,
    // pero el job en backend queda en transcribed_pending huérfano.
    const nextIdx = r.queueIdx + 1;
    try {
      if (nextIdx < r.queue.length) {
        transcribeNext(r.queue, nextIdx);
      } else if (r.queue.length === 1) {
        await startGenerationWithSegments(newApproved);
      } else {
        setReadyToGenerate(true);
      }
    } catch (e) {
      console.error("[wizard] approve→generate failed", e);
      alert({
        title: t("wizard.generate_failed_title") || "No pudimos disparar la generación",
        description: t("wizard.generate_failed_desc") ||
          "Hubo un error inesperado al iniciar la generación. Recargá la página y volvé a intentar.",
        tone: "error",
      });
    }
  };

  const startGenerationWithSegments = async (approved) => {
    // Guard audit 2026-06-11: el tab "Upload" activo sin archivo real
    // (nunca eligió uno, o quedó un stub post-refresh) generaba con
    // fondo IA EN SILENCIO — el usuario creía que iba su archivo.
    // Mismo principio que el guard de audio stub más abajo: avisar y
    // abortar, nunca degradar en silencio.
    if (bgSelectMode === "custom" && (!backgroundFile || typeof backgroundFile.slice !== "function")) {
      alert({
        title: t("wizard.custom_bg_missing_title") || "Falta el fondo",
        description: t("wizard.custom_bg_missing_desc") ||
          "Elegiste \"Upload\" como fondo pero no hay ningún archivo cargado. Subí tu imagen o video en el paso Modo, o cambiá a \"Generar con IA\".",
        tone: "warning",
      });
      return;
    }
    track("wizard.generate", { mode: "reviewed", batch_size: (approved || []).length });
    // HOTFIX 2026-05-29 (#473.2): si el operador refrescó la pestaña entre
    // upload y "Aprobar y generar", `a.file` quedó como stub serializado
    // (sin Blob real, sin .slice). El POST /generate necesita el audio en
    // multipart UNLESS hay transcribeJobId (en cuyo caso el backend reusa
    // la copia cacheada en R2). Si no se cumple ninguna, abortar con UX
    // clara en vez de crashear al leer a.file.name.
    const broken = approved.find(
      (a) => !a.transcribeJobId && (!a.file || typeof a.file.slice !== "function")
    );
    if (broken) {
      console.warn("[wizard] approve aborted: file is not a Blob", {
        has_file: !!broken.file,
        has_transcribeJobId: !!broken.transcribeJobId,
        is_stub: !!broken.file?._restoredStub,
      });
      alert({
        title: t("wizard.session_expired_title") || "Tu sesión expiró",
        description: t("wizard.session_expired_desc") ||
          "El audio se perdió al refrescar la pestaña. Volvé a Crear video y re-subí el archivo.",
        tone: "warning",
      });
      wizardPersistence.clear();
      setCurrentReview(null);
      setApprovedJobs([]);
      setReadyToGenerate(false);
      navigate("/new", { replace: true });
      return;
    }

    const jobList = approved.map((a) => ({
      filename: (a.file && a.file.name) || "audio.mp3", _file: a.file, artist: a.artist,
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
      // Title card customization (Full Rotor v1).
      titleTemplate: a.titleTemplate || "auto",
      titleSize: a.titleSize || "1.0",
      titleArtistFont: a.titleArtistFont || "",
      titleSongFont: a.titleSongFont || "",
      titleSongBreak: a.titleSongBreak || "",
      segments: a.segments,
      transcribeJobId: a.transcribeJobId || null,
      status: "queued", current_step: null, progress: 0, job_id: null, error: null,
    }));
    setJobs(jobList);
    navigate("/generating");
    setReadyToGenerate(false);
    setApprovedJobs([]);
    // Audit 2026-06-09 ("siempre queda cargado en el wizard"): los inputs
    // ya fueron consumidos en jobList — si quedan en `files`/`reviewQueue`,
    // el próximo /new (sidebar, sin reset) muestra los audios del batch
    // ANTERIOR staged; el operador suma los nuevos, genera, y los viejos
    // se re-renderizan (videos duplicados + costo Veo duplicado). Limpiar
    // acá también vacía el snapshot de sesión (effect anyState), así el
    // banner "batch sin terminar" no ofrece retomar un batch ya generado.
    setFiles([]);
    setReviewQueue([]);

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
        // Escenas (multi-escena): el backend re-valida elegibilidad.
        // Multi-escena sólo con fondo generado por IA (no Biblioteca/Subir).
        if (enableScenes && bgSelectMode === "auto") formData.append("enable_scenes", "true");
        formData.append("text_case", jobList[i].textCase || "upper");
        formData.append("frame_format", jobList[i].frameFormat || "full");
        formData.append("font_scale", String(jobList[i].fontScale || "1.0"));
        // lyric_transition + text_motion: deprecados 2026-05-23 (no se envían).
        formData.append("lyrics_animation", jobList[i].lyricsAnimation || "none");
        formData.append("line_transition", jobList[i].lineTransition || "none");
        formData.append("lyric_color", jobList[i].lyricColor || "#FFFFFF");
        formData.append("lyric_sung_color", jobList[i].lyricSungColor || "#FFFFFF");
        formData.append("text_contrast", jobList[i].textContrast || "medium");
        // Title card customization (Full Rotor v1).
        formData.append("title_template", jobList[i].titleTemplate || "auto");
        formData.append("title_size", String(jobList[i].titleSize || "1.0"));
        formData.append("title_artist_font", jobList[i].titleArtistFont || "");
        formData.append("title_song_font", jobList[i].titleSongFont || "");
        formData.append("title_song_break", jobList[i].titleSongBreak || "");
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
        appendBackgroundFields(formData, {
          bgSelectMode, backgroundId, backgroundMode, backgroundFile, animateImage,
        });

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
          // Expired JWT: every remaining row would 401 the same way —
          // log out (dashboard behavior) instead of painting the whole
          // batch red with a misleading per-row error.
          if ((err?.status ?? err?.response?.status) === 401) {
            handleLogout("expired");
            return;
          }
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
        // Escenas (multi-escena): el backend re-valida elegibilidad.
        // Multi-escena sólo con fondo generado por IA (no Biblioteca/Subir).
        if (enableScenes && bgSelectMode === "auto") generateBody.append("enable_scenes", "true");
        generateBody.append("text_case", jobList[i].textCase || "upper");
        generateBody.append("frame_format", jobList[i].frameFormat || "full");
        generateBody.append("font_scale", String(jobList[i].fontScale || "1.0"));
        // lyric_transition + text_motion: deprecados 2026-05-23 (no se envían).
        generateBody.append("lyrics_animation", jobList[i].lyricsAnimation || "none");
        generateBody.append("line_transition", jobList[i].lineTransition || "none");
        generateBody.append("lyric_color", jobList[i].lyricColor || "#FFFFFF");
        generateBody.append("lyric_sung_color", jobList[i].lyricSungColor || "#FFFFFF");
        generateBody.append("text_contrast", jobList[i].textContrast || "medium");
        // Title card customization (Full Rotor v1).
        generateBody.append("title_template", jobList[i].titleTemplate || "auto");
        generateBody.append("title_size", String(jobList[i].titleSize || "1.0"));
        generateBody.append("title_artist_font", jobList[i].titleArtistFont || "");
        generateBody.append("title_song_font", jobList[i].titleSongFont || "");
        generateBody.append("title_song_break", jobList[i].titleSongBreak || "");
        generateBody.append("match_lyrics", String(!!inspiredByLyrics));
        appendBackgroundFields(generateBody, {
          bgSelectMode, backgroundId, backgroundMode, backgroundFile, animateImage,
        });

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

  // Art track submit: one multipart POST /generate per audio with the cover as
  // background_file + art_track=true + empty segments. No lyrics editor, no R2
  // two-step — the direct /generate path streams the audio to disk locally and
  // uploads it to R2 in prod (so the separate worker can fetch it), same as the
  // legacy direct create. The pipeline skips Whisper and composites the cover.
  const handleGenerateArtTrack = () => {
    if (!files.length || !files.every((f) => f.artist.trim())) return;
    if (!backgroundFile || typeof backgroundFile.slice !== "function") {
      alert({
        title: t("arttrack.cover_missing_title") || "Falta la portada",
        description: t("arttrack.cover_missing_desc") ||
          "Un art track necesita el cover del tema. Subí la imagen de portada.",
        tone: "warning",
      });
      return;
    }
    const jobList = files.map((f) => ({
      filename: f.file.name, _file: f.file, _cover: backgroundFile,
      artist: f.artist.trim(), songTitle: (f.songTitle || "").trim(),
      status: "queued", current_step: null,
      progress: 0, job_id: null, error: null,
    }));
    setJobs(jobList);
    navigate("/generating");
    setFiles([]);
    processArtTrackQueue(jobList);
  };

  const processArtTrackQueue = async (jobList) => {
    let nextIdx = 0;
    const worker = async () => {
      while (nextIdx < jobList.length) {
        const i = nextIdx++;
        setJobs((prev) => prev.map((j, idx) =>
          idx === i ? { ...j, status: "processing", current_step: "uploading", progress: 0 } : j
        ));
        const body = new FormData();
        body.append("file", jobList[i]._file, jobList[i].filename);
        body.append("background_file", jobList[i]._cover,
                    jobList[i]._cover.name || "cover.jpg");
        body.append("artist", jobList[i].artist);
        if (jobList[i].songTitle) body.append("song_title", jobList[i].songTitle);
        body.append("segments_json", "[]");
        body.append("art_track", "true");
        if ((delivery.label_line || "").trim()) {
          body.append("label_line", delivery.label_line.trim());
        }
        if ((delivery.effect || "").trim()) {
          body.append("effect", delivery.effect.trim());
        }
        body.append("delivery_profile", delivery.delivery_profile);
        if (delivery.delivery_profile !== "youtube") {
          body.append("umg_frame_size", delivery.umg_frame_size);
          body.append("umg_fps", String(delivery.umg_fps));
          body.append("umg_prores_profile", String(delivery.umg_prores_profile));
        }

        let genRes = null;
        try {
          genRes = await authFetch(`${API}/generate`, { method: "POST", body });
          let data;
          try {
            data = await genRes.json();
          } catch {
            const reason = await describeFetchError(null, genRes, t);
            setJobs((prev) => prev.map((j, idx) =>
              idx === i ? { ...j, status: "error", error: reason } : j));
            continue;
          }
          if (!genRes.ok || data.detail) {
            if ((genRes.status ?? 0) === 401) { handleLogout("expired"); return; }
            const reason = data.detail || await describeFetchError(null, genRes, t);
            setJobs((prev) => prev.map((j, idx) =>
              idx === i ? { ...j, status: "error", error: reason } : j));
            continue;
          }
          setJobs((prev) => prev.map((j, idx) =>
            idx === i ? { ...j, current_step: "video", progress: 0, job_id: data.job_id } : j));
          await pollJob(data.job_id);
        } catch (err) {
          if ((err?.status ?? err?.response?.status) === 401) { handleLogout("expired"); return; }
          const reason = await describeFetchError(err, genRes, t);
          setJobs((prev) => prev.map((j, idx) =>
            idx === i ? { ...j, status: "error", error: reason } : j));
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
    setBgSelectMode("auto"); setAnimateImage(false); setEnableScenes(false);
    // Volver a lyric video en "Descartar todo": sin esto artTrack queda true y
    // el self-heal fuerza bgSelectMode a "custom" de nuevo, dejando el wizard
    // en modo art track sin cover.
    setArtTrack(false);
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
  // Mirror sincrónico editor → currentReview. Auditoría 2026-06-10: la
  // versión anterior era un arrow INLINE en el JSX — identidad nueva en
  // cada render de App. Como LyricsEditor lo tiene en las deps de su
  // effect espejo, cada render re-corría el effect → setCurrentReview con
  // objeto nuevo → otro render de App → loop perpetuo (confirmado
  // empíricamente: ~5000 ciclos/100ms). El loop saturaba el main thread
  // (drags y taps caían en frames perdidos — parte del "se mueve y no me
  // deja"), ensanchaba la ventana de la race del autosave y cancelaba en
  // cada ciclo el requestIdleCallback de wizardPersistence.save.
  // Dos cortes: identidad estable (useCallback) + no-op cuando los
  // valores no cambiaron (devolver el MISMO objeto corta el re-render).
  const handleEditedChange = useCallback((segs) => {
    setCurrentReview((r) => mergeEditedSegments(r, segs));
  }, []);

  const handleBackInReview = () => {
    if (approvedJobs.length > 0) {
      const last = approvedJobs[approvedJobs.length - 1];
      setApprovedJobs(approvedJobs.slice(0, -1));
      setCurrentReview({
        file: last.file,
        artist: last.artist,
        // Auditoría 2026-06-10: SIN transcribeJobId el autosave del editor
        // queda mudo (guard en LyricsEditor: no persiste sin jobId) — todo
        // lo que la operadora retocara al volver atrás se perdía en
        // silencio. El entry de approvedJobs siempre lo trae.
        transcribeJobId: last.transcribeJobId || null,
        songTitle: last.songTitle || "",
        bgCacheKey: last.bgCacheKey || null,
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
    // Auditoría 2026-06-10: al volver desde la PRIMERA canción, el
    // prefetchCache conserva la transcripción ORIGINAL — si la operadora
    // re-entra a review, transcribeNext la sirve y pisa visualmente todo
    // lo sincronizado (el "tengo que volver a comenzar"). Refrescamos el
    // cache con los segments vigentes antes de soltar la review.
    try {
      if (currentReview?.file && Array.isArray(currentReview.segments)) {
        const k = prefetchKey(currentReview.file);
        const cached = prefetchCache.current[k];
        if (cached?.status === "ready" && cached.data) {
          cached.data = { ...cached.data, segments: currentReview.segments };
        }
      }
    } catch { /* best-effort: el cache es una optimización, no la verdad */ }
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
    // Double-click guard. See generateLockRef declaration above for the
    // race this closes. Lock released by startGenerationWithSegments
    // when it finishes kicking off the per-job /generate POSTs (it
    // navigates to /generating, so the second click would also try to
    // navigate from an already-navigated page; cleaner to short-circuit
    // here than to let two parallel POSTs collide in the API).
    if (generateLockRef.current) {
      return;
    }
    // El guard de fondo custom faltante corre ANTES de ocultar el resumen:
    // si aborta, el operador sigue viendo "Crear N videos" y puede
    // corregir el fondo sin perder el estado del batch.
    if (bgSelectMode === "custom" && (!backgroundFile || typeof backgroundFile.slice !== "function")) {
      alert({
        title: t("wizard.custom_bg_missing_title") || "Falta el fondo",
        description: t("wizard.custom_bg_missing_desc") ||
          "Elegiste \"Upload\" como fondo pero no hay ningún archivo cargado. Subí tu imagen o video en el paso Modo, o cambiá a \"Generar con IA\".",
        tone: "warning",
      });
      return;
    }
    generateLockRef.current = true;
    setReadyToGenerate(false);
    // No tocamos wizardStage acá — startGenerationWithSegments navega a
    // /generating (pantalla dedicada de progreso). El wizard queda
    // "stale" pero handleReset lo limpia cuando el operator vuelve.
    Promise.resolve(startGenerationWithSegments(approvedJobs)).finally(() => {
      // Release the lock after the start dance settles. Failures should
      // also release so the operator can retry (the underlying error UI
      // takes over the screen — the button itself is hidden by then).
      generateLockRef.current = false;
    });
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

  const handleSearchSelectJob = (jobId, status) => {
    const onWizardRoute = ["/new", "/review", "/generating"].includes(location.pathname);
    if (onWizardRoute && wizardPersistence.hasResumableContent(wizardPersistence.load())) {
      const msg =
        t("wizard.confirm_leave") ||
        "Tenés un batch en progreso. Si te vas, podés retomarlo al volver desde el banner amarillo, pero perdés el contexto actual. ¿Continuar?";
      if (!window.confirm(msg)) return false;
    }
    handleSelectJob(jobId, status);
    return true;
  };

  const handleStartNewBatch = () => {
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
    setBackgroundFile(null);
    setBackgroundId(null);
    setBgSelectMode("auto");
    setAnimateImage(false);
    setEnableScenes(false);
    wizardPersistence.clear();
    navigate("/new");
  };

  const handleBulkApproveBatch = async (jobIds) => {
    if (!Array.isArray(jobIds) || jobIds.length === 0) return;
    const failed = [];
    for (const jobId of jobIds) {
      try {
        const res = await authFetch(`${API}/approve/${jobId}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ notes: "" }),
        });
        if (!res.ok) {
          const data = await res.json().catch(() => ({}));
          failed.push({ jobId, reason: data.detail || `Error ${res.status}` });
          continue;
        }
        setJobs((prev) =>
          prev.map((j) => j.job_id === jobId ? { ...j, status: "done" } : j)
        );
      } catch (err) {
        failed.push({ jobId, reason: err?.message || t("common.network_error") });
      }
    }
    await fetchHistory();
    if (failed.length > 0) {
      const firstFailure = failed[0];
      alert({
        title: t("history.bulk_approve_partial_title"),
        description: `${failed.length}/${jobIds.length} ${t("history.bulk_approve_partial_description")} ${firstFailure.jobId}: ${firstFailure.reason}`,
        tone: "error",
      });
    }
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
    // Mismo gate por bgSelectMode que el payload de /generate (bgPayload.js):
    // un backgroundFile residual con el tab en "IA" no debe alterar los
    // params del pre-gen — el hash saldría distinto al del render real.
    animateImage: bgSelectMode === "custom" && animateImage && !!backgroundFile,
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
    // QA fix 2026-05-27: en edit mode el job ya tiene bg_r2_key_cached
    // poblado; pre-generar otra vez muestra el chip "Generando fondo en
    // background…" sobre un fondo que ya existe (ruido visual + costo
    // Gemini gratuito). Si el operador clickea "Editar y re-renderizar"
    // con cambio de background, ese flow dispara su propio re-render
    // via /edit/{id} con edit_type=background — no necesita el preview.
    enabled: shouldEnableBackgroundPreview({
      hasReview: !!currentReview,
      editMode: !!currentReview?.editMode,
      bgSelectMode,
      enableScenes,
    }),
    api: API,
    authHeaders,
    onCacheKey: (key, meta) => {
      // Audit adversarial 2026-06-09: si el hook marca la respuesta como
      // stale (los params cambiaron mientras el fetch volaba), el key NO
      // puede tocar la review actual — currentReview puede ser ya OTRA
      // canción y le estaríamos cruzando el fondo. Solo sirve para el
      // backfill de abajo, que matchea por filename del entry original.
      if (!meta?.stale) {
        // Update currentReview si aún estamos editando ese file.
        setCurrentReview((r) => (r ? { ...r, bgCacheKey: key } : r));
      }
      if (key == null) return;
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
          // Solo completar entries SIN key: un job aprobado ya copió el
          // key que le correspondía al aprobarse; pisarlo acá habilita
          // last-writer-wins entre canciones con el mismo filename.
          if (j.bgCacheKey) return j;
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

  // Edit-mode metadata banner (PR feat/edit-wizard-mode, 2026-05-27):
  // when EditLyricsRoute mounted us in edit mode, show artist+song_title
  // inputs at the top of the wizard so the operator can fix a typo in the
  // title card without leaving the editor. Writes to currentReview so the
  // diff in handleApproveLyrics picks them up via the metadata bucket.
  // Hidden on regular new-job flow (currentReview?.editMode === undefined).
  // QA fix 2026-05-28 (UX polish): banner más compacto. Antes tenía 3
  // bloques verticales (header + 2 inputs + hint) con padding generoso
  // → ocupaba ~150 px. Ahora los inputs se integran horizontalmente con
  // las labels chips, el hint pasa a sublabel del header, y el icono
  // sube a tamaño 14 con un wrapper pill que da identidad visual sin
  // gritar. Reducimos altura total a ~88 px → +60 px para preview en
  // viewport.
  const editingHeaderBanner = currentReview?.editMode ? (
    <div className="rounded-card bg-brand/[0.06] ring-1 ring-brand/30 px-4 py-3 mb-4 animate-fade-in">
      <div className="flex items-start gap-3 flex-wrap">
        <div className="flex items-center gap-2 shrink-0 pt-1.5">
          <span className="w-6 h-6 rounded-lg bg-brand/15 ring-1 ring-brand/30 flex items-center justify-center">
            <svg className="w-3.5 h-3.5 text-brand-light" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
              <path d="M11 4H4a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7" strokeLinecap="round" strokeLinejoin="round" />
              <path d="M18.5 2.5a2.121 2.121 0 013 3L12 15l-4 1 1-4 9.5-9.5z" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </span>
          <div className="flex flex-col leading-tight">
            <span className="text-[11px] uppercase tracking-[0.18em] text-brand-light font-medium">
              {t("editor.editing_banner_label") || "Editando este video"}
            </span>
            <span className="text-[10px] text-gray-500 mt-0.5">
              {t("editor.editing_banner_hint_short") || "Lo que no toques queda igual"}
            </span>
          </div>
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-2 flex-1 min-w-[260px]">
          <div className="flex items-center gap-2">
            <span className="text-[10px] uppercase tracking-wider text-gray-500 w-12 shrink-0">
              {t("upload.artist") || "Artista"}
            </span>
            <input
              type="text"
              value={currentReview.artist || ""}
              onChange={(e) => setCurrentReview((r) => (r ? { ...r, artist: e.target.value } : r))}
              placeholder={t("upload.artist_placeholder") || "Ej: Viejas Locas"}
              maxLength={255}
              className="flex-1 rounded-lg bg-surface-1 border border-white/[0.08] focus:border-brand/50 focus:ring-2 focus:ring-brand/20 px-3 py-1.5 text-sm text-gray-100 placeholder:text-gray-600 outline-none transition-all"
              aria-label={t("editor.editing_artist") || "Editar artista"}
            />
          </div>
          <div className="flex items-center gap-2">
            <span className="text-[10px] uppercase tracking-wider text-gray-500 w-12 shrink-0">
              {t("upload.song_title_short") || "Título"}
            </span>
            <input
              type="text"
              value={currentReview.songTitle || ""}
              onChange={(e) => setCurrentReview((r) => (r ? { ...r, songTitle: e.target.value } : r))}
              placeholder={t("upload.song_title_placeholder") || "Ej: Legalícenla"}
              maxLength={500}
              className="flex-1 rounded-lg bg-surface-1 border border-white/[0.08] focus:border-brand/50 focus:ring-2 focus:ring-brand/20 px-3 py-1.5 text-sm text-gray-100 placeholder:text-gray-600 outline-none transition-all"
              aria-label={t("editor.editing_title") || "Editar título"}
            />
          </div>
        </div>
        {/* UI v1.1 (2026-05-30): the live title-card preview used to live
            here as a 320 px box next to the artist/song inputs — it looked
            "stretched and lost" because the box was static while the editor
            grew. We removed it: the title card is now previewed in the
            CENTRAL sticky preview (toggle Letra / Portada inside
            UploadZone). The artist + song inputs still feed the preview
            because UploadZone reads them through titlePreviewArtist /
            titlePreviewSong props plumbed below. */}
      </div>
    </div>
  ) : null;

  const newBatchScreen = (
    // QA fix 2026-05-28 (UX, scroll architecture): pre-fix el page-scroll
    // viajaba por todo el contenido (header banners + grid del wizard) y
    // el operador, al scrollear lyrics, perdía el banner "Editando este
    // video" + el batch banner + a veces incluso el stepper/preview. Solo
    // habían sticky-top-4 en las dos columnas, no en los headers.
    //
    // Approach: en lg+ el container se ajusta a viewport-height (descontando
    // la altura del top bar de App, ~72 px aprox). Los banners pasan a
    // `lg:shrink-0` (toman su altura natural) y el wrapper de UploadZone
    // toma `lg:flex-1 lg:min-h-0` para llenar el resto. Adentro, la grid
    // del wizard se convierte en su propio scroll context — el panel
    // derecho es el único que scrollea internamente. Mobile (<lg) mantiene
    // el page-scroll histórico sin cambios.
    //
    // 100dvh evita el problema del 100vh en mobile-Safari donde el chrome
    // shifting (URL bar) hace que 100vh sea más grande que el viewport
    // visible; en desktop el comportamiento es idéntico.
    <div className="w-full max-w-[1700px] mx-auto animate-fade-in lg:h-[calc(100dvh-72px)] lg:flex lg:flex-col lg:overflow-hidden">
      <div className="flex items-center gap-3 mb-8 lg:mb-6 lg:shrink-0">
        <button onClick={() => navigate("/dashboard")}
          className="w-9 h-9 rounded-xl glass flex items-center justify-center text-gray-400 hover:text-white transition-colors">
          <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
            <path d="M19 12H5M12 19l-7-7 7-7" />
          </svg>
        </button>
        <div>
          <h1 className="text-2xl font-bold">
            {currentReview?.editMode
              ? (t("editor.editing_wizard_title") || "Editar y re-renderizar")
              : t("upload.new_batch")}
          </h1>
          <p className="text-sm text-gray-500">
            {currentReview?.editMode
              ? (t("editor.editing_wizard_sub") ||
                  "Corregí cualquier campo y re-renderizá. Lo que no cambies queda igual.")
              : t("upload.new_batch_sub")}
          </p>
        </div>
      </div>

      <div className="lg:shrink-0">{editingHeaderBanner}</div>

      <div className="lg:shrink-0">{resumeBanner}</div>

      <div className="lg:flex-1 lg:min-h-0 lg:overflow-hidden lg:flex lg:flex-col">
      <Suspense fallback={<RouteSuspenseFallback />}>
      <UploadZone
        files={files}
        onFiles={setFiles}
        delivery={delivery}
        onDeliveryChange={setDelivery}
        style={style}
        onStyleChange={setStyle}
        customColors={customColors}
        onCustomColorsChange={setCustomColors}
        enableScenes={enableScenes}
        onEnableScenesChange={setEnableScenes}
        artTrack={artTrack}
        onArtTrackChange={(on) => {
          setArtTrack(on);
          // Art track = cover subido + sin Escenas. Forzar modo "custom" para
          // que aparezca el uploader del cover; limpiar Escenas (incompatible).
          if (on) {
            setBgSelectMode("custom"); setEnableScenes(false);
          } else {
            // Al volver a Lyric Video: restaurar el fondo AI por defecto y
            // soltar el cover subido. Sin esto, bgSelectMode queda "custom" y
            // el lyric video se rendería con el cover como fondo (regresión).
            setBgSelectMode("auto"); setBackgroundFile(null);
          }
        }}
        onGenerateArtTrack={handleGenerateArtTrack}
        backgroundFile={backgroundFile}
        onBackgroundFile={setBackgroundFile}
        backgroundId={backgroundId}
        onBackgroundId={setBackgroundId}
        backgroundMode={backgroundMode}
        onBackgroundMode={setBackgroundMode}
        bgMode={bgSelectMode}
        onBgMode={setBgSelectMode}
        animateImage={animateImage}
        onAnimateImage={setAnimateImage}
        inspiredByLyrics={inspiredByLyrics}
        onInspiredByLyricsChange={setInspiredByLyrics}
        allHaveArtist={allHaveArtist}
        onStartReview={handleStartReview}
        onGenerateDirect={handleGenerateDirect}
        user={user}
        sidebarOpen={sidebarOpen}
        // Prefetch de transcripción al avanzar del paso "Subí" (no al drop):
        // la fuente de letra + la letra oficial ya están resueltas por
        // canción, así el POST sale con anchor_lyrics correcto.
        onUploadAdvance={handleUploadAdvance}
        // Edge case (a): editar fuente/letra tras avanzar invalida el
        // prefetch de esa canción para que se re-transcriba.
        onInvalidatePrefetch={invalidatePrefetchForFile}
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
        // 2026-07-16: callback ref para el slot del player bar bajo el video.
        onPlayerSlotRef={setPlayerSlotEl}
        // Post-render edit (EditLyricsRoute): el wizard se monta sobre un
        // job ya renderizado. QA fix 2026-05-27: bajamos los locks de
        // [1, 2, 3, 5] a [1, 5] — solo file upload (paso 1) y delivery
        // profile (paso 5) son verdaderamente structural (audio fijo, no
        // se puede cambiar formato sin regenerar todo). Pasos 2 (Modo) y
        // 3 (Movimiento) ahora son navegables: el operador puede editar
        // background_hint, bg_verbatim, scene mode, movement_style y
        // effect. El style picker (paleta) dentro de step 2 queda
        // lockeado a nivel control via `editMode` + overlay — no a nivel
        // step, así el resto de step 2 sí es interactivo.
        lockedSteps={currentReview?.editMode ? [1, 5] : []}
        editMode={!!currentReview?.editMode}
        // QA fix 2026-05-27: en edit mode los controles del wizard
        // (step 2 background_hint/bg_verbatim, step 3 movement/effect,
        // step 4 typography) escriben a batchDefaults + files via
        // updateBatchDefault. Como files=[] en edit mode, el fan-out es
        // no-op y los cambios no llegan al diff de handleApproveLyrics.
        // Este callback los forward a currentReview con el mismo nombre
        // de field (batchDefaults y currentReview usan camelCase con las
        // mismas keys: backgroundHint, bgVerbatim, movementStyle,
        // effect, font, textCase, fontScale, textContrast,
        // lyricsAnimation, lineTransition, lyricColor, lyricSungColor).
        onEditFieldChange={(field, value) =>
          setCurrentReview((r) => (r ? { ...r, [field]: value } : r))
        }
        // UI v1.1 (2026-05-30): feed the central title-card preview with the
        // currently-active artist/song. In edit mode the canonical source is
        // currentReview (the operator can edit them in the banner inputs
        // above); in batch mode we pick the first file as a representative.
        // Empty strings render the "—" placeholder in TitleCardPreview.
        titlePreviewArtist={
          currentReview?.artist ?? (files?.[0]?.artist || "")
        }
        titlePreviewSong={
          currentReview?.songTitle ?? (files?.[0]?.songTitle || "")
        }
        renderedVideoUrl={editingRenderedVideoUrl || null}
        // UI F5 (2026-05-26): le pasamos el bgStatus al wizard para que
        // UploadZone pueda derivar `placeholderBg` cuando montamos el
        // preview en paso 6. "done" = fondo final listo, todo lo demás
        // = muestra. El badge del preview cambia de "EN VIVO" a
        // "(muestra)" en consecuencia.
        bgStatus={bgPreview.status}
      />
      </Suspense>
      </div>
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
            <div className="mt-3 flex items-center justify-center gap-4 flex-wrap">
              {transcribeRetryCtx.current && (
                <button
                  onClick={() => {
                    const ctx = transcribeRetryCtx.current;
                    setTranscribeError(null);
                    transcribeRetryCtx.current = null;
                    transcribeNext(ctx.queue, ctx.idx, ctx.reuseJobId || null);
                  }}
                  className="text-xs text-brand hover:text-brand-light transition-colors font-medium"
                >
                  {t("upload.retry") || "Reintentar"}
                </button>
              )}
              {/* QA fix 2026-05-28 (audit P0 #77): cuando el job está en
                  background procesándose (timeout del front pero backend
                  sigue trabajando), el operador necesita un link directo
                  al historial. */}
              <button
                onClick={() => { setTranscribeError(null); navigate("/dashboard"); }}
                className="text-xs text-gray-300 hover:text-white transition-colors font-medium underline"
              >
                {t("transcribe.go_to_dashboard") || "Ver mi historial"}
              </button>
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
          <Suspense fallback={<EditorSuspenseFallback />}>
          <LyricsEditor
            // 2026-07-16: cuando el wizard pasa un slot (bajo el video), el
            // player bar se portalea ahí; null (modal/inline) → inline.
            playerSlot={playerSlotEl}
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
            //
            // 2026-05-31 (Agus batch upload bug): the previous key was
            // `filename:queueIdx`. In batch upload, when two songs are
            // queued at consecutive indices and `currentReview` is
            // swapped between them, the queueIdx changes but the
            // filename can transiently match (early state where the new
            // job's filename hasn't propagated yet), or the editor
            // remounts to the wrong song if both queue entries point at
            // the same R2 upload. Including `transcribeJobId` makes the
            // key identity-stable per backend job — the only correct
            // notion of "what song is this". Confirmed against the live
            // DB: job 82a5a8ab547e ("Donde Estan Corazón",
            // segments_json=null) was shown with the segments of job
            // 9df1132f6169 ("Luz de día") that preceded it in the same
            // session. With the jobId in the key, that swap forces an
            // unmount and `edited` re-seeds from the new (empty/loading)
            // segments instead of pinning the previous song's text.
            key={`${currentReview.transcribeJobId || "no-job"}:${currentReview.file?.name || currentReview.filename || "resume"}:${currentReview.queueIdx}`}
            // QA fix 2026-05-28 (scroll architecture): pre-fix usaba 72 para
            // clear el top bar de App porque el editor scrolleaba con el
            // page-scroll y el sticky-top-72 ponía el audio bar JUSTO
            // abajo del top bar de App. Con el nuevo wizard layout
            // viewport-bound, el editor monta dentro de la columna RIGHT
            // de UploadZone (su propio scroll container, h-full). El top
            // bar de App ya no afecta visualmente — el sticky del audio
            // bar interno es relativo al scroll de la columna RIGHT, no
            // al viewport. top=0 lo pega al top de su scroll container,
            // que es justo abajo del banner stack del wizard. Sin cambio
            // visual perceptible para el operador, pero conceptualmente
            // correcto en el nuevo layout.
            stickyHeaderTop={0}
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
            onReanchor={reanchorSegmentsOnBackend}
            // QA fix 2026-05-28 (bug #2): synchronous mirror del estado
            // local del LyricsEditor a currentReview.segments para que el
            // WizardLivePreview central (que lee `reviewSegments`) refleje
            // los drag-resize de timings sin esperar al autosave de 3s +
            // network roundtrip. Auditoría 2026-06-10: memoizado + guard de
            // no-op — ver handleEditedChange (el arrow inline acá causaba
            // un loop perpetuo de re-renders App↔editor).
            onEditedChange={handleEditedChange}
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
          </Suspense>
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
                <span className="text-sm text-white truncate flex-1">{((job.file && job.file.name) || "audio.mp3").replace(/\.mp3$/i, "")}</span>
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
      {user && <WhatsNewModal user={user} />}
      <Routes>
        <Route
          path="/"
          element={
            token
              ? <Navigate to="/dashboard" replace />
              : (
                /* 2026-05-30 perf: Landing is lazy. Suspense fallback is
                   null because the landing render itself is the page —
                   showing a "loading…" flicker would feel worse than a
                   blank ~80 ms while the chunk parses. */
                <Suspense fallback={null}>
                  <Landing
                    onStart={() => navigate("/login")}
                    onLogin={() => navigate("/login")}
                    isLoggedIn={false}
                  />
                </Suspense>
              )
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
                history={history}
                sidebarOpen={sidebarOpen}
                setSidebarOpen={setSidebarOpen}
                onLogout={handleLogout}
                onOpenSearch={() => setSearchOpen(true)}
                onStartNewBatch={handleStartNewBatch}
              />
            </RequireAuth>
          }
        >
          <Route path="/dashboard" element={
            <Suspense fallback={<RouteSuspenseFallback />}>
              <Dashboard
                user={user}
                history={history}
                historyError={historyError}
                historyLoaded={historyLoaded}
                onRetryHistory={fetchHistory}
                onSelectJob={handleSelectJob}
                onNewBatch={handleStartNewBatch}
                onViewHistory={() => navigate("/videos")}
              />
            </Suspense>
          } />
          {/* Capa B 2026-05-24 — /new y /review renderizan el MISMO content
              (wizardScreen) que conmuta upload ↔ review ↔ ready_to_generate
              vía wizardStage. /review se mantiene como ruta válida sólo para
              compat con bookmarks viejos; URL nueva canónica es /new. */}
          <Route path="/new" element={wizardScreen} />
          <Route path="/review" element={wizardScreen} />
          <Route path="/generating" element={generatingScreen} />
          <Route path="/videos" element={
            <Suspense fallback={<RouteSuspenseFallback />}>
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
            </Suspense>
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
              setWizardStage={setWizardStage}
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
          <Route path="/account" element={
            <Suspense fallback={<RouteSuspenseFallback />}>
              <Settings onBack={() => navigate("/dashboard")} />
            </Suspense>
          } />
          <Route path="/admin" element={
            user?.role === "admin"
              ? (
                <Suspense fallback={<RouteSuspenseFallback />}>
                  <AdminPanel
                    onBack={() => navigate("/dashboard")}
                    isSuperAdmin={Boolean(user?.is_super_admin)}
                  />
                </Suspense>
              )
              : <Navigate to="/dashboard" replace />
          } />
        </Route>
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>

      {/* 2026-05-25 PR-2 — Command palette ⌘K. Renderizado fuera de
          <Routes> para que sobreviva navegación entre rutas. El listener
          de teclado global vive en el GlobalSearchKeybinding helper. */}
      {searchOpen && (
        <Suspense fallback={null}>
          <SearchPalette
            isOpen={searchOpen}
            onClose={() => setSearchOpen(false)}
            jobs={history}
            onSelectJob={handleSearchSelectJob}
          />
        </Suspense>
      )}
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
