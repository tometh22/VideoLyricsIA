import { useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { useI18n } from "../../i18n";
import { useChangelog } from "./useChangelog";
import ReleaseVisual from "./ReleaseVisual";

function fmtDate(iso, locale) {
  try {
    return new Date(iso + "T00:00:00").toLocaleDateString(locale || undefined, {
      day: "numeric", month: "short", year: "numeric",
    });
  } catch {
    return iso;
  }
}

export default function WhatsNewPanel({ onClose }) {
  const { t, lang } = useI18n();
  const navigate = useNavigate();
  const { entries } = useChangelog();
  const [latest, ...rest] = entries;

  useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const go = (entry) => {
    if (!entry.ctaTo) return;
    onClose();
    navigate(entry.ctaTo);
  };

  return (
    <div className="fixed inset-0 z-[70] flex justify-end bg-black/55 animate-fade-in" onClick={onClose}>
      <div
        role="dialog"
        aria-modal="true"
        aria-label={t("whatsnew.title") || "Novedades"}
        className="h-full w-full max-w-md overflow-y-auto bg-surface-2 shadow-glow ring-1 ring-white/[0.06]"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="sticky top-0 z-10 border-b border-white/[0.06] bg-surface-2/95 px-5 py-4 backdrop-blur-xl">
          <div className="flex items-start justify-between gap-4">
            <div>
              <h2 className="text-[15px] font-bold text-white">{t("whatsnew.title") || "Novedades"}</h2>
              {latest && (
                <p className="mt-1 text-[11px] text-gray-500">
                  {t("whatsnew.updated", { date: fmtDate(latest.date, lang) })}
                </p>
              )}
            </div>
            <button
              onClick={onClose}
              aria-label={t("common.cancel") || "Cerrar"}
              className="rounded text-lg leading-none text-gray-400 outline-none transition-colors hover:text-white focus-visible:ring-2 focus-visible:ring-brand/60"
            >
              ×
            </button>
          </div>
        </div>

        {entries.length === 0 ? (
          <p className="p-6 text-[12px] text-ink-secondary">{t("whatsnew.empty") || "No hay novedades por ahora."}</p>
        ) : (
          <div className="space-y-5 p-4">
            <article className="overflow-hidden rounded-card bg-surface/45 ring-1 ring-white/[0.06]">
              <ReleaseVisual entry={latest} compact />
              <div className="p-4">
                <p className="text-[10px] uppercase tracking-[0.16em] text-gray-500">{fmtDate(latest.date, lang)}</p>
                <h3 className="mt-1 text-[15px] font-bold leading-tight text-white">{t(latest.titleKey)}</h3>
                {latest.taglineKey && (
                  <p className="mt-1 text-[12px] font-medium leading-snug text-brand-light">{t(latest.taglineKey)}</p>
                )}
                {Array.isArray(latest.highlightKeys) && latest.highlightKeys.length > 0 ? (
                  <ul className="mt-3 space-y-1.5">
                    {latest.highlightKeys.slice(0, 3).map((k) => (
                      <li key={k} className="text-[12px] leading-snug text-ink-secondary">{t(k)}</li>
                    ))}
                  </ul>
                ) : (
                  <p className="mt-2 text-[12px] leading-relaxed text-ink-secondary">{t(latest.bodyKey)}</p>
                )}
                {latest.ctaTo && (
                  <button
                    type="button"
                    onClick={() => go(latest)}
                    className="mt-4 text-[12px] font-semibold text-brand-light transition-colors hover:text-white"
                  >
                    {t(latest.ctaKey)}
                  </button>
                )}
              </div>
            </article>

            {rest.length > 0 && (
              <div>
                <p className="mb-3 text-[10px] uppercase tracking-[0.18em] text-gray-500">
                  {t("whatsnew.timeline")}
                </p>
                <ol className="relative space-y-4 border-l border-white/[0.08] pl-4">
                  {rest.map((entry) => (
                    <li key={entry.id} className="relative">
                      <span className="absolute -left-[21px] top-1.5 h-2 w-2 rounded-full bg-brand ring-4 ring-surface-2" />
                      <button
                        type="button"
                        onClick={() => go(entry)}
                        className="w-full rounded-xl p-2 text-left transition-colors hover:bg-white/[0.035]"
                      >
                        <p className="text-[10px] uppercase tracking-[0.14em] text-gray-500">{fmtDate(entry.date, lang)}</p>
                        <h3 className="mt-1 text-[13px] font-bold leading-tight text-white">{t(entry.titleKey)}</h3>
                        <p className="mt-1 text-[12px] leading-snug text-ink-secondary">
                          {entry.taglineKey ? t(entry.taglineKey) : t(entry.bodyKey)}
                        </p>
                      </button>
                    </li>
                  ))}
                </ol>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
