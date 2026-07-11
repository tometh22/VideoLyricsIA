import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useI18n } from "../../i18n";
import { CHANGELOG } from "../../changelog";
import ReleaseVisual from "./ReleaseVisual";

const DISMISS_KEY = "genly_novedad_hero_dismissed_id";

const RELEASE_FMT = new Intl.DateTimeFormat("es-AR", {
  day: "numeric",
  month: "short",
});

function releaseDate(date) {
  const d = new Date(`${date}T12:00:00`);
  return Number.isNaN(d.getTime()) ? date : RELEASE_FMT.format(d);
}

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

  const updates = secondary.slice(0, 3);

  return (
    <section className="mb-5 animate-fade-in" data-tour="whatsnew-release">
      <div className="relative overflow-hidden rounded-xl bg-[#111118]/86 ring-1 ring-white/[0.065]">
        <div className="absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-white/20 to-transparent" />
        <button
          type="button"
          onClick={dismiss}
          aria-label={t("common.cancel") || "Cerrar"}
          className="absolute right-2.5 top-2.5 z-20 grid h-7 w-7 place-items-center rounded-lg text-[16px] leading-none text-gray-500 transition-colors hover:bg-white/[0.05] hover:text-white"
        >
          ×
        </button>

        <div className="grid min-h-[112px] gap-3 p-4 pr-11 md:grid-cols-[minmax(0,1fr)_220px] md:items-center md:gap-4 md:pr-12">
          <div className="min-w-0">
            <div className="mb-2.5 flex flex-wrap items-center gap-2">
              <span className="rounded-full bg-white/[0.045] px-2.5 py-1 text-[10px] font-semibold uppercase text-gray-400 ring-1 ring-white/[0.06]">
                {t("whatsnew.title") || "Novedades"}
              </span>
              <span className="rounded-full bg-accent/[0.08] px-2.5 py-1 text-[10px] font-semibold text-accent ring-1 ring-accent/20">
                {releaseDate(featured.date)}
              </span>
              <span className="inline-flex items-center gap-1.5 rounded-full bg-white/[0.035] px-2.5 py-1 text-[10px] font-medium text-gray-400 ring-1 ring-white/[0.05]">
                <span className="h-1.5 w-1.5 rounded-full bg-accent" />
                activo ahora
              </span>
            </div>

            <h2 className="max-w-2xl text-[18px] font-bold leading-tight text-white md:text-[20px]">
              {t(featured.titleKey)}
            </h2>
            {featured.taglineKey && (
              <p className="mt-1.5 max-w-2xl text-[12.5px] font-medium leading-relaxed text-ink-secondary">
                {t(featured.taglineKey)}
              </p>
            )}

            <div className="mt-2 hidden flex-wrap items-center gap-2.5 xl:flex">
              {Array.isArray(featured.highlightKeys) && featured.highlightKeys.slice(0, 2).map((key) => (
                <span
                  key={key}
                  className="inline-flex max-w-full items-center gap-1.5 rounded-full bg-white/[0.035] px-2.5 py-1 text-[11px] text-gray-300 ring-1 ring-white/[0.05]"
                >
                  <span className="h-1 w-1 shrink-0 rounded-full bg-accent/80" />
                  <span className="truncate">{t(key)}</span>
                </span>
              ))}
            </div>

            <div className="mt-4 flex flex-wrap items-center gap-3">
              {featured.ctaTo && (
                <button
                  type="button"
                  onClick={() => navigate(featured.ctaTo)}
                  className="inline-flex h-9 items-center gap-1.5 rounded-lg bg-brand px-3.5 text-[12px] font-semibold text-white shadow-[0_6px_18px_rgba(109,74,255,0.22)] transition-colors hover:bg-brand-light"
                >
                  {t(featured.ctaKey)}
                  <svg className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth="2.4" viewBox="0 0 24 24">
                    <path d="M5 12h14M13 5l7 7-7 7" strokeLinecap="round" strokeLinejoin="round" />
                  </svg>
                </button>
              )}
              {updates.length > 0 && (
                <div className="flex min-w-0 flex-wrap items-center gap-1.5 text-[11px] text-gray-500">
                  {updates.map((entry) => (
                    <button
                      key={entry.id}
                      type="button"
                      onClick={() => navigate(entry.ctaTo || "/new")}
                      className="max-w-[190px] truncate rounded-full bg-white/[0.028] px-2.5 py-1 text-left text-gray-400 ring-1 ring-white/[0.05] transition-colors hover:bg-white/[0.05] hover:text-white"
                      title={t(entry.titleKey)}
                    >
                      {t(entry.titleKey)}
                    </button>
                  ))}
                </div>
              )}
            </div>
          </div>

          <div className="hidden min-w-0 lg:block">
            <ReleaseVisual entry={featured} compact />
          </div>
        </div>
      </div>
    </section>
  );
}
