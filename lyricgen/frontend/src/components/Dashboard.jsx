import { useState, useEffect, useMemo } from "react";
import { useNavigate } from "react-router-dom";
import { useI18n } from "../i18n";
import { useMediaUrl } from "../mediaUrl";
import { fetchWithTimeout } from "../fetchWithTimeout";
import { DashboardTour } from "./OnboardingTour";
import ProResBadge from "./ProResBadge";
import { SkeletonVideoCard } from "./Skeleton";
import DashboardStepper from "./DashboardRich/Stepper";
import FormatGallery from "./DashboardRich/FormatGallery";
import "./DashboardRich/DashboardRich.css";

// sessionStorage key the wizard reads on mount to pre-apply a delivery
// profile / short flag picked from the FormatGallery on home. Keeps the
// coupling loose: UploadZone consumes if present, ignores if not.
export const FORMAT_PRESET_KEY = "genly_format_preset";

const API = import.meta.env.VITE_API_URL || "";

// 2026-05-27 perf audit: module-level formatters so the date header's
// IIFE doesn't `new Intl.DateTimeFormat()` on every render. Creating
// a DateTimeFormat is ~5-10 ms in Chrome — fine once, but adds up at
// 3-5 renders/sec while the polling loop is ticking.
const DATE_HEADER_FMT = new Intl.DateTimeFormat("es-AR", {
  weekday: "long", day: "numeric", month: "long",
});
const MONTH_FMT = new Intl.DateTimeFormat("es-AR", { month: "long" });

function authHeaders() {
  const token = localStorage.getItem("genly_token");
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function timeAgo(ts) {
  if (!ts) return "";
  const diff = Date.now() / 1000 - ts;
  if (diff < 60) return "ahora";
  if (diff < 3600) return `${Math.floor(diff / 60)}m`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h`;
  return `${Math.floor(diff / 86400)}d`;
}

// Tiny uppercase label used to introduce sections — Linear / Vercel style.
// `text-section` token (tailwind.config) ya incluye fontWeight 600 +
// letter-spacing 0.18em + size 10px. Antes era arbitrary; el token lo unifica.
function SectionLabel({ children }) {
  return (
    <p className="text-section text-gray-500 uppercase mb-3">
      {children}
    </p>
  );
}

function ProcessingRow({ job, onSelect, t }) {
  return (
    <button
      onClick={() => onSelect(job.job_id)}
      className="w-full flex items-center gap-3 px-3 py-2.5 rounded-xl hover:bg-surface-2/60 transition-colors text-left"
    >
      <div className="relative w-2 h-2 shrink-0">
        <div className="absolute inset-0 rounded-full bg-brand animate-ping opacity-60" />
        <div className="relative w-2 h-2 rounded-full bg-brand" />
      </div>
      <span className="text-sm text-white truncate flex-1">
        {(job.filename || "").replace(/\.mp3$/i, "")}
      </span>
      <span className="text-[11px] text-gray-500 shrink-0">
        {job.status === "queued"
          ? (t("dash.queued") || "En cola")
          : job.status === "editing"
            ? (t("dash.editing") || "Re-renderizando")
            : t("dash.processing")}
      </span>
    </button>
  );
}

function VideoCard({ job, onSelect }) {
  const name = (job.filename || "").replace(/\.mp3$/i, "");
  const songName = name.includes(" - ") ? name.split(" - ").slice(1).join(" - ") : name;
  const artistName = job.artist || (name.includes(" - ") ? name.split(" - ")[0] : "");
  const thumbSrc = useMediaUrl(job.job_id, "thumbnail", "preview");

  return (
    <button
      onClick={() => onSelect(job.job_id, job.status)}
      className="rounded-card overflow-hidden text-left group bg-surface-2/40 hover:bg-surface-2/70 ring-1 ring-white/[0.04] hover:ring-white/[0.10] transition-all"
    >
      <div className="aspect-video bg-surface-3/30 relative overflow-hidden">
        {thumbSrc && (
          <img
            src={thumbSrc}
            alt=""
            className="w-full h-full object-cover group-hover:scale-[1.04] transition-transform duration-500"
            onError={(e) => { e.target.style.display = "none"; }}
          />
        )}
        <div className="absolute inset-0 flex items-center justify-center opacity-30 sm:opacity-0 sm:group-hover:opacity-100 transition-opacity bg-black/30">
          <div className="w-10 h-10 rounded-full bg-white/15 backdrop-blur-md flex items-center justify-center ring-1 ring-white/20">
            <svg className="w-4 h-4 text-white ml-0.5" fill="currentColor" viewBox="0 0 24 24">
              <path d="M8 5v14l11-7z"/>
            </svg>
          </div>
        </div>
      </div>
      <div className="px-3.5 py-3">
        <div className="flex items-start gap-2 min-w-0">
          <p className="text-[13px] font-medium text-white truncate flex-1 min-w-0">{songName || "Sin nombre"}</p>
          <ProResBadge
            deliveryProfile={job.delivery_profile}
            proresReady={job.prores_ready}
            jobStatus={job.status}
          />
        </div>
        <p className="text-[11px] text-gray-500 truncate mt-0.5">
          {artistName}
          {job.created_at && <span className="ml-1.5 text-gray-600">· {timeAgo(job.created_at)}</span>}
        </p>
      </div>
    </button>
  );
}

export default function Dashboard({ user, history, historyError, historyLoaded = true, onRetryHistory, onSelectJob, onNewBatch, onViewHistory, onOpenSearch }) {
  const { t } = useI18n();
  const navigate = useNavigate();

  // FormatGallery handlers.
  // - "youtube"/"prores"/"thumbnail" cards: stash the preset in sessionStorage
  //   so UploadZone (or whoever consumes /new next) can read it once on mount
  //   and clear it. Then trigger the regular new-batch navigation.
  // - "short" today is not a separate delivery profile — every job already
  //   produces an MP4 + short bundle. We still pre-fill the preset so the
  //   wizard can later decide to highlight short-related options.
  // - Locked ProRes (free plan) routes to billing instead.
  const handleSelectFormat = (fmt) => {
    try {
      const preset = { id: fmt.id, profile: fmt.profile, subType: fmt.subType || null };
      sessionStorage.setItem(FORMAT_PRESET_KEY, JSON.stringify(preset));
    } catch {}
    if (typeof onNewBatch === "function") onNewBatch();
    else navigate("/new");
  };
  const handleUpgrade = () => {
    navigate("/account");
  };

  // 2026-05-27 perf audit (UMG micro-freezes): four `history.filter()`
  // calls re-ran on EVERY render — including every SSE poll tick (every
  // 3-5 s during a generation). For a tenant with 200 jobs that's
  // 4 × 200 = 800 string comparisons per poll × 5 = 4000/sec just for
  // re-bucketing. Memoizing on `history` reduces this to one pass per
  // actual change (when a job status mutates), saving ~50-60 ms of
  // main-thread work per poll tick.
  const pendingReview = useMemo(
    () => history.filter((h) => h.status === "pending_review"),
    [history],
  );
  // "editing" jobs are mid edit-request re-render — UX-wise they're the
  // same as the initial processing state (worker is rendering, user can't
  // approve yet), so we bucket them together with processing/queued.
  const processing = useMemo(
    () => history.filter(
      (h) => h.status === "processing" || h.status === "queued" || h.status === "editing"
    ),
    [history],
  );
  const recentDone = useMemo(
    () => history.filter((h) => h.status === "done").slice(0, 6),
    [history],
  );
  const errors = useMemo(
    () => history.filter((h) => h.status === "error" || h.status === "validation_failed"),
    [history],
  );

  // Real plan usage from API. We surface load failures so the operator
  // doesn't sit on "cargando..." forever when /usage hangs (CORS,
  // backend cold start, transient 5xx). 10 s timeout + a retry button
  // covers the rare case; on success the error state clears itself.
  const [usage, setUsage] = useState(null);
  const [usageError, setUsageError] = useState(false);
  const [usageRetryNonce, setUsageRetryNonce] = useState(0);
  useEffect(() => {
    let cancelled = false;
    setUsageError(false);
    fetchWithTimeout(`${API}/usage`, { headers: authHeaders() })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((data) => { if (!cancelled) setUsage(data); })
      .catch(() => { if (!cancelled) setUsageError(true); });
    return () => { cancelled = true; };
  }, [history.length, usageRetryNonce]);
  const retryUsage = () => setUsageRetryNonce((n) => n + 1);

  // Errors banner is dismissible. We persist the count at dismiss time so
  // the banner re-surfaces only when *new* errors arrive (otherwise the
  // operator would have to dismiss it every page load until next month).
  const errorsKey = (() => {
    const d = new Date();
    return `dash_errors_dismissed_${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
  })();
  const [errorsDismissedAt, setErrorsDismissedAt] = useState(() => {
    const v = localStorage.getItem(errorsKey);
    return v ? parseInt(v, 10) : 0;
  });
  const errorsBannerVisible = errors.length > errorsDismissedAt;
  const dismissErrors = () => {
    localStorage.setItem(errorsKey, String(errors.length));
    setErrorsDismissedAt(errors.length);
  };

  const monthlyLimit = usage?.limit ?? null;
  const monthlyUsed = usage?.used ?? 0;
  const isUnlimited = usage?.plan === "unlimited" || (monthlyLimit && monthlyLimit >= 999999);
  const usagePercent = isUnlimited
    ? 0
    : (usage?.percent ?? (monthlyLimit ? Math.min(100, (monthlyUsed / monthlyLimit) * 100) : 0));

  const greeting = (() => {
    const h = new Date().getHours();
    if (h < 12) return "Buenos días";
    if (h < 19) return "Buenas tardes";
    return "Buenas noches";
  })();
  const firstName = user?.username || "";

  const monthlySubtitle = (() => {
    if (history.length === 0) return "Subí tu primer audio para empezar";
    if (monthlyUsed === 0) return "Aún no completaste videos este mes";
    return `${monthlyUsed} ${monthlyUsed === 1 ? "video listo" : "videos listos"} este mes`;
  })();

  // 2026-05-25 — Atención drawer state. Los banners de quota+errors
  // (antes apilados arriba compitiendo con el hero) ahora viven adentro
  // de un drawer colapsable a la derecha del hero. Solo se expande
  // cuando hay algo que mostrar Y el operador lo abre.
  const [attentionOpen, setAttentionOpen] = useState(false);
  const quotaAlert = !isUnlimited && monthlyLimit && (usage?.alert_100 || usage?.alert_80);
  const attentionCount = (quotaAlert ? 1 : 0) + (errorsBannerVisible ? 1 : 0);

  // Header dinámico — operativo, no amable. Cambia según estado del sistema.
  // Linear/Stripe pattern: "Control room", no "Hola buenos días".
  const heroHeadline = (() => {
    if (history.length === 0) return "Sistema listo";
    if (pendingReview.length > 0) return `${pendingReview.length} ${pendingReview.length === 1 ? "video espera" : "videos esperan"} tu aprobación`;
    if (processing.length > 0) return `${processing.length} ${processing.length === 1 ? "video" : "videos"} renderizando`;
    return "Todo al día";
  })();
  const heroSubline = (() => {
    if (history.length === 0) return "Subí tu primer audio para empezar.";
    if (pendingReview.length > 0) return "Revisá y aprobá para destrabar descargas.";
    if (processing.length > 0) return "El sistema sigue trabajando en background.";
    return "Cero pendientes. Subí más audio cuando quieras.";
  })();

  // El "próximo a terminar" — heurística simple: primer processing con
  // mayor `progress`. Si no hay progress confiable, el más viejo en cola.
  const nextToFinish = (() => {
    if (processing.length === 0) return null;
    const sorted = [...processing].sort((a, b) =>
      (b.progress || 0) - (a.progress || 0)
        || (a.created_at || 0) - (b.created_at || 0)
    );
    return sorted[0];
  })();

  return (
    <div className="w-full max-w-[1700px] animate-fade-in">
      {/* ─── Command bar simplificada (header reposicionado, search vendrá en PR-2) ─── */}
      <div className="flex items-center justify-between mb-7">
        <div className="min-w-0">
          <p className="text-section text-gray-500 uppercase tracking-[0.18em]">
            {(() => {
              const d = new Date();
              return DATE_HEADER_FMT.format(d) + " · " + d.toLocaleTimeString("es-AR", { hour: "2-digit", minute: "2-digit" });
            })()}
          </p>
          <h1 className="text-[26px] leading-tight font-bold tracking-tight mt-1 truncate">
            {heroHeadline}
          </h1>
          <p className="text-sm text-ink-secondary mt-1">{heroSubline}</p>
        </div>
        <div className="flex items-center gap-2 shrink-0 ml-4">
          {/* Search button — abre el SearchPalette (PR-2 2026-05-25).
              Visual: input fake con placeholder + atajo ⌘K a la derecha.
              Match patrón Linear/Vercel command bar. */}
          {onOpenSearch && (
            <button
              type="button"
              onClick={onOpenSearch}
              className="hidden md:flex items-center gap-2 h-9 px-3 rounded-lg bg-surface-2/60 ring-1 ring-white/[0.06] hover:ring-white/[0.12] hover:bg-surface-2/80 text-gray-400 hover:text-gray-200 transition-colors text-xs"
              aria-label="Buscar"
            >
              <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                <circle cx="11" cy="11" r="8" /><path d="M21 21l-4.35-4.35" strokeLinecap="round" />
              </svg>
              <span>Buscar</span>
              <kbd className="ml-2 px-1.5 h-5 inline-flex items-center rounded text-[10px] font-mono bg-white/[0.06] ring-1 ring-white/10 text-gray-500">
                ⌘K
              </kbd>
            </button>
          )}
          {attentionCount > 0 && (
            <button
              type="button"
              onClick={() => setAttentionOpen((v) => !v)}
              aria-expanded={attentionOpen}
              className={`flex items-center gap-1.5 px-3 h-9 rounded-lg text-xs font-medium transition-colors ring-1
                ${attentionOpen
                  ? "bg-amber-500/15 text-amber-200 ring-amber-500/30"
                  : "bg-amber-500/[0.06] text-amber-300/80 ring-amber-500/20 hover:bg-amber-500/[0.10] hover:text-amber-200"
                }`}
            >
              <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24"><path d="M12 9v4M12 17h.01"/><circle cx="12" cy="12" r="10"/></svg>
              {attentionCount} {attentionCount === 1 ? "aviso" : "avisos"}
              <svg className={`w-3 h-3 transition-transform ${attentionOpen ? "rotate-180" : ""}`} fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24"><path d="M6 9l6 6 6-6" strokeLinecap="round" strokeLinejoin="round"/></svg>
            </button>
          )}
          <button onClick={onNewBatch} className="btn-primary px-5" data-tour="dashboard-new-batch">
            <svg className="inline-block w-4 h-4 mr-1.5 -mt-0.5" fill="none" stroke="currentColor" strokeWidth="2.5" viewBox="0 0 24 24">
              <path d="M12 5v14M5 12h14" strokeLinecap="round"/>
            </svg>
            {t("nav.new_batch")}
          </button>
        </div>
      </div>

      {/* ─── Hero KPI 3-up (Aprobar · Renderizando · Cuota) ───────────
            Una sola tarjeta con divide-x — leído como una unidad horizontal
            no como tres cards separadas. Stripe Dashboard pattern. ─── */}
      <div className="mb-8 rounded-card bg-surface-2/40 ring-1 ring-white/[0.04] grid grid-cols-1 md:grid-cols-3 md:divide-x md:divide-white/[0.04]">

        {/* COL 1: APROBAR — north star del operador */}
        <button
          onClick={() => pendingReview.length > 0 && onSelectJob(pendingReview[0].job_id)}
          disabled={pendingReview.length === 0}
          className={`text-left px-6 py-6 transition-colors ${pendingReview.length > 0 ? "hover:bg-surface-2/40 cursor-pointer" : "cursor-default"}`}
        >
          <p className="text-section text-gray-500 uppercase tracking-[0.18em]">Aprobar</p>
          <div className="mt-2 flex items-baseline gap-2">
            <span className={`text-[44px] leading-none font-bold tracking-tight tabular-nums
              ${pendingReview.length === 0 ? "text-white/40" :
                pendingReview.length >= 5 ? "text-red-300" :
                "text-amber-200"}`}>
              {pendingReview.length}
            </span>
            <span className="text-xs text-ink-secondary">
              {pendingReview.length === 0 ? "todo aprobado" :
                pendingReview.length === 1 ? "esperando review" : "esperando review"}
            </span>
          </div>
          {pendingReview.length > 0 && pendingReview[0] && (
            <p className="text-[11px] text-ink-secondary mt-3 truncate">
              <span className="font-mono tabular-nums text-gray-400">{timeAgo(pendingReview[0].created_at)}</span>
              {" · "}
              {(pendingReview[0].filename || "").replace(/\.(mp3|wav)$/i, "")}
            </p>
          )}
          {pendingReview.length > 0 && (
            <p className="text-[11px] text-brand-light mt-2 flex items-center gap-1 group-hover:translate-x-0.5 transition-transform">
              Revisar ahora
              <svg className="w-3 h-3" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24"><path d="M9 5l7 7-7 7" strokeLinecap="round" strokeLinejoin="round"/></svg>
            </p>
          )}
        </button>

        {/* COL 2: RENDERIZANDO — sistema en vivo */}
        <div className="px-6 py-6">
          <p className="text-section text-gray-500 uppercase tracking-[0.18em]">Renderizando</p>
          <div className="mt-2 flex items-baseline gap-2">
            <span className={`text-[44px] leading-none font-bold tracking-tight tabular-nums
              ${processing.length === 0 ? "text-white/40" : "text-brand-light"}`}>
              {processing.length}
            </span>
            <span className="text-xs text-ink-secondary">
              {processing.length === 0 ? "cola vacía" : processing.length === 1 ? "video en curso" : "jobs en curso"}
            </span>
          </div>
          {nextToFinish ? (
            <button
              onClick={() => onSelectJob(nextToFinish.job_id)}
              className="mt-3 w-full text-left flex items-center gap-2 group"
            >
              <span className="relative w-1.5 h-1.5 shrink-0">
                <span className="absolute inset-0 rounded-full bg-brand animate-ping opacity-60" />
                <span className="relative block w-1.5 h-1.5 rounded-full bg-brand" />
              </span>
              <span className="text-[11px] text-white truncate group-hover:text-brand-light transition-colors">
                {(nextToFinish.filename || "").replace(/\.(mp3|wav)$/i, "")}
              </span>
              {nextToFinish.progress !== undefined && nextToFinish.progress > 0 && (
                <span className="text-[10px] text-gray-500 font-mono tabular-nums shrink-0">
                  {Math.round(nextToFinish.progress)}%
                </span>
              )}
            </button>
          ) : (
            <p className="text-[11px] text-ink-secondary mt-3">
              <span className="inline-block w-1.5 h-1.5 rounded-full bg-accent/40 mr-2 align-middle" />
              Sistema OK · esperando tu próximo upload
            </p>
          )}
        </div>

        {/* COL 3: CUOTA — Stripe pattern (número grande + barra slim + delta) */}
        <div className="px-6 py-6">
          <p className="text-section text-gray-500 uppercase tracking-[0.18em]">
            Cuota {MONTH_FMT.format(new Date())}
          </p>
          <div className="mt-2 flex items-baseline gap-2">
            {isUnlimited ? (
              <>
                <span className="text-[44px] leading-none font-bold tracking-tight tabular-nums text-white">{monthlyUsed}</span>
                <span className="text-xs text-ink-secondary">sin límite</span>
              </>
            ) : monthlyLimit ? (
              <>
                <span className={`text-[44px] leading-none font-bold tracking-tight tabular-nums ${
                  usagePercent >= 100 ? "text-red-300" :
                  usagePercent >= 80 ? "text-amber-200" :
                  "text-white"
                }`}>
                  {monthlyUsed}
                </span>
                <span className="text-xs text-ink-secondary font-mono tabular-nums">/ {monthlyLimit}</span>
                <span className={`text-xs ml-auto ${
                  usagePercent >= 100 ? "text-red-300/80" :
                  usagePercent >= 80 ? "text-amber-300/80" :
                  "text-ink-secondary"
                }`}>
                  {Math.round(usagePercent)}%
                </span>
              </>
            ) : usageError ? (
              <button onClick={retryUsage} className="text-xs text-brand-light hover:underline underline-offset-2">
                Reintentar carga
              </button>
            ) : (
              <span className="text-xs text-ink-secondary">cargando…</span>
            )}
          </div>
          {!isUnlimited && monthlyLimit && (
            <div className="mt-3 w-full h-1 bg-surface-3/60 rounded-full overflow-hidden">
              <div
                className={`h-full rounded-full transition-all duration-700 ease-out ${
                  usagePercent >= 100
                    ? "bg-gradient-to-r from-amber-500 to-red-500"
                    : usagePercent >= 80
                      ? "bg-gradient-to-r from-brand to-amber-400"
                      : "bg-gradient-to-r from-brand to-accent"
                }`}
                style={{ width: `${Math.max(2, Math.min(100, usagePercent))}%` }}
              />
            </div>
          )}
          {!isUnlimited && monthlyLimit && (
            <p className="text-[11px] text-ink-secondary mt-2 font-mono tabular-nums">
              {monthlyLimit - monthlyUsed > 0
                ? `${monthlyLimit - monthlyUsed} restantes este mes`
                : "Cupo agotado · contactá soporte"}
            </p>
          )}
        </div>
      </div>

      {/* ─── Atención drawer — colapsable, solo se renderiza si hay avisos
            Y el operador lo abrió. Los banners viejos vivían apilados
            arriba compitiendo con el hero; ahora viven acá. ─── */}
      {attentionOpen && attentionCount > 0 && (
        <div className="mb-8 space-y-3 animate-fade-in">

      {!isUnlimited && monthlyLimit && (usage?.alert_100 || usage?.alert_80) && (
        (() => {
          const overageMode = usage.alert_100 && user?.allow_overage;
          const blockMode = usage.alert_100 && !user?.allow_overage;
          return (
            <div
              className={`w-full mb-4 flex items-center gap-3 px-5 py-4 rounded-card ring-1 ${
                blockMode
                  ? "bg-red-500/[0.08] ring-red-500/30"
                  : overageMode
                    ? "bg-brand/[0.08] ring-brand/30"
                    : "bg-amber-500/[0.06] ring-amber-500/25"
              }`}
            >
              <svg
                className={`w-5 h-5 shrink-0 ${
                  blockMode ? "text-red-300" :
                  overageMode ? "text-brand-light" :
                  "text-amber-300"
                }`}
                fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24"
              >
                <path d="M12 9v4M12 17h.01"/><circle cx="12" cy="12" r="10"/>
              </svg>
              <div className="flex-1 min-w-0">
                {overageMode ? (
                  <>
                    <p className="text-sm font-semibold text-brand-light">
                      Pasaste el plan mensual — los extras se facturan al cierre
                    </p>
                    <p className="text-xs text-ink-secondary mt-0.5">
                      {monthlyUsed} videos generados · {usage.overage} adicionales × ${usage.overage_cost_per_video}{" "}
                      = <span className="font-semibold text-white">${usage.overage_total}</span> a abonar este mes.
                    </p>
                  </>
                ) : blockMode ? (
                  <>
                    <p className="text-sm font-semibold text-red-200">
                      Llegaste al límite mensual ({monthlyUsed}/{monthlyLimit})
                    </p>
                    <p className="text-xs text-red-300/80 mt-0.5">
                      No vas a poder subir más videos hasta el mes que viene. Si necesitás extender el cupo, escribinos a{" "}
                      <a href="mailto:soporte@genly.pro" className="underline font-medium hover:text-red-200">
                        soporte@genly.pro
                      </a>.
                    </p>
                  </>
                ) : (
                  <>
                    <p className="text-sm font-semibold text-amber-200">
                      Te quedan {monthlyLimit - monthlyUsed} videos este mes ({monthlyUsed}/{monthlyLimit})
                    </p>
                    <p className="text-xs text-amber-300/80 mt-0.5">
                      {user?.allow_overage
                        ? `Pasado el tope, cada video adicional cuesta $${usage.overage_cost_per_video} y se factura al cierre.`
                        : <>Si vas a necesitar más, contactanos antes de llegar al tope:{" "}
                            <a href="mailto:soporte@genly.pro" className="underline font-medium hover:text-amber-200">
                              soporte@genly.pro
                            </a>.</>
                      }
                    </p>
                  </>
                )}
              </div>
            </div>
          );
        })()
      )}

          {/* Pending review CTA + monthly usage card + En proceso section eliminados:
              ahora viven dentro del hero 3-up arriba (cols Aprobar / Renderizando / Cuota). */}
          {errorsBannerVisible && (
            <div className="w-full flex items-center gap-3 px-4 py-3 rounded-xl bg-red-500/[0.06] ring-1 ring-red-500/20">
              <svg className="w-4 h-4 text-red-400 shrink-0" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                <circle cx="12" cy="12" r="10"/><path d="M15 9l-6 6M9 9l6 6"/>
              </svg>
              <p className="text-xs text-red-300 flex-1">
                {errors.length} {errors.length === 1 ? "video falló este mes" : "videos fallaron este mes"}
              </p>
              <button
                onClick={dismissErrors}
                aria-label="Descartar"
                className="text-red-400/60 hover:text-red-300 transition-colors p-1 -mr-1"
              >
                <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2.5" viewBox="0 0 24 24">
                  <path d="M18 6L6 18M6 6l12 12" strokeLinecap="round"/>
                </svg>
              </button>
            </div>
          )}
        </div>
      )}

      {/* ─── DashboardRich: Stepper + FormatGallery (PR #462) ─────────
            Educan flujo + venden formatos. Stepper es dismissible
            (localStorage). FormatGallery segmenta ProRes por plan. ─── */}
      <DashboardStepper
        onPrimaryAction={(stepIdx) => {
          // Step 1 (Subir) + Step 4 (Renderizar) → arrancan upload.
          // Steps 2/3 → no-op (decorativos por ahora; futuros enlaces al
          // help center cuando llegue a main).
          if (stepIdx === 0 || stepIdx === 3) {
            if (typeof onNewBatch === "function") onNewBatch();
            else navigate("/new");
          }
        }}
      />
      <FormatGallery
        user={user}
        onSelectFormat={handleSelectFormat}
        onUpgrade={handleUpgrade}
      />

      {/* ─── Tus últimos videos — visual scan, NOT a copy of History ── */}
      {recentDone.length > 0 && (
        <div data-tour="dashboard-recent">
          <div className="flex items-center justify-between mb-4">
            <SectionLabel>Tus últimos videos</SectionLabel>
            <button onClick={onViewHistory} className="text-[11px] text-brand hover:text-brand-light transition-colors flex items-center gap-1 -translate-y-1.5">
              Ver historial completo
              <svg className="w-3 h-3" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                <path d="M9 5l7 7-7 7" strokeLinecap="round" strokeLinejoin="round"/>
              </svg>
            </button>
          </div>
          <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
            {recentDone.map((job) => (
              <VideoCard key={job.job_id} job={job} onSelect={onSelectJob} />
            ))}
          </div>
        </div>
      )}

      {/* Onboarding tour — fires only on first dashboard visit for new users */}
      <DashboardTour user={user} />

      {/* ─── Empty state — only when there is literally nothing.
          Order: loading > error > empty. We must beat the empty branch
          while /jobs is in flight or 5xx'ing, so a returning user with
          100 videos in their library never sees "Empezá tu primer
          lote" during the initial fetch. ─── */}
      {history.length === 0 && !historyLoaded && !historyError && (
        /* Skeleton screen — UI specialist 2026-05-24: reemplazó el spinner
           genérico. El operador YA ve la estructura del Dashboard (6 cards
           en grid) antes de que llegue /jobs; el swap no tiene reflow. */
        <div>
          <div className="mb-4">
            <div className="h-3 w-32 rounded bg-surface-2/60 animate-pulse mb-3" />
          </div>
          <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
            {Array.from({ length: 6 }).map((_, i) => (
              <SkeletonVideoCard key={i} />
            ))}
          </div>
        </div>
      )}
      {history.length === 0 && historyError && (
        <div className="rounded-card p-10 text-center bg-amber-500/[0.06] ring-1 ring-amber-500/25">
          <div className="w-12 h-12 mx-auto mb-4 rounded-2xl bg-amber-500/15 ring-1 ring-amber-500/30 flex items-center justify-center">
            <svg className="w-6 h-6 text-amber-300" fill="none" stroke="currentColor" strokeWidth="1.6" viewBox="0 0 24 24">
              <path d="M12 9v3.5m0 3.5h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z" strokeLinecap="round" strokeLinejoin="round"/>
            </svg>
          </div>
          <h3 className="text-base font-semibold text-white mb-1.5 tracking-tight">
            {t("dash.history_error_title") || "No pudimos cargar tu historial"}
          </h3>
          <p className="text-sm text-ink-secondary mb-5">
            {t("dash.history_error_body") || "Puede ser una caída momentánea de la conexión. Probá de nuevo."}
          </p>
          <button onClick={onRetryHistory} className="btn-primary px-6">
            {t("dash.retry") || "Reintentar"}
          </button>
        </div>
      )}
      {history.length === 0 && historyLoaded && !historyError && (
        <div className="rounded-card p-14 text-center bg-surface-2/30 ring-1 ring-white/[0.04]">
          <div className="w-14 h-14 mx-auto mb-5 rounded-2xl bg-brand/10 ring-1 ring-brand/20 flex items-center justify-center">
            <svg className="w-7 h-7 text-brand-light" fill="none" stroke="currentColor" strokeWidth="1.5" viewBox="0 0 24 24">
              <path d="M9 18V5l12-2v13" strokeLinecap="round" strokeLinejoin="round"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>
            </svg>
          </div>
          <h3 className="text-lg font-bold text-white mb-1.5 tracking-tight">Empezá tu primer lote</h3>
          <p className="text-sm text-ink-secondary mb-6">Subí un audio (.mp3 o .wav). Generamos el lyric video automáticamente.</p>
          <button onClick={onNewBatch} className="btn-primary px-6">
            {t("nav.new_batch")}
          </button>
        </div>
      )}
    </div>
  );
}
