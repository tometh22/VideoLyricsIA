import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useI18n } from "../../i18n";
import { CHANGELOG } from "../../changelog";

// Hero de lanzamiento en la home: presenta la última novedad del changelog con
// peso de marketing (video + título grande + bajada + beneficios + CTA), no una
// card que se mezcla. Dismissible por usuario (localStorage, por id de entrada).
// Se actualiza solo: la entrada vive en changelog.js.
const DISMISS_KEY = "genly_novedad_hero_dismissed_id";

export default function NovedadHero() {
  const { t } = useI18n();
  const navigate = useNavigate();
  const entry = CHANGELOG.length ? CHANGELOG[0] : null;
  const [dismissed, setDismissed] = useState(() => {
    try { return localStorage.getItem(DISMISS_KEY) === (entry ? entry.id : ""); } catch { return false; }
  });
  if (!entry || dismissed) return null;

  const dismiss = () => {
    setDismissed(true);
    try { localStorage.setItem(DISMISS_KEY, entry.id); } catch { /* storage bloqueado */ }
  };
  const isVideo = (entry.media || "").endsWith(".mp4");

  return (
    <section className="relative mb-6 rounded-card overflow-hidden ring-1 ring-brand/30 bg-gradient-to-br from-brand/[0.14] via-surface-2 to-surface-2 shadow-glow animate-fade-in">
      <button
        onClick={dismiss}
        aria-label="Cerrar"
        className="absolute top-3 right-3 z-10 w-7 h-7 rounded-full bg-black/40 hover:bg-black/60 text-gray-300 hover:text-white grid place-items-center text-sm transition-colors"
      >×</button>

      <div className="grid md:grid-cols-[1.1fr_1fr]">
        {entry.media && (
          <div className="relative min-h-[200px] bg-black">
            {isVideo ? (
              <video src={entry.media} autoPlay muted loop playsInline className="absolute inset-0 w-full h-full object-cover" />
            ) : (
              <img src={entry.media} alt="" className="absolute inset-0 w-full h-full object-cover" />
            )}
            {/* fundido hacia el contenido para integrar el video con el panel */}
            <div className="absolute inset-0 bg-gradient-to-t md:bg-gradient-to-r from-surface-2/70 via-transparent to-transparent" />
          </div>
        )}

        <div className="p-5 md:p-7 flex flex-col justify-center gap-3">
          <div className="flex items-center gap-2">
            <span className="text-[10px] font-bold tracking-[0.08em] px-2 py-0.5 rounded bg-accent text-white">
              {t("announce.scenes_badge") || "NUEVO"}
            </span>
            <span className="text-[10px] uppercase tracking-[0.12em] text-gray-500">
              {t("whatsnew.title") || "Novedades"}
            </span>
          </div>

          <h2 className="text-[19px] md:text-[22px] font-extrabold text-white leading-tight">
            {t(entry.titleKey)}
          </h2>

          {entry.taglineKey && (
            <p className="text-[13px] text-brand-light font-medium leading-snug">{t(entry.taglineKey)}</p>
          )}

          {Array.isArray(entry.highlightKeys) && entry.highlightKeys.length > 0 && (
            <ul className="space-y-1.5 mt-0.5">
              {entry.highlightKeys.map((k) => (
                <li key={k} className="text-[12.5px] text-ink-secondary leading-snug">{t(k)}</li>
              ))}
            </ul>
          )}

          {entry.ctaTo && (
            <div className="mt-2">
              <button
                onClick={() => navigate(entry.ctaTo)}
                className="inline-flex items-center gap-1.5 text-[13px] font-semibold px-4 py-2 rounded-lg bg-brand hover:bg-brand-light text-white shadow-glow transition-colors"
              >
                {t(entry.ctaKey) || "Probar"}
                <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"><path d="M5 12h14M13 5l7 7-7 7" strokeLinecap="round" strokeLinejoin="round"/></svg>
              </button>
            </div>
          )}
        </div>
      </div>
    </section>
  );
}
