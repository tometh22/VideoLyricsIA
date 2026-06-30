import { useState, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { useI18n } from "../../i18n";
import { useChangelog } from "./useChangelog";

// Modal de anuncio one-time, data-driven por el changelog: muestra la entrada
// `featured` (si el usuario no la vio aún). Reemplaza al ScenesAnnounceModal a
// medida — ahora cualquier feature grande se anuncia agregando una entrada con
// `featured: true` en src/changelog.js.
export default function WhatsNewModal({ user }) {
  const { t } = useI18n();
  const navigate = useNavigate();
  const { modalEntry, dismissModal } = useChangelog();
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (user && modalEntry) setOpen(true);
  }, [user, modalEntry]);

  const close = () => { setOpen(false); dismissModal(); };

  useEffect(() => {
    if (!open) return undefined;
    const onKey = (e) => { if (e.key === "Escape") close(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  if (!open || !modalEntry) return null;
  const e = modalEntry;
  const isVideo = (e.media || "").endsWith(".mp4");

  return (
    <div
      className="fixed inset-0 z-[60] grid place-items-center bg-black/70 p-4 animate-fade-in"
      onClick={close}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label={t(e.titleKey) || "Novedad"}
        className="w-full max-w-md rounded-card bg-surface-2 ring-1 ring-brand/25 overflow-hidden shadow-glow"
        onClick={(ev) => ev.stopPropagation()}
      >
        {e.media && (
          <div className="relative">
            {isVideo ? (
              <video src={e.media} autoPlay muted loop playsInline className="w-full aspect-video object-cover bg-black" />
            ) : (
              <img src={e.media} alt="" className="w-full aspect-video object-cover bg-black" />
            )}
            <span className="absolute top-2 left-2 text-[10px] font-bold tracking-[0.05em] px-2 py-0.5 rounded bg-accent text-white">
              {t("announce.scenes_badge") || "NUEVO"}
            </span>
          </div>
        )}
        <div className="p-5">
          <h3 className="text-[17px] font-extrabold text-white text-center leading-tight">{t(e.titleKey)}</h3>
          {e.taglineKey && (
            <p className="text-[12.5px] text-brand-light font-medium text-center mt-1.5 leading-snug">{t(e.taglineKey)}</p>
          )}
          {Array.isArray(e.highlightKeys) && e.highlightKeys.length > 0 ? (
            <ul className="mt-3.5 space-y-2 text-left">
              {e.highlightKeys.map((k) => (
                <li key={k} className="text-[12.5px] text-ink-secondary leading-snug">{t(k)}</li>
              ))}
            </ul>
          ) : (
            <p className="text-[12.5px] text-ink-secondary mt-2 leading-relaxed text-center">{t(e.bodyKey)}</p>
          )}
          <div className="mt-4 flex gap-2 justify-center">
            <button
              onClick={close}
              className="text-[12px] font-medium px-3.5 py-1.5 rounded-lg bg-white/[0.04] hover:bg-white/[0.08] text-gray-300"
            >
              {t("announce.later") || "Más tarde"}
            </button>
            {e.ctaTo && (
              <button
                onClick={() => { close(); navigate(e.ctaTo); }}
                className="text-[12px] font-semibold px-3.5 py-1.5 rounded-lg bg-brand hover:bg-brand-light text-white"
              >
                {t(e.ctaKey) || "Probar"}
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
