import { useState, useEffect } from "react";
import { useI18n } from "../i18n";
import { DashboardTour } from "./OnboardingTour";

const API = import.meta.env.VITE_API_URL || "";

function authHeaders() {
  const token = localStorage.getItem("genly_token");
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function tokenParam() {
  const token = localStorage.getItem("genly_token");
  return token ? `token=${encodeURIComponent(token)}` : "";
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
function SectionLabel({ children }) {
  return (
    <p className="text-[10px] font-medium text-gray-500 uppercase tracking-[0.18em] mb-3">
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
        {job.status === "queued" ? (t("dash.queued") || "En cola") : t("dash.processing")}
      </span>
    </button>
  );
}

function VideoCard({ job, onSelect }) {
  const name = (job.filename || "").replace(/\.mp3$/i, "");
  const songName = name.includes(" - ") ? name.split(" - ").slice(1).join(" - ") : name;
  const artistName = job.artist || (name.includes(" - ") ? name.split(" - ")[0] : "");

  return (
    <button
      onClick={() => onSelect(job.job_id)}
      className="rounded-card overflow-hidden text-left group bg-surface-2/40 hover:bg-surface-2/70 ring-1 ring-white/[0.04] hover:ring-white/[0.10] transition-all"
    >
      <div className="aspect-video bg-surface-3/30 relative overflow-hidden">
        <img
          src={`${API}/preview/${job.job_id}/thumbnail?${tokenParam()}`}
          alt=""
          className="w-full h-full object-cover group-hover:scale-[1.04] transition-transform duration-500"
          onError={(e) => { e.target.style.display = "none"; }}
        />
        <div className="absolute inset-0 flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity bg-black/30">
          <div className="w-10 h-10 rounded-full bg-white/15 backdrop-blur-md flex items-center justify-center ring-1 ring-white/20">
            <svg className="w-4 h-4 text-white ml-0.5" fill="currentColor" viewBox="0 0 24 24">
              <path d="M8 5v14l11-7z"/>
            </svg>
          </div>
        </div>
      </div>
      <div className="px-3.5 py-3">
        <p className="text-[13px] font-medium text-white truncate">{songName || "Sin nombre"}</p>
        <p className="text-[11px] text-gray-500 truncate mt-0.5">
          {artistName}
          {job.created_at && <span className="ml-1.5 text-gray-600">· {timeAgo(job.created_at)}</span>}
        </p>
      </div>
    </button>
  );
}

export default function Dashboard({ user, history, onSelectJob, onNewBatch, onViewHistory }) {
  const { t } = useI18n();

  const pendingReview = history.filter((h) => h.status === "pending_review");
  const processing = history.filter((h) => h.status === "processing" || h.status === "queued");
  const recentDone = history.filter((h) => h.status === "done").slice(0, 6);
  const errors = history.filter((h) => h.status === "error" || h.status === "validation_failed");

  // Real plan usage from API.
  const [usage, setUsage] = useState(null);
  useEffect(() => {
    fetch(`${API}/usage`, { headers: authHeaders() })
      .then((r) => (r.ok ? r.json() : null))
      .then(setUsage)
      .catch(() => {});
  }, [history.length]);

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

  return (
    <div className="w-full max-w-4xl animate-fade-in">
      {/* ─── Header ─────────────────────────────────────────────────── */}
      <div className="flex items-end justify-between mb-10">
        <div>
          <h1 className="text-[28px] leading-tight font-bold tracking-tight">
            {greeting}{firstName && <span className="text-ink-secondary font-normal">, {firstName}</span>}
          </h1>
          <p className="text-sm text-ink-secondary mt-1.5">{monthlySubtitle}</p>
        </div>
        <button onClick={onNewBatch} className="btn-primary px-6" data-tour="dashboard-new-batch">
          <svg className="inline-block w-4 h-4 mr-2 -mt-0.5" fill="none" stroke="currentColor" strokeWidth="2.5" viewBox="0 0 24 24">
            <path d="M12 5v14M5 12h14" strokeLinecap="round"/>
          </svg>
          {t("nav.new_batch")}
        </button>
      </div>

      {/* ─── Pending review CTA — brand violet because it's a positive
            "do this next", not a danger warning ───────────────────── */}
      {pendingReview.length > 0 && (
        <button
          onClick={() => onSelectJob(pendingReview[0].job_id)}
          className="w-full mb-4 flex items-center gap-4 px-5 py-4 rounded-card text-left group transition-all
                     bg-gradient-to-r from-brand/[0.10] via-brand/[0.06] to-transparent
                     ring-1 ring-brand/20 hover:ring-brand/40
                     hover:from-brand/[0.14] hover:via-brand/[0.08]"
        >
          <div className="w-10 h-10 rounded-xl bg-brand/15 flex items-center justify-center shrink-0 ring-1 ring-brand/30">
            <svg className="w-5 h-5 text-brand-light" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
              <path d="M9 11l3 3L22 4M21 12v7a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2h11" strokeLinecap="round" strokeLinejoin="round"/>
            </svg>
          </div>
          <div className="flex-1 min-w-0">
            <p className="text-sm font-semibold text-white">
              {pendingReview.length === 1
                ? "1 video esperando tu aprobación"
                : `${pendingReview.length} videos esperando tu aprobación`}
            </p>
            <p className="text-xs text-ink-secondary mt-0.5">
              Revisá la transcripción y aprobá para destrabar la descarga
            </p>
          </div>
          <svg className="w-5 h-5 text-brand-light/70 group-hover:translate-x-0.5 transition-transform shrink-0" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
            <path d="M9 5l7 7-7 7" strokeLinecap="round" strokeLinejoin="round"/>
          </svg>
        </button>
      )}

      {/* ─── Errors banner (rare, secondary tone, dismissible) ────── */}
      {errorsBannerVisible && (
        <div className="w-full mb-4 flex items-center gap-3 px-4 py-3 rounded-xl bg-red-500/[0.06] ring-1 ring-red-500/20">
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

      {/* ─── Plan usage — Stripe-style hero number, bar as secondary ── */}
      <div className="rounded-card p-7 mb-10 bg-surface-2/40 ring-1 ring-white/[0.04]" data-tour="dashboard-usage">
        <div className="flex items-end justify-between mb-5">
          <div>
            <SectionLabel>{t("dash.monthly_usage")}</SectionLabel>
            <div className="flex items-baseline gap-2">
              {isUnlimited ? (
                <>
                  <span className="text-4xl font-bold tracking-tight text-white">{monthlyUsed}</span>
                  <span className="text-sm text-ink-secondary">videos · sin límite</span>
                </>
              ) : monthlyLimit ? (
                <>
                  <span className="text-4xl font-bold tracking-tight text-white">{monthlyUsed}</span>
                  <span className="text-sm text-ink-secondary">/ {monthlyLimit}</span>
                </>
              ) : (
                <span className="text-sm text-ink-secondary">cargando…</span>
              )}
            </div>
            {usage?.plan && !isUnlimited && (
              <p className="text-xs text-ink-secondary mt-1.5">
                Plan <span className="text-brand font-medium">{usage.plan}</span> · {monthlyLimit} videos/mes incluidos
              </p>
            )}
          </div>
          {!isUnlimited && monthlyLimit && (
            <span className={`text-2xl font-bold tracking-tight ${
              usagePercent >= 100 ? "text-red-400" :
              usagePercent >= 80 ? "text-amber-400" :
              "text-brand-light"
            }`}>
              {Math.round(usagePercent)}%
            </span>
          )}
        </div>
        {!isUnlimited && monthlyLimit && (
          <div className="w-full h-2 bg-surface-3/60 rounded-full overflow-hidden">
            <div
              className={`h-full rounded-full transition-all duration-700 ease-out ${
                usagePercent >= 100
                  ? "bg-gradient-to-r from-amber-500 to-red-500"
                  : usagePercent >= 80
                    ? "bg-gradient-to-r from-brand to-amber-400"
                    : "bg-gradient-to-r from-brand to-brand-light"
              }`}
              style={{ width: `${Math.max(2, Math.min(100, usagePercent))}%` }}
            />
          </div>
        )}
        {usage?.overage > 0 && (
          <div className="mt-4 flex items-center gap-2.5 px-3.5 py-2.5 rounded-xl bg-amber-500/10 ring-1 ring-amber-500/20">
            <svg className="w-4 h-4 text-amber-400 shrink-0" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
              <path d="M12 9v4M12 17h.01"/><circle cx="12" cy="12" r="10"/>
            </svg>
            <span className="text-xs text-amber-200">
              {usage.overage} excedentes × ${usage.overage_cost_per_video} = <span className="font-semibold">${usage.overage_total}</span>
            </span>
          </div>
        )}
      </div>

      {/* ─── En proceso ahora — only when there's live work ─────── */}
      {processing.length > 0 && (
        <div className="mb-10">
          <div className="flex items-center justify-between mb-3">
            <SectionLabel>En proceso</SectionLabel>
            <span className="text-[10px] text-gray-500 uppercase tracking-[0.18em]">
              {processing.length} {processing.length === 1 ? "video" : "videos"}
            </span>
          </div>
          <div className="rounded-card p-2 bg-surface-2/30 ring-1 ring-white/[0.03]">
            {processing.slice(0, 5).map((job) => (
              <ProcessingRow key={job.job_id} job={job} onSelect={onSelectJob} t={t} />
            ))}
          </div>
        </div>
      )}

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

      {/* ─── Empty state — only when there is literally nothing ─── */}
      {history.length === 0 && (
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
