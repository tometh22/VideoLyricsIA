import { useRef, useState, useEffect } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useI18n } from "../../i18n";
import { useChangelog } from "./useChangelog";
import ReleaseVisual from "./ReleaseVisual";
import useDialogA11y from "../../hooks/useDialogA11y";

export default function WhatsNewModal({ user }) {
  const { t } = useI18n();
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const { modalEntry, dismissModal } = useChangelog();
  const [open, setOpen] = useState(false);
  const closeButtonRef = useRef(null);
  const isReviewDelivery = pathname === "/admin/cola" || pathname.startsWith("/review/");

  useEffect(() => {
    if (isReviewDelivery) {
      setOpen(false);
    } else if (user && modalEntry) {
      setOpen(true);
    }
  }, [user, modalEntry, isReviewDelivery]);

  const close = () => { setOpen(false); dismissModal(); };

  const dialogRef = useDialogA11y({ open, onClose: close, initialFocusRef: closeButtonRef });

  if (isReviewDelivery || !open || !modalEntry) return null;
  const e = modalEntry;
  const modalFeatures = Array.isArray(e.modalFeatures) ? e.modalFeatures : [];

  return (
    <div
      className="fixed inset-0 z-[60] grid place-items-center overflow-y-auto bg-black/80 p-3 backdrop-blur-md animate-fade-in sm:p-5"
      onClick={close}
    >
      <div
        ref={dialogRef}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-label={t(e.titleKey) || "Novedad"}
        className="my-auto max-h-[calc(100vh-1.5rem)] w-full max-w-2xl overflow-y-auto rounded-2xl bg-[#0b0c12] shadow-[0_30px_90px_rgba(0,0,0,.65)] ring-1 ring-white/[0.10] animate-slide-up sm:max-h-[calc(100vh-2.5rem)]"
        onClick={(ev) => ev.stopPropagation()}
      >
        <div className="relative p-3 pb-0 sm:p-4 sm:pb-0">
          <ReleaseVisual entry={e} />
          <span className="absolute left-6 top-6 rounded-full bg-accent px-2.5 py-1 text-[9px] font-extrabold uppercase tracking-[0.14em] text-black shadow-[0_8px_24px_rgba(20,200,168,.28)] sm:left-8 sm:top-8">
            {t("announce.scenes_badge") || "NUEVO"}
          </span>
          <button
            ref={closeButtonRef}
            onClick={close}
            aria-label={t("common.cancel") || "Cerrar"}
            className="absolute right-6 top-6 grid h-8 w-8 place-items-center rounded-full bg-black/45 text-[17px] leading-none text-white/75 outline-none ring-1 ring-white/10 transition-all hover:scale-105 hover:bg-black/70 hover:text-white focus-visible:ring-2 focus-visible:ring-brand/60 sm:right-8 sm:top-8"
          >
            ×
          </button>
        </div>
        <div className="px-5 pb-5 pt-5 sm:px-7 sm:pb-7 sm:pt-6">
          <h3 className="max-w-xl text-[25px] font-extrabold leading-[1.05] tracking-[-0.025em] text-white sm:text-[30px]">
            {t(e.titleKey)}
          </h3>
          {e.taglineKey && (
            <p className="mt-2.5 max-w-xl text-[14px] font-medium leading-relaxed text-ink-secondary sm:text-[15px]">
              {t(e.taglineKey)}
            </p>
          )}
          {modalFeatures.length > 0 && (
            <div className="mt-5 grid gap-2.5 sm:grid-cols-3">
              {modalFeatures.map((feature, index) => (
                <div
                  key={feature.titleKey}
                  className="group rounded-xl bg-white/[0.035] p-3.5 ring-1 ring-white/[0.065] transition-colors hover:bg-white/[0.055] hover:ring-white/[0.10]"
                >
                  <div className="mb-2.5 flex items-center gap-2">
                    <span className="grid h-6 w-6 shrink-0 place-items-center rounded-full bg-brand/15 text-[10px] font-extrabold text-brand-light ring-1 ring-brand/25">
                      {index + 1}
                    </span>
                    <p className="text-[12px] font-bold leading-tight text-white">{t(feature.titleKey)}</p>
                  </div>
                  <p className="text-[11.5px] leading-relaxed text-gray-400">{t(feature.bodyKey)}</p>
                </div>
              ))}
            </div>
          )}
          {e.ctaTo && (
            <button
              onClick={() => { close(); navigate(e.ctaTo); }}
              className="mt-5 inline-flex h-11 w-full items-center justify-center gap-2 rounded-xl bg-gradient-to-r from-brand to-[#8167ff] px-4 text-[13px] font-bold text-white shadow-[0_12px_32px_rgba(109,74,255,0.30)] transition-all hover:-translate-y-0.5 hover:shadow-[0_16px_38px_rgba(109,74,255,0.38)]"
            >
              {t(e.ctaKey) || "Probar"}
              <svg className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="2.3" viewBox="0 0 24 24" aria-hidden="true">
                <path d="M5 12h14M13 6l6 6-6 6" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
