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
  const releaseVersion = "v2.7";

  return (
    <section className="mb-8 animate-fade-in">
      <div className="relative overflow-hidden rounded-card bg-[#111118]/88 ring-1 ring-white/[0.075] shadow-depth">
        <div className="absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-accent/70 to-transparent" />
        <div className="absolute inset-0 bg-[radial-gradient(circle_at_76%_16%,rgba(20,200,168,0.13),transparent_34%),linear-gradient(180deg,rgba(255,255,255,0.035),transparent_42%)]" />
        <button
          type="button"
          onClick={dismiss}
          aria-label={t("common.cancel") || "Cerrar"}
          className="absolute right-4 top-4 z-20 grid h-8 w-8 place-items-center rounded-full bg-black/35 text-gray-500 ring-1 ring-white/[0.06] transition-colors hover:bg-black/55 hover:text-white"
        >
          ×
        </button>

        <div className="relative z-10 grid items-center gap-7 p-5 lg:grid-cols-[0.86fr_1fr] lg:p-7">
          <div className="flex min-w-0 flex-col justify-center pr-2 lg:pr-8">
            <div className="flex flex-wrap items-center gap-2">
              <span className="rounded-full bg-accent/12 px-2.5 py-1 text-[10px] font-extrabold uppercase text-accent ring-1 ring-accent/25">
                Release {releaseVersion}
              </span>
              <span className="rounded-full bg-white/[0.045] px-2.5 py-1 text-[10px] font-semibold uppercase text-gray-400 ring-1 ring-white/[0.06]">
                {releaseDate(featured.date)}
              </span>
              <span className="inline-flex items-center gap-1.5 rounded-full bg-emerald-400/[0.08] px-2.5 py-1 text-[10px] font-semibold text-emerald-300 ring-1 ring-emerald-400/20">
                <span className="h-1.5 w-1.5 rounded-full bg-emerald-300 shadow-[0_0_12px_rgba(110,231,183,.7)]" />
                activo ahora
              </span>
            </div>
            <p className="mt-5 text-[11px] font-semibold uppercase text-gray-500">
              {t("whatsnew.title")} de julio
            </p>
            <h2 className="mt-2 max-w-xl text-[30px] font-extrabold leading-[1.08] tracking-normal text-white">
              {t(featured.titleKey)}
            </h2>
            {featured.taglineKey && (
              <p className="mt-3 max-w-xl text-[15px] font-semibold leading-relaxed text-brand-light">
                {t(featured.taglineKey)}
              </p>
            )}
            {Array.isArray(featured.highlightKeys) && featured.highlightKeys.length > 0 && (
              <ul className="mt-5 grid max-w-xl gap-2">
                {featured.highlightKeys.slice(0, 3).map((key) => (
                  <li key={key} className="flex items-start gap-2 text-[13px] leading-snug text-ink-secondary">
                    <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-accent/80" />
                    <span>{t(key)}</span>
                  </li>
                ))}
              </ul>
            )}
            {featured.ctaTo && (
              <div className="mt-6 flex flex-wrap items-center gap-3">
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
                <span className="text-[12px] text-gray-500">
                  {updates.length + 1} mejoras publicadas esta semana
                </span>
              </div>
            )}
          </div>

          <ReleaseVisual entry={featured} />
        </div>

        <div className="relative z-10 border-t border-white/[0.06] bg-black/[0.12] px-4 py-4 lg:px-5">
          <div className="mb-3 flex items-center justify-between gap-3">
            <p className="text-[10px] font-semibold uppercase text-gray-500">También llegó</p>
            <p className="hidden text-[11px] text-gray-500 sm:block">Historial breve de mejoras recientes</p>
          </div>
          <div className="grid gap-3 lg:grid-cols-3">
            {updates.map((entry, idx) => (
              <button
                key={entry.id}
                type="button"
                onClick={() => navigate(entry.ctaTo || "/new")}
                className="group grid min-h-[132px] grid-cols-[132px_1fr] items-center gap-3 rounded-2xl bg-white/[0.035] p-3 text-left ring-1 ring-white/[0.055] transition-all hover:bg-white/[0.055] hover:ring-white/[0.10] max-sm:grid-cols-1"
              >
                <ReleaseVisual entry={entry} compact />
                <span className="min-w-0">
                  <span className="mb-2 flex items-center gap-2">
                    <span className="grid h-5 w-5 place-items-center rounded-full bg-accent/10 text-[10px] font-bold text-accent ring-1 ring-accent/20">
                      {idx + 1}
                    </span>
                    <span className="text-[10px] font-semibold uppercase text-gray-500">
                      {releaseDate(entry.date)}
                    </span>
                  </span>
                  <span className="block text-[14px] font-bold leading-tight text-white">{t(entry.titleKey)}</span>
                  {entry.taglineKey && (
                    <span className="mt-1.5 block text-[12px] leading-relaxed text-ink-secondary">{t(entry.taglineKey)}</span>
                  )}
                </span>
              </button>
            ))}
          </div>
        </div>
      </div>
    </section>
  );
}
