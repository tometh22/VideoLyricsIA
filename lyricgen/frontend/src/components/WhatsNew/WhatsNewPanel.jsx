import { useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { useI18n } from "../../i18n";
import { useChangelog } from "./useChangelog";

// Panel deslizable de "Novedades": lista TODAS las entradas del changelog
// (media + fecha + título + cuerpo + CTA opcional). Accesible siempre desde la
// campana del header. Cada feature nueva aparece acá sola, vía src/changelog.js.
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

  useEffect(() => {
    const onKey = (e) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="fixed inset-0 z-[70] flex justify-end bg-black/50 animate-fade-in" onClick={onClose}>
      <div
        role="dialog"
        aria-modal="true"
        aria-label={t("whatsnew.title") || "Novedades"}
        className="w-full max-w-md h-full overflow-y-auto bg-surface-2 ring-1 ring-white/[0.06] shadow-glow"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="sticky top-0 z-10 flex items-center justify-between px-5 py-4 border-b border-white/[0.06] bg-surface-2/95 backdrop-blur-xl">
          <h2 className="text-[15px] font-bold text-white flex items-center gap-2">
            🎉 {t("whatsnew.title") || "Novedades"}
          </h2>
          <button
            onClick={onClose}
            aria-label={t("common.cancel") || "Cerrar"}
            className="text-gray-400 hover:text-white text-lg leading-none outline-none focus-visible:ring-2 focus-visible:ring-brand/60 rounded"
          >×</button>
        </div>

        {entries.length === 0 ? (
          <p className="p-6 text-[12px] text-ink-secondary">{t("whatsnew.empty") || "No hay novedades por ahora."}</p>
        ) : (
          <ul className="p-4 space-y-4">
            {entries.map((e) => {
              const isVideo = (e.media || "").endsWith(".mp4");
              return (
                <li key={e.id} className="rounded-card bg-surface/40 ring-1 ring-white/[0.05] overflow-hidden">
                  {e.media && (
                    isVideo ? (
                      <video src={e.media} autoPlay muted loop playsInline className="w-full aspect-video object-cover bg-black" />
                    ) : (
                      <img src={e.media} alt="" className="w-full aspect-video object-cover bg-black" />
                    )
                  )}
                  <div className="p-4">
                    <p className="text-[10px] uppercase tracking-wide text-ink-secondary">{fmtDate(e.date, lang)}</p>
                    <h3 className="text-[14px] font-bold text-white mt-1 leading-tight">{t(e.titleKey)}</h3>
                    {e.taglineKey && (
                      <p className="text-[12px] text-brand-light font-medium mt-1 leading-snug">{t(e.taglineKey)}</p>
                    )}
                    {Array.isArray(e.highlightKeys) && e.highlightKeys.length > 0 ? (
                      <ul className="mt-2 space-y-1.5">
                        {e.highlightKeys.map((k) => (
                          <li key={k} className="text-[12px] text-ink-secondary leading-snug">{t(k)}</li>
                        ))}
                      </ul>
                    ) : (
                      <p className="text-[12px] text-ink-secondary mt-1 leading-relaxed">{t(e.bodyKey)}</p>
                    )}
                    {e.ctaTo && (
                      <button
                        onClick={() => { onClose(); navigate(e.ctaTo); }}
                        className="mt-3 text-[12px] font-semibold px-3 py-1.5 rounded-lg bg-brand hover:bg-brand-light text-white"
                      >
                        {t(e.ctaKey) || "Probar"}
                      </button>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </div>
  );
}
