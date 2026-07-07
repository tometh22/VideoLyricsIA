import { useI18n } from "../../i18n";

// Per-asset progress rows for background publishes, visual language
// borrowed from BatchProgress: icon + label + progress bar.

function StatusIcon({ status }) {
  if (status === "published") {
    return (
      <div className="w-8 h-8 rounded-xl bg-accent/10 flex items-center justify-center shrink-0">
        <svg className="w-4 h-4 text-accent" fill="none" stroke="currentColor" strokeWidth="2.5" viewBox="0 0 24 24">
          <polyline points="20 6 9 17 4 12" />
        </svg>
      </div>
    );
  }
  if (status === "failed" || status === "canceled") {
    return (
      <div className="w-8 h-8 rounded-xl bg-red-500/10 flex items-center justify-center shrink-0">
        <svg className="w-4 h-4 text-red-400" fill="none" stroke="currentColor" strokeWidth="2.5" viewBox="0 0 24 24">
          <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
        </svg>
      </div>
    );
  }
  if (status === "queued" || status === "scheduled") {
    return (
      <div className="w-8 h-8 rounded-xl bg-surface-3/50 flex items-center justify-center shrink-0">
        <svg className="w-4 h-4 text-gray-500" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
          <circle cx="12" cy="12" r="9" /><polyline points="12 7 12 12 15 14" />
        </svg>
      </div>
    );
  }
  return (
    <div className="w-8 h-8 rounded-xl bg-brand/10 flex items-center justify-center shrink-0">
      <div className="w-4 h-4 border-2 border-brand border-t-transparent rounded-full animate-spin" />
    </div>
  );
}

function statusLabel(t, row) {
  switch (row.status) {
    case "queued": return t("yt.progress.queued");
    case "scheduled": return t("yt.progress.scheduled");
    case "uploading":
      return row.progress >= 100
        ? t("yt.progress.processing")
        : `${t("yt.progress.uploading")} ${row.progress || 0}%`;
    case "published": return t("yt.progress.published");
    case "failed": return t("yt.progress.failed");
    case "canceled": return t("yt.progress.canceled");
    default: return row.status;
  }
}

export default function PublishProgress({ rows, onRetry, onCancel }) {
  const { t, lang } = useI18n();

  return (
    <div className="space-y-3">
      {rows.map((row) => (
        <div key={row.id} className="rounded-xl bg-surface-2/40 ring-1 ring-white/[0.04] px-4 py-3">
          <div className="flex items-center gap-3">
            <StatusIcon status={row.status} />
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2 flex-wrap">
                <span className="text-sm text-white font-medium">
                  {row.kind === "short" ? "Short" : "Video"}
                </span>
                <span className="text-xs text-gray-500">{statusLabel(t, row)}</span>
                {row.scheduled_at && (
                  <span className="px-2 py-0.5 rounded-full text-[10px] font-medium bg-amber-500/15 text-amber-400 ring-1 ring-amber-500/30">
                    {t("yt.progress.scheduled_for")}{" "}
                    {new Date(row.scheduled_at).toLocaleString(lang, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}
                  </span>
                )}
              </div>
              {row.status === "uploading" && (
                <div className="mt-2 h-1.5 rounded-full bg-surface-3/60 overflow-hidden">
                  <div className="h-full rounded-full bg-gradient-to-r from-brand to-brand-light transition-all duration-500"
                    style={{ width: `${Math.max(row.progress || 0, 3)}%` }} />
                </div>
              )}
              {row.status === "failed" && row.error && (
                <p className="text-xs text-red-400 mt-1">{row.error}</p>
              )}
            </div>

            <div className="shrink-0 flex items-center gap-3">
              {row.status === "published" && row.video_url && (
                <a href={row.video_url} target="_blank" rel="noopener noreferrer"
                  className="text-xs text-brand-light hover:text-white transition-colors underline">
                  {row.kind === "short" ? t("yt.progress.view_short") : t("yt.progress.view_video")}
                </a>
              )}
              {row.status === "failed" && onRetry && (
                <button onClick={() => onRetry(row)}
                  className="text-xs text-brand-light hover:text-white font-medium transition-colors">
                  {t("yt.progress.retry")}
                </button>
              )}
              {(row.status === "queued" || row.status === "scheduled") && onCancel && (
                <button onClick={() => onCancel(row)}
                  className="text-xs text-gray-500 hover:text-red-400 transition-colors">
                  {t("detail.cancel")}
                </button>
              )}
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}
