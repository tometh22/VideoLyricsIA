import { useState, useMemo } from "react";
import { useI18n } from "../i18n";
import { useMediaUrl } from "../mediaUrl";

const API = import.meta.env.VITE_API_URL || "";

function timeAgo(ts) {
  if (!ts) return "";
  const diff = Date.now() / 1000 - ts;
  if (diff < 60) return "ahora";
  if (diff < 3600) return `${Math.floor(diff / 60)}m`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h`;
  return `${Math.floor(diff / 86400)}d`;
}

function FilterPill({ active, count, onClick, children }) {
  return (
    <button
      onClick={onClick}
      className={`flex items-center gap-2 h-9 px-4 rounded-full text-xs font-medium transition-all ${
        active
          ? "bg-brand/15 text-brand-light ring-1 ring-brand/40"
          : "bg-surface-2/40 text-ink-secondary ring-1 ring-white/[0.04] hover:ring-white/[0.08] hover:text-white"
      }`}
    >
      {children}
      <span className={`text-[10px] tabular-nums ${active ? "text-brand-light/80" : "text-gray-500"}`}>
        {count}
      </span>
    </button>
  );
}

function StatusBadge({ status, t }) {
  const map = {
    done:               { label: t("history.done"),                       cls: "bg-accent/15 text-accent ring-1 ring-accent/30" },
    pending_review:     { label: t("batch.pending_review") || "Pending",  cls: "bg-amber-500/15 text-amber-300 ring-1 ring-amber-500/30" },
    processing:         { label: t("history.processing"),                 cls: "bg-brand/15 text-brand-light ring-1 ring-brand/30" },
    queued:             { label: "En cola",                                cls: "bg-surface-3/60 text-ink-secondary ring-1 ring-white/[0.06]" },
    error:              { label: t("history.error"),                      cls: "bg-red-500/15 text-red-300 ring-1 ring-red-500/30" },
    validation_failed:  { label: t("batch.validation_failed") || "Failed", cls: "bg-red-500/15 text-red-300 ring-1 ring-red-500/30" },
  };
  const cfg = map[status] || map.processing;
  return (
    <span className={`inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-medium backdrop-blur-md ${cfg.cls}`}>
      {cfg.label}
    </span>
  );
}

function VideoCard({ job, onSelect, t }) {
  const name = (job.filename || "").replace(/\.mp3$/i, "");
  const songName = name.includes(" - ") ? name.split(" - ").slice(1).join(" - ") : name;
  const artistName = job.artist || (name.includes(" - ") ? name.split(" - ")[0] : "");
  const showThumb = job.status === "done" || job.status === "pending_review";
  const thumbSrc = useMediaUrl(showThumb ? job.job_id : "", "thumbnail", "preview");

  return (
    <button
      onClick={() => onSelect(job.job_id)}
      className="rounded-card overflow-hidden text-left group bg-surface-2/40 hover:bg-surface-2/70 ring-1 ring-white/[0.04] hover:ring-white/[0.10] transition-all"
    >
      <div className="aspect-video bg-surface-3/30 relative overflow-hidden">
        {showThumb && thumbSrc && (
          <img
            src={thumbSrc}
            alt=""
            className="w-full h-full object-cover group-hover:scale-[1.04] transition-transform duration-500"
            onError={(e) => { e.target.style.display = "none"; }}
          />
        )}
        {(job.status === "processing" || job.status === "queued") && (
          <div className="absolute inset-0 flex items-center justify-center">
            <div className="w-7 h-7 border-2 border-brand border-t-transparent rounded-full animate-spin" />
          </div>
        )}
        {(job.status === "error" || job.status === "validation_failed") && (
          <div className="absolute inset-0 flex items-center justify-center bg-red-500/[0.04]">
            <svg className="w-7 h-7 text-red-400/60" fill="none" stroke="currentColor" strokeWidth="1.5" viewBox="0 0 24 24">
              <circle cx="12" cy="12" r="10"/><path d="M15 9l-6 6M9 9l6 6"/>
            </svg>
          </div>
        )}

        <div className="absolute top-2.5 right-2.5">
          <StatusBadge status={job.status} t={t} />
        </div>

        {(job.status === "done" || job.status === "pending_review") && (
          <div className="absolute inset-0 flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity bg-black/30">
            <div className="w-11 h-11 rounded-full bg-white/15 backdrop-blur-md flex items-center justify-center ring-1 ring-white/20">
              <svg className="w-4 h-4 text-white ml-0.5" fill="currentColor" viewBox="0 0 24 24">
                <path d="M8 5v14l11-7z"/>
              </svg>
            </div>
          </div>
        )}
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

const FILTERS = [
  { id: "all",     label: "Todos",     match: () => true },
  { id: "done",    label: "Listos",    match: (j) => j.status === "done" },
  { id: "pending", label: "Pendientes", match: (j) => j.status === "pending_review" },
  { id: "active",  label: "En curso",  match: (j) => j.status === "processing" || j.status === "queued" },
  { id: "failed",  label: "Fallidos",  match: (j) => j.status === "error" || j.status === "validation_failed" },
];

export default function HistoryView({ history, onSelect, onBack }) {
  const { t } = useI18n();
  const [filter, setFilter] = useState("all");

  const counts = useMemo(() => {
    const c = {};
    for (const f of FILTERS) c[f.id] = history.filter(f.match).length;
    return c;
  }, [history]);

  const visible = useMemo(() => {
    const f = FILTERS.find((x) => x.id === filter) || FILTERS[0];
    return history.filter(f.match);
  }, [history, filter]);

  return (
    <div className="w-full max-w-4xl animate-fade-in">
      {/* ─── Header ─────────────────────────────────────────────── */}
      <div className="flex items-end justify-between mb-8">
        <div className="flex items-center gap-3">
          <button onClick={onBack}
            className="w-9 h-9 rounded-xl bg-surface-2/40 ring-1 ring-white/[0.04] hover:ring-white/[0.08] hover:text-white flex items-center justify-center text-gray-400 transition-colors">
            <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
              <path d="M19 12H5M12 19l-7-7 7-7" />
            </svg>
          </button>
          <div>
            <h1 className="text-[28px] leading-tight font-bold tracking-tight">{t("history.title")}</h1>
            <p className="text-sm text-ink-secondary mt-1">
              {history.length === 0
                ? "Aún no hay videos"
                : `${history.length} ${history.length === 1 ? "video en total" : "videos en total"}`}
            </p>
          </div>
        </div>
      </div>

      {/* ─── Filters ─────────────────────────────────────────────── */}
      {history.length > 0 && (
        <div className="flex flex-wrap gap-2 mb-6">
          {FILTERS.filter((f) => f.id === "all" || counts[f.id] > 0).map((f) => (
            <FilterPill
              key={f.id}
              active={filter === f.id}
              count={counts[f.id]}
              onClick={() => setFilter(f.id)}
            >
              {f.label}
            </FilterPill>
          ))}
        </div>
      )}

      {/* ─── Grid ─────────────────────────────────────────────── */}
      {visible.length === 0 ? (
        <div className="rounded-card p-14 text-center bg-surface-2/30 ring-1 ring-white/[0.04]">
          <p className="text-sm text-ink-secondary">
            {history.length === 0 ? t("history.empty") : "No hay videos en esta vista"}
          </p>
        </div>
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
          {visible.map((job) => (
            <VideoCard key={job.job_id} job={job} onSelect={onSelect} t={t} />
          ))}
        </div>
      )}
    </div>
  );
}
