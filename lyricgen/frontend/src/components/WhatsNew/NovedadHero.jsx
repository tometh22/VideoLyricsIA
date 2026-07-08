import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useI18n } from "../../i18n";
import { CHANGELOG } from "../../changelog";
import ReleaseVisual from "./ReleaseVisual";

const DISMISS_KEY = "genly_novedad_hero_dismissed_id";

export default function NovedadHero() {
  const { t } = useI18n();
  const navigate = useNavigate();
  const [featured, ...secondary] = CHANGELOG;
  const [dismissed, setDismissed] = useState(() => {
    try {
      return localStorage.getItem(DISMISS_KEY) === (featured ? featured.id : "");
    } catch {
      return false;
    }
  });
  if (!featured || dismissed) return null;

  const dismiss = () => {
    setDismissed(true);
    try { localStorage.setItem(DISMISS_KEY, featured.id); } catch {}
  };

  return (
    <section className="mb-6 animate-fade-in">
      <div className="relative overflow-hidden rounded-card bg-surface-2/60 ring-1 ring-white/[0.06] shadow-depth">
        <div className="absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-brand/70 to-transparent" />
        <button
          type="button"
          onClick={dismiss}
          aria-label={t("common.cancel") || "Cerrar"}
          className="absolute right-4 top-4 z-10 grid h-8 w-8 place-items-center rounded-full bg-black/30 text-gray-500 transition-colors hover:bg-black/50 hover:text-white"
        >
          ×
        </button>

        <div className="grid items-center gap-6 p-5 lg:grid-cols-[0.92fr_1fr] lg:p-7">
          <div className="flex flex-col justify-center pr-2 lg:pr-6">
            <div className="flex items-center gap-2">
              <span className="rounded-full bg-accent/14 px-2.5 py-1 text-[10px] font-extrabold uppercase tracking-[0.12em] text-accent ring-1 ring-accent/25">
                {t("announce.scenes_badge") || "NUEVO"}
              </span>
              <span className="text-[10px] uppercase tracking-[0.18em] text-gray-500">{t("whatsnew.title")}</span>
            </div>
            <h2 className="mt-4 max-w-xl text-[25px] font-extrabold leading-tight tracking-tight text-white">
              {t(featured.titleKey)}
            </h2>
            {featured.taglineKey && (
              <p className="mt-2 max-w-xl text-[14px] font-semibold leading-relaxed text-brand-light">
                {t(featured.taglineKey)}
              </p>
            )}
            {Array.isArray(featured.highlightKeys) && featured.highlightKeys.length > 0 && (
              <ul className="mt-4 max-w-xl space-y-2">
                {featured.highlightKeys.slice(0, 3).map((key) => (
                  <li key={key} className="text-[13px] leading-snug text-ink-secondary">{t(key)}</li>
                ))}
              </ul>
            )}
            {featured.ctaTo && (
              <div className="mt-5">
                <button
                  type="button"
                  onClick={() => navigate(featured.ctaTo)}
                  className="inline-flex h-10 items-center gap-2 rounded-button bg-brand px-5 text-sm font-semibold text-white shadow-glow transition-colors hover:bg-brand-light"
                >
                  {t(featured.ctaKey)}
                  <svg className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="2.3" viewBox="0 0 24 24">
                    <path d="M5 12h14M13 5l7 7-7 7" strokeLinecap="round" strokeLinejoin="round" />
                  </svg>
                </button>
              </div>
            )}
          </div>

          <ReleaseVisual entry={featured} />
        </div>
      </div>

      {secondary.length > 0 && (
        <div className="mt-3 grid gap-3 sm:grid-cols-2">
          {secondary.slice(0, 2).map((entry) => (
            <button
              key={entry.id}
              type="button"
              onClick={() => navigate(entry.ctaTo || "/new")}
              className="grid min-h-[168px] grid-cols-[190px_1fr] items-center gap-4 rounded-2xl bg-surface-2/35 p-4 text-left ring-1 ring-white/[0.045] transition-all hover:bg-surface-2/55 hover:ring-white/[0.08] max-lg:grid-cols-[160px_1fr] max-sm:grid-cols-1"
            >
              <ReleaseVisual entry={entry} compact />
              <span className="min-w-0">
                <span className="block text-[15px] font-bold leading-tight text-white">{t(entry.titleKey)}</span>
                {entry.taglineKey && (
                  <span className="mt-2 block max-w-md text-[12.5px] leading-relaxed text-ink-secondary">{t(entry.taglineKey)}</span>
                )}
              </span>
            </button>
          ))}
        </div>
      )}
    </section>
  );
}
