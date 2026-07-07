import { useState, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { useI18n } from "../../i18n";
import { useChangelog } from "./useChangelog";

// Modal de anuncio one-time, data-driven por el changelog: muestra la entrada
// `featured` (si el usuario no la vio aún). Reemplaza al ScenesAnnounceModal a
// medida — ahora cualquier feature grande se anuncia agregando una entrada con
// `featured: true` en src/changelog.js.
//
// Diseño (revisión 07/07, "world-class" ≈ Linear/Figma/Stripe): el modal es
// el TEASER, no la ficha técnica — hero visual + título + UNA línea de
// gancho + un CTA. El detalle (highlightKeys/body) se movió al panel
// (WhatsNewPanel), que el usuario abre cuando quiere profundizar. La versión
// anterior metía los 4 bullets acá adentro y se leía como términos y
// condiciones en vez de un lanzamiento.
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
      className="fixed inset-0 z-[60] grid place-items-center bg-black/70 backdrop-blur-sm p-4 animate-fade-in"
      onClick={close}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label={t(e.titleKey) || "Novedad"}
        className="w-full max-w-sm rounded-card bg-surface-2 ring-1 ring-brand/25 overflow-hidden shadow-glow-lg animate-slide-up"
        onClick={(ev) => ev.stopPropagation()}
      >
        <div className="relative">
          {e.media ? (
            isVideo ? (
              <video src={e.media} autoPlay muted loop playsInline className="w-full aspect-video object-cover bg-black" />
            ) : (
              <img src={e.media} alt="" className="w-full aspect-video object-cover bg-black" />
            )
          ) : (
            // Sin demo grabado: hero de gradiente + ícono grande (mismo
            // lenguaje visual que NovedadHero/Landing — orbes de marca
            // difuminados) en vez de dejar el hueco vacío.
            <div className="relative w-full aspect-video overflow-hidden bg-gradient-to-br from-brand/[0.18] via-surface-2 to-surface-2">
              <div className="absolute -top-8 -left-8 w-32 h-32 bg-brand/20 rounded-full blur-[50px]" />
              <div className="absolute -bottom-8 -right-4 w-28 h-28 bg-accent/15 rounded-full blur-[45px]" />
              <div className="absolute inset-0 grid place-items-center">
                <span className="text-5xl drop-shadow-[0_0_24px_rgba(109,74,255,0.5)]">
                  {e.icon || "✨"}
                </span>
              </div>
            </div>
          )}
          <span className="absolute top-3 left-3 text-[10px] font-bold tracking-[0.05em] px-2 py-0.5 rounded bg-accent text-white shadow-depth">
            {t("announce.scenes_badge") || "NUEVO"}
          </span>
          <button
            onClick={close}
            aria-label={t("common.cancel") || "Cerrar"}
            className="absolute top-3 right-3 w-7 h-7 rounded-full bg-black/40 hover:bg-black/60 backdrop-blur-sm
              text-white/80 hover:text-white grid place-items-center text-[15px] leading-none outline-none
              focus-visible:ring-2 focus-visible:ring-brand/60 transition-colors"
          >
            ×
          </button>
        </div>
        <div className="p-6 text-center">
          <h3 className="text-[20px] font-extrabold text-white leading-tight">{t(e.titleKey)}</h3>
          {e.taglineKey && (
            <p className="text-[13.5px] text-ink-secondary font-medium mt-2 leading-snug">{t(e.taglineKey)}</p>
          )}
          {e.ctaTo && (
            <button
              onClick={() => { close(); navigate(e.ctaTo); }}
              className="mt-5 w-full text-[13px] font-semibold px-4 py-2.5 rounded-button bg-brand hover:bg-brand-light
                text-white shadow-glow transition-colors"
            >
              {t(e.ctaKey) || "Probar"}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
