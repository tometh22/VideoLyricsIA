import { useState, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { useI18n } from "../../i18n";
import { useChangelog } from "./useChangelog";
import ReleaseVisual from "./ReleaseVisual";

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

  return (
    <div
      className="fixed inset-0 z-[60] grid place-items-center bg-black/70 p-4 backdrop-blur-sm animate-fade-in"
      onClick={close}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label={t(e.titleKey) || "Novedad"}
        className="w-full max-w-md overflow-hidden rounded-card bg-surface-2 shadow-glow-lg ring-1 ring-brand/20 animate-slide-up"
        onClick={(ev) => ev.stopPropagation()}
      >
        <div className="relative p-4 pb-0">
          <ReleaseVisual entry={e} />
          <span className="absolute left-7 top-7 rounded-full bg-accent px-2 py-0.5 text-[10px] font-bold tracking-[0.05em] text-white shadow-depth">
            {t("announce.scenes_badge") || "NUEVO"}
          </span>
          <button
            onClick={close}
            aria-label={t("common.cancel") || "Cerrar"}
            className="absolute right-7 top-7 grid h-7 w-7 place-items-center rounded-full bg-black/40 text-[15px] leading-none text-white/80 outline-none transition-colors hover:bg-black/60 hover:text-white focus-visible:ring-2 focus-visible:ring-brand/60"
          >
            ×
          </button>
        </div>
        <div className="p-6 text-center">
          <h3 className="text-[21px] font-extrabold leading-tight text-white">{t(e.titleKey)}</h3>
          {e.taglineKey && (
            <p className="mt-2 text-[13.5px] font-medium leading-snug text-ink-secondary">{t(e.taglineKey)}</p>
          )}
          {e.ctaTo && (
            <button
              onClick={() => { close(); navigate(e.ctaTo); }}
              className="mt-5 w-full rounded-button bg-brand px-4 py-2.5 text-[13px] font-semibold text-white shadow-glow transition-colors hover:bg-brand-light"
            >
              {t(e.ctaKey) || "Probar"}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
