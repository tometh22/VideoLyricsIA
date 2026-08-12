import { useState, useEffect, useMemo } from "react";
import { useNavigate } from "react-router-dom";
import { useI18n } from "../i18n";
import { useMediaUrl } from "../mediaUrl";
import { fetchWithTimeout } from "../fetchWithTimeout";
import { DashboardTour } from "./OnboardingTour";
import NovedadHero from "./WhatsNew/NovedadHero";
import ProResBadge from "./ProResBadge";
import { SkeletonVideoCard } from "./Skeleton";
import DashboardStepper from "./DashboardRich/Stepper";
import FormatGallery from "./DashboardRich/FormatGallery";
import MediaPreview from "./MediaPreview";
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

function authHeaders() {
  const token = localStorage.getItem("genly_token");
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function timeAgo(ts, t) {
  if (!ts) return "";
  const diff = Date.now() / 1000 - ts;
  if (diff < 60) return t("common.now");
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

function VideoCard({ job, onSelect, t }) {
  const name = (job.filename || "").replace(/\.mp3$/i, "");
  const songName = name.includes(" - ") ? name.split(" - ").slice(1).join(" - ") : name;
  const artistName = job.artist || (name.includes(" - ") ? name.split(" - ")[0] : "");
  // version: edits overwrite the same R2 thumbnail key — bust the URL when
  // the render changes so the card doesn't keep showing the pre-edit image.
  const thumbSrc = useMediaUrl(job.job_id, "thumbnail", "preview", `${job.edit_count || 0}-${job.status || ""}`);

  return (
    <button
      onClick={() => onSelect(job.job_id, job.status)}
      className="overflow-hidden rounded-xl text-left group bg-surface-2/40 hover:bg-surface-2/70 ring-1 ring-white/[0.04] hover:ring-white/[0.10] transition-all"
    >
      <MediaPreview src={thumbSrc} status={job.status} alt={`${t("media.thumbnail_of")} ${songName || t("common.video")}`} label={t("media.video_preview")} className="aspect-video" imageClassName="group-hover:scale-[1.04] transition-transform duration-500">
        <div className="absolute z-[2] inset-0 flex items-center justify-center opacity-30 sm:opacity-0 sm:group-hover:opacity-100 transition-opacity bg-black/30">
          <div className="w-10 h-10 rounded-full bg-white/15 backdrop-blur-md flex items-center justify-center ring-1 ring-white/20">
            <svg className="w-4 h-4 text-white ml-0.5" fill="currentColor" viewBox="0 0 24 24">
              <path d="M8 5v14l11-7z"/>
            </svg>
          </div>
        </div>
      </MediaPreview>
      <div className="px-3.5 py-3">
        <div className="flex items-start gap-2 min-w-0">
          <p className="text-[13px] font-medium text-white truncate flex-1 min-w-0">{songName || t("common.untitled")}</p>
          <ProResBadge
            deliveryProfile={job.delivery_profile}
            proresReady={job.prores_ready}
            jobStatus={job.status}
          />
        </div>
        <p className="text-[11px] text-gray-500 truncate mt-0.5">
          {artistName}
          {job.created_at && <span className="ml-1.5 text-gray-600">· {timeAgo(job.created_at, t)}</span>}
        </p>
      </div>
    </button>
  );
}

export default function Dashboard({ user, history, historyError, historyLoaded = true, onRetryHistory, onSelectJob, onNewBatch, onViewHistory, onOpenSearch }) {
  const { t, lang } = useI18n();
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
    navigate("/account?tab=facturacion");
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

  // First-week user gate (matches the onboarding-tour age gate).
  const isFirstWeekUser = (() => {
    if (!user || !user.created_at) return false;
    const t = Date.parse(user.created_at);
    if (Number.isNaN(t)) return false;
    return (Date.now() - t) / 86400000 < 14;
  })();
  // Hotfix 2026-05-29: agus.cafisi reportó ver el hero gigante "creá tu
  // primer video" con history=[] aunque tiene historial real. Causa
  // probable: /jobs falló silenciosamente (CORS / 5xx caché / cold start)
  // y volvió un array vacío sin setear historyError. Para no asustar a
  // un veterano con "todo borrado", restringimos el hero al combo
  // user nuevo (<14 días) + 0 history + carga OK. Para users veteranos
  // con history=[] (raro pero posible: cuenta nueva en un sello viejo,
  // backend hiccup) mostramos el empty state pequeño tradicional que
  // dice "Empezá tu primer lote" pero NO sustituye toda la home.
  const isTrueEmptyState = history.length === 0 && historyLoaded && !historyError;
  const isEmptyState = isTrueEmptyState && isFirstWeekUser;
  const showStepper = isEmptyState || isFirstWeekUser;

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
    if (history.length === 0) return t("dash.hero_system_ready");
    if (pendingReview.length > 0) return t(pendingReview.length === 1 ? "dash.hero_review_one" : "dash.hero_review_many", { count: pendingReview.length });
    if (processing.length > 0) return t(processing.length === 1 ? "dash.hero_render_one" : "dash.hero_render_many", { count: processing.length });
    return t("dash.hero_all_clear");
  })();
  const heroSubline = (() => {
    if (history.length === 0) return t("dash.first_audio_subtitle");
    if (pendingReview.length > 0) return t("dash.hero_review_subline");
    if (processing.length > 0) return t("dash.hero_render_subline");
    return t("dash.hero_clear_subline");
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
  const locale = lang === "en" ? "en-US" : lang === "pt" ? "pt-BR" : "es-AR";
  const dashboardDate = (() => {
    const d = new Date();
    return new Intl.DateTimeFormat(locale, { weekday: "long", day: "numeric", month: "long" }).format(d)
      + " · " + d.toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit" });
  })();
  const liveState = processing.length > 0
    ? t("dash.live_render")
    : pendingReview.length > 0
      ? t("dash.live_review")
      : t("dash.live_operational");
  const liveDotClass = processing.length > 0
    ? "bg-brand shadow-[0_0_14px_rgba(124,92,255,.75)]"
    : pendingReview.length > 0
      ? "bg-amber-300 shadow-[0_0_14px_rgba(252,211,77,.65)]"
      : "bg-accent shadow-[0_0_14px_rgba(20,200,168,.65)]";

  return (
    <div className="w-full max-w-[1360px] animate-fade-in">
      {/* Page header: global search + create live in GlobalTopbar. */}
      <div className="mb-4 px-1 py-2">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
        <div className="min-w-0">
          <div className="mb-2 flex flex-wrap items-center gap-2">
            <p className="text-section text-gray-500 uppercase tracking-[0.18em]">{t("dash.production_center")}</p>
            <span className="rounded-full bg-white/[0.045] px-2.5 py-1 text-[10px] font-semibold text-gray-400 ring-1 ring-white/[0.06]">
              {dashboardDate}
            </span>
            <span className="inline-flex items-center gap-1.5 rounded-full bg-white/[0.04] px-2.5 py-1 text-[10px] font-semibold text-gray-300 ring-1 ring-white/[0.06]">
              <span className={`h-1.5 w-1.5 rounded-full ${liveDotClass}`} />
              {liveState}
            </span>
          </div>
          <h1 className="text-[24px] leading-[1.14] font-bold tracking-normal text-white md:text-[26px]">
            {heroHeadline}
          </h1>
          <p className="mt-1.5 max-w-2xl text-[13px] leading-relaxed text-ink-secondary">{heroSubline}</p>
        </div>
        <div className="flex shrink-0 flex-wrap items-center gap-2 lg:justify-end">
          {/* Search button — abre el SearchPalette (PR-2 2026-05-25).
              Visual: input fake con placeholder + atajo ⌘K a la derecha.
              Match patrón Linear/Vercel command bar. */}
          {onOpenSearch && (
            <button
              type="button"
              onClick={onOpenSearch}
              className="hidden md:flex items-center gap-2 h-9 px-3 rounded-lg bg-surface-2/60 ring-1 ring-white/[0.06] hover:ring-white/[0.12] hover:bg-surface-2/80 text-gray-400 hover:text-gray-200 transition-colors text-xs"
              aria-label={t("topbar.search")}
            >
              <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                <circle cx="11" cy="11" r="8" /><path d="M21 21l-4.35-4.35" strokeLinecap="round" />
              </svg>
              <span>{t("common.search")}</span>
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
              {attentionCount} {attentionCount === 1 ? t("dash.notice_one") : t("dash.notice_many")}
              <svg className={`w-3 h-3 transition-transform ${attentionOpen ? "rotate-180" : ""}`} fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24"><path d="M6 9l6 6 6-6" strokeLinecap="round" strokeLinejoin="round"/></svg>
            </button>
          )}
        </div>
        </div>
      </div>

      {/* ─── Hero KPI 3-up (Aprobar · Renderizando · Cuota) ───────────
            Una sola tarjeta con divide-x — leído como una unidad horizontal
            no como tres cards separadas. Stripe Dashboard pattern.
            UX 2026-05-29: ocultas cuando todos los valores son 0 — en cuenta
            nueva el bloque ocupaba 200px diciendo "no pasa nada". ─── */}
      {(pendingReview.length > 0 || processing.length > 0 || monthlyUsed > 0) && (
      <div className="mb-5 grid grid-cols-1 overflow-hidden rounded-xl bg-[#111118]/82 ring-1 ring-white/[0.06] md:grid-cols-3 md:divide-x md:divide-white/[0.055]">

        {/* COL 1: APROBAR — north star del operador */}
        <button
          onClick={() => pendingReview.length > 0 && onSelectJob(pendingReview[0].job_id)}
          disabled={pendingReview.length === 0}
          className={`group text-left px-5 py-4 transition-colors ${pendingReview.length > 0 ? "hover:bg-white/[0.035] cursor-pointer" : "cursor-default"}`}
        >
          <div className="flex items-center justify-between gap-3">
            <p className="text-section text-gray-500 uppercase tracking-[0.18em]">{t("review.approve")}</p>
            <span className={`rounded-full px-2 py-1 text-[10px] font-semibold ring-1 ${
              pendingReview.length > 0
                ? "bg-amber-400/[0.08] text-amber-200 ring-amber-400/20"
                : "bg-white/[0.04] text-gray-500 ring-white/[0.05]"
            }`}>
              {pendingReview.length > 0 ? t("dash.requires_action") : t("dash.clear")}
            </span>
          </div>
          <div className="mt-2 flex items-baseline gap-2">
            <span className={`text-[34px] leading-none font-bold tracking-normal tabular-nums md:text-[36px]
              ${pendingReview.length === 0 ? "text-white/40" :
                pendingReview.length >= 5 ? "text-red-300" :
                "text-amber-200"}`}>
              {pendingReview.length}
            </span>
            <span className="text-xs text-ink-secondary">
              {pendingReview.length === 0 ? t("dash.all_approved") :
                pendingReview.length === 1 ? t("dash.review_pending_one") : t("dash.review_pending_many")}
            </span>
          </div>
          {pendingReview.length > 0 && pendingReview[0] && (
            <p className="text-[11px] text-ink-secondary mt-3 truncate">
              <span className="font-mono tabular-nums text-gray-400">{timeAgo(pendingReview[0].created_at, t)}</span>
              {" · "}
              {(pendingReview[0].filename || "").replace(/\.(mp3|wav)$/i, "")}
            </p>
          )}
          {pendingReview.length > 0 && (
            <p className="text-[11px] text-brand-light mt-2 flex items-center gap-1 group-hover:translate-x-0.5 transition-transform">
              {t("dash.review_now")}
              <svg className="w-3 h-3" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24"><path d="M9 5l7 7-7 7" strokeLinecap="round" strokeLinejoin="round"/></svg>
            </p>
          )}
        </button>

        {/* COL 2: RENDERIZANDO — sistema en vivo */}
        <div className="px-5 py-4">
          <div className="flex items-center justify-between gap-3">
            <p className="text-section text-gray-500 uppercase tracking-[0.18em]">{t("dash.rendering")}</p>
            <span className={`rounded-full px-2 py-1 text-[10px] font-semibold ring-1 ${
              processing.length > 0
                ? "bg-brand/[0.10] text-brand-light ring-brand/25"
                : "bg-white/[0.04] text-gray-500 ring-white/[0.05]"
            }`}>
              {processing.length > 0 ? t("dash.live") : t("dash.no_queue")}
            </span>
          </div>
          <div className="mt-2 flex items-baseline gap-2">
            <span className={`text-[34px] leading-none font-bold tracking-normal tabular-nums md:text-[36px]
              ${processing.length === 0 ? "text-white/40" : "text-brand-light"}`}>
              {processing.length}
            </span>
            <span className="text-xs text-ink-secondary">
              {processing.length === 0 ? t("dash.empty_queue") : processing.length === 1 ? t("dash.job_running_one") : t("dash.job_running_many")}
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
              {errors.length > 0 ? t("dash.errors_need_attention", { count: errors.length }) : t("dash.no_active_renders")}
            </p>
          )}
        </div>

        {/* COL 3: CUOTA — Stripe pattern (número grande + barra slim + delta) */}
        <div className="px-5 py-4">
          <div className="flex items-center justify-between gap-3">
            <p className="text-section text-gray-500 uppercase tracking-[0.18em]">
              {t("dash.quota")} {new Intl.DateTimeFormat(locale, { month: "long" }).format(new Date())}
            </p>
            <span className={`rounded-full px-2 py-1 text-[10px] font-semibold ring-1 ${
              isUnlimited
                ? "bg-accent/[0.08] text-accent ring-accent/20"
                : usagePercent >= 80
                  ? "bg-amber-400/[0.08] text-amber-200 ring-amber-400/20"
                  : "bg-white/[0.04] text-gray-500 ring-white/[0.05]"
            }`}>
              {isUnlimited ? t("dash.unlimited") : monthlyLimit ? t("dash.percent_used", { percent: Math.round(usagePercent) }) : t("dash.loading")}
            </span>
          </div>
          <div className="mt-2 flex items-baseline gap-2">
            {isUnlimited ? (
              <>
                <span className="text-[34px] leading-none font-bold tracking-normal tabular-nums text-white md:text-[36px]">{monthlyUsed}</span>
                <span className="text-xs text-ink-secondary">{t("dash.unlimited")}</span>
              </>
            ) : monthlyLimit ? (
              <>
                <span className={`text-[34px] leading-none font-bold tracking-normal tabular-nums md:text-[36px] ${
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
                {t("dash.retry")}
              </button>
            ) : (
              <span className="text-xs text-ink-secondary">{t("dash.loading")}</span>
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
                ? t("dash.remaining_month", { count: monthlyLimit - monthlyUsed })
                : t("dash.quota_exhausted")}
            </p>
          )}
          {/* Créditos de regalo + qué rinde (dinámico). Mismo dato que el
              medidor de la sidebar; el costo sale del backend (scenes_credit_cost). */}
          {!isUnlimited && (() => {
            const bonusRemaining = usage?.bonus_remaining ?? 0;
            const totalAvail = usage?.total_available;
            const cost = usage?.scenes_credit_cost ?? 3;
            const proj = usage?.projection || {};
            const projN = proj.normal ?? totalAvail;
            const projE = proj.escenas ?? (totalAvail != null ? Math.floor(totalAvail / (cost || 1)) : null);
            if (totalAvail == null) return null;
            let giftDays = null;
            if (bonusRemaining > 0 && usage?.bonus_expires_at) {
              const ms = new Date(usage.bonus_expires_at).getTime() - Date.now();
              giftDays = Number.isFinite(ms) ? Math.max(0, Math.ceil(ms / 86_400_000)) : null;
            }
            return (
              <div className="mt-3 pt-3 border-t border-white/[0.06] space-y-1.5">
                {bonusRemaining > 0 && (
                  <p className="text-[11px] text-emerald-300 font-medium">
                    {t("dash.bonus_active", { count: bonusRemaining })}
                    {giftDays != null ? (giftDays === 0 ? ` · ${t("dash.expires_today")}` : ` · ${t("dash.expires_days", { count: giftDays })}`) : ""}
                  </p>
                )}
                <p className="text-[11px] text-ink-secondary">
                  {t("dash.projection", { normal: projN, scenes: projE })}
                </p>
                <p className="text-[10px] text-gray-500">
                  {t("dash.credit_cost", { cost })}
                </p>
              </div>
            );
          })()}
        </div>
      </div>
      )}

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
              className={`w-full mb-4 flex items-center gap-3 px-5 py-4 rounded-xl ring-1 ${
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
                      {t("dash.overage_title")}
                    </p>
                    <p className="text-xs text-ink-secondary mt-0.5">
                      {t("dash.overage_body", { used: monthlyUsed, overage: usage.overage, cost: usage.overage_cost_per_video })}{" "}
                      = <span className="font-semibold text-white">${usage.overage_total}</span> {t("dash.overage_due")}
                    </p>
                  </>
                ) : blockMode ? (
                  <>
                    <p className="text-sm font-semibold text-red-200">
                      {t("dash.limit_title", { used: monthlyUsed, limit: monthlyLimit })}
                    </p>
                    <p className="text-xs text-red-300/80 mt-0.5">
                      {t("dash.limit_body")}{" "}
                      <a href="mailto:soporte@genly.pro" className="underline font-medium hover:text-red-200">
                        soporte@genly.pro
                      </a>.
                    </p>
                  </>
                ) : (
                  <>
                    <p className="text-sm font-semibold text-amber-200">
                      {t("dash.low_quota_title", { remaining: monthlyLimit - monthlyUsed, used: monthlyUsed, limit: monthlyLimit })}
                    </p>
                    <p className="text-xs text-amber-300/80 mt-0.5">
                      {user?.allow_overage
                        ? t("dash.low_quota_overage", { cost: usage.overage_cost_per_video })
                        : <>{t("billing.nudge_body") || "Mejorá tu plan para no frenarte cuando llegues al tope."}{" "}
                            <button onClick={handleUpgrade} className="underline font-medium hover:text-amber-200">
                              {t("billing.nudge_cta") || "Mejorar plan"}
                            </button></>
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
                {t(errors.length === 1 ? "dash.failed_month_one" : "dash.failed_month_many", { count: errors.length })}
              </p>
              <button
                onClick={dismissErrors}
                aria-label={t("common.dismiss")}
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

      {/* ─── Hero dropzone — empty state focal point.
            UX 2026-05-29: reemplaza el "Empezá tu primer lote" chico que
            estaba al final. Para una cuenta sin historial, esto pasa a ser
            el protagonista visual claro (patrón Notion/Loom empty state). ─── */}
      {isEmptyState && (
        <button
          type="button"
          onClick={onNewBatch}
          className="dr-hero-drop w-full mb-6 group"
          aria-label={t("dash.hero.cta") || "Subir audio para empezar"}
        >
          <div className="dr-hero-drop-inner">
            <div className="dr-hero-drop-icon" aria-hidden="true">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
                <path d="M12 5v14M5 12l7-7 7 7"/>
              </svg>
            </div>
            <h2 className="dr-hero-drop-title">
              {t("dash.hero.title") || "Arrastrá tu MP3 para empezar"}
            </h2>
            <p className="dr-hero-drop-sub">
              {t("dash.hero.sub") || "O cliqueá para elegir. MP3 o WAV, hasta 5 archivos a la vez, 100 MB cada uno."}
            </p>
            <span className="dr-hero-drop-cta">
              {t("dash.hero.cta") || "Subir audio"}
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><path d="M5 12h14M13 5l7 7-7 7" strokeLinecap="round" strokeLinejoin="round"/></svg>
            </span>
          </div>
        </button>
      )}

      {/* ─── DashboardRich: Stepper + FormatGallery (PR #465) ─────────
            Educan flujo + venden formatos. UX 2026-05-29: el Stepper se
            muestra solo a users del primer mes o en empty state. Para
            veteranos activos no aporta y ocupa fold. ─── */}
      {showStepper && (
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
      )}
      {/* Hero de Novedades: anuncia la release featured del changelog y
          provee el target data-tour="whatsnew-release" del DashboardTour. */}
      <NovedadHero />
      {/* ─── Tus últimos videos — visual scan, NOT a copy of History ── */}
      {recentDone.length > 0 && (
        <div data-tour="dashboard-recent">
          <div className="flex items-center justify-between mb-4">
            <SectionLabel>{t("dash.recent_activity")}</SectionLabel>
            <button onClick={onViewHistory} className="text-[11px] text-brand hover:text-brand-light transition-colors flex items-center gap-1 -translate-y-1.5">
              {t("dash.full_history")}
              <svg className="w-3 h-3" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                <path d="M9 5l7 7-7 7" strokeLinecap="round" strokeLinejoin="round"/>
              </svg>
            </button>
          </div>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-[repeat(auto-fill,minmax(250px,340px))]">
            {recentDone.map((job) => (
              <VideoCard key={job.job_id} job={job} onSelect={onSelectJob} t={t} />
            ))}
          </div>
        </div>
      )}

      <FormatGallery
        user={user}
        onSelectFormat={handleSelectFormat}
        onUpgrade={handleUpgrade}
      />

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
        <div className="rounded-xl p-10 text-center bg-amber-500/[0.06] ring-1 ring-amber-500/25">
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
      {/* Empty state pequeño — vuelve para users veteranos con history=[].
          Hotfix 2026-05-29: agus.cafisi (user con historial real) reportó
          ver el hero "creá tu primer video" tras un fetch de /jobs que
          volvió silenciosamente vacío. El hero ahora solo aparece para
          users <14 días; para el resto con history=[] (incluyendo el caso
          de fallo silencioso de /jobs) mostramos este card sutil que no
          alarma con "creá tu primer video". */}
      {isTrueEmptyState && !isFirstWeekUser && (
        <div className="rounded-xl p-14 text-center bg-surface-2/30 ring-1 ring-white/[0.04]">
          <div className="w-14 h-14 mx-auto mb-5 rounded-2xl bg-brand/10 ring-1 ring-brand/20 flex items-center justify-center">
            <svg className="w-7 h-7 text-brand-light" fill="none" stroke="currentColor" strokeWidth="1.5" viewBox="0 0 24 24">
              <path d="M9 18V5l12-2v13" strokeLinecap="round" strokeLinejoin="round"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>
            </svg>
          </div>
          <h3 className="text-lg font-bold text-white mb-1.5 tracking-tight">
            {t("dash.no_recent_title") || "Nada por revisar ahora"}
          </h3>
          <p className="text-sm text-ink-secondary mb-6">
            {t("dash.no_recent_sub") || "Si esperabas ver tu historial y no aparece, probá recargar la página."}
          </p>
          <div className="flex items-center justify-center gap-2">
            <button onClick={onNewBatch} className="btn-primary px-6">
              {t("nav.new_batch")}
            </button>
            {onRetryHistory && (
              <button
                onClick={onRetryHistory}
                className="px-5 py-3 rounded-lg text-sm text-ink-secondary hover:text-white hover:bg-surface-2/60 transition-colors"
              >
                {t("dash.retry") || "Reintentar"}
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
