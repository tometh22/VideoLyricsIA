import { useState } from "react";
import { createPortal } from "react-dom";
import { useI18n } from "../../i18n";
import { useChangelog } from "./useChangelog";
import WhatsNewPanel from "./WhatsNewPanel";

// Campana de "Novedades" en el header: badge con cantidad de no-leídos; al
// abrir el panel, marca todo como visto (el badge desaparece). Self-contained:
// App.jsx solo renderiza <WhatsNewBell /> en el topbar.
export default function WhatsNewBell() {
  const { t } = useI18n();
  const { unreadCount, markAllSeen } = useChangelog();
  const [open, setOpen] = useState(false);

  const openPanel = () => { markAllSeen(); setOpen(true); };
  const hasUnread = unreadCount > 0;

  return (
    <>
      <button
        onClick={openPanel}
        aria-label={t("whatsnew.title") || "Novedades"}
        title={t("whatsnew.title") || "Novedades"}
        data-tour="whatsnew-bell"
        className={`relative h-9 rounded-xl flex items-center justify-center transition-colors outline-none focus-visible:ring-2 focus-visible:ring-brand/60 ${
          hasUnread
            ? "w-auto gap-2 bg-accent/[0.08] px-2.5 text-accent ring-1 ring-accent/20 hover:bg-accent/[0.12] hover:text-accent-light"
            : "w-9 text-gray-400 hover:text-white hover:bg-white/[0.04]"
        }`}
      >
        <svg className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth="1.8" viewBox="0 0 24 24" aria-hidden="true">
          <path d="M3 11l15-5v13L3 14z" strokeLinecap="round" strokeLinejoin="round" />
          <path d="M8 15v2.5A1.5 1.5 0 009.5 19h0A1.5 1.5 0 0011 17.5V16" strokeLinecap="round" strokeLinejoin="round" />
          <path d="M18 8a3 3 0 010 6" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
        {hasUnread && (
          <>
            <span className="hidden text-[11px] font-semibold lg:inline">
              {t("whatsnew.new_badge") || "Nuevo"}
            </span>
            <span className="grid h-4 min-w-[16px] place-items-center rounded-full bg-accent px-1 text-[10px] font-bold text-black">
            {unreadCount}
            </span>
          </>
        )}
      </button>
      {/* Portal a document.body: el header tiene backdrop-blur, que crea un
          containing block para position:fixed — sin el portal, el panel queda
          relativo al header (corto) y sale cortado. */}
      {open && createPortal(<WhatsNewPanel onClose={() => setOpen(false)} />, document.body)}
    </>
  );
}
