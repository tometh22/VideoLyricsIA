import { useState } from "react";
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

  return (
    <>
      <button
        onClick={openPanel}
        aria-label={t("whatsnew.title") || "Novedades"}
        title={t("whatsnew.title") || "Novedades"}
        className="relative w-9 h-9 rounded-xl flex items-center justify-center text-gray-400 hover:text-white hover:bg-white/[0.04] transition-colors outline-none focus-visible:ring-2 focus-visible:ring-brand/60"
      >
        <svg className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth="1.8" viewBox="0 0 24 24" aria-hidden="true">
          <path d="M3 11l15-5v13L3 14z" strokeLinecap="round" strokeLinejoin="round" />
          <path d="M8 15v2.5A1.5 1.5 0 009.5 19h0A1.5 1.5 0 0011 17.5V16" strokeLinecap="round" strokeLinejoin="round" />
          <path d="M18 8a3 3 0 010 6" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
        {unreadCount > 0 && (
          <span className="absolute -top-0.5 -right-0.5 min-w-[16px] h-4 px-1 rounded-full bg-accent text-white text-[10px] font-bold grid place-items-center">
            {unreadCount}
          </span>
        )}
      </button>
      {open && <WhatsNewPanel onClose={() => setOpen(false)} />}
    </>
  );
}
