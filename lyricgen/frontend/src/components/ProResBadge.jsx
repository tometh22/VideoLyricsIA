import { useI18n } from "../i18n";

/**
 * ProResBadge — tiny pill rendered on job cards (Dashboard,
 * HistoryView) and the JobDetail header to let the operator see at a
 * glance whether the lazy ProRes transcode has finished.
 *
 * Rules:
 *   • job not UMG (delivery_profile != umg/both)  →  render nothing.
 *   • UMG + prores_ready === true                  →  green "ProRes listo".
 *   • UMG + prores_ready === false (or missing)    →  amber pulsing
 *     "Generando ProRes…".
 *
 * The pill is informational only — clicking does nothing. The actual
 * download happens from the dedicated buttons in JobDetail.
 *
 * Props:
 *   • deliveryProfile: string ("youtube" | "umg" | "both")
 *   • proresReady: boolean | null
 *   • jobStatus: string — when not in (done, pending_review) the badge
 *     stays hidden, so we don't surface "generating ProRes" while the
 *     MP4 itself is still being rendered.
 *   • size: "sm" | "md" — visual scale (defaults to sm).
 */
export default function ProResBadge({
  deliveryProfile,
  proresReady,
  jobStatus,
  size = "sm",
}) {
  const { t } = useI18n();

  const isUmg = deliveryProfile === "umg" || deliveryProfile === "both";
  if (!isUmg) return null;

  // Don't show the badge until the underlying MP4 render is finished —
  // a pulsing "generating ProRes" while the lyric video is still on
  // step 3/5 would be confusing.
  if (jobStatus !== "done" && jobStatus !== "pending_review") return null;

  const ready = proresReady === true;
  const sizeClasses = size === "md"
    ? "px-2.5 py-1 text-[11px]"
    : "px-2 py-0.5 text-[10px]";

  if (ready) {
    return (
      <span
        className={`inline-flex items-center gap-1.5 rounded-full bg-accent/15 text-accent ring-1 ring-accent/30 font-medium ${sizeClasses}`}
        title={t("prores.ready_tooltip") || "Master ProRes listo para descargar"}
      >
        <svg className="w-2.5 h-2.5" fill="none" stroke="currentColor" strokeWidth="3" viewBox="0 0 24 24">
          <polyline points="20 6 9 17 4 12" />
        </svg>
        {t("prores.ready") || "ProRes listo"}
      </span>
    );
  }

  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full bg-amber-500/10 text-amber-300 ring-1 ring-amber-500/25 font-medium ${sizeClasses}`}
      title={t("prores.generating_tooltip")
        || "El máster ProRes se está generando en segundo plano. Tarda 1-3 minutos."}
    >
      <span className="relative flex h-2 w-2">
        <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-amber-400 opacity-60" />
        <span className="relative inline-flex rounded-full h-2 w-2 bg-amber-400" />
      </span>
      {t("prores.generating") || "Generando ProRes…"}
    </span>
  );
}
