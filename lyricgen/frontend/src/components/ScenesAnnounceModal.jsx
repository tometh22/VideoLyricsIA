import { useState, useEffect } from "react";
import { useI18n } from "../i18n";

// One-time announcement del add-on "Escenas". Se muestra una vez por usuario
// (flag en localStorage); muestra un loop real (public/escenas_demo.mp4: un
// fondo único que da paso a varias escenas) + el costo en créditos. El
// descubrimiento en contexto sigue con el badge/beacon de la card del wizard.
const SEEN_KEY = "genly_scenes_announce_seen";

export default function ScenesAnnounceModal({ user }) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (!user) return;
    let seen = true; // si localStorage falla, default a "ya visto" (no molestar)
    try { seen = localStorage.getItem(SEEN_KEY) === "1"; } catch { seen = true; }
    if (!seen) setOpen(true);
  }, [user]);

  const close = () => {
    setOpen(false);
    try { localStorage.setItem(SEEN_KEY, "1"); } catch { /* storage bloqueado */ }
  };

  useEffect(() => {
    if (!open) return undefined;
    const onKey = (e) => { if (e.key === "Escape") close(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  if (!open) return null;
  const cost = user?.features?.scenes_credit_cost ?? 3;

  return (
    <div
      className="fixed inset-0 z-[60] grid place-items-center bg-black/70 p-4 animate-fade-in"
      onClick={close}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label={t("announce.scenes_title") || "Nuevo: Escenas"}
        className="w-full max-w-md rounded-card bg-surface-2 ring-1 ring-brand/25 overflow-hidden shadow-glow"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="relative">
          <video
            src="/escenas_demo.mp4"
            autoPlay
            muted
            loop
            playsInline
            className="w-full aspect-video object-cover bg-black"
          />
          <span className="absolute top-2 left-2 text-[10px] font-bold tracking-[0.05em] px-2 py-0.5 rounded bg-accent text-white">
            {t("announce.scenes_badge") || "NUEVO"}
          </span>
        </div>
        <div className="p-5 text-center">
          <h3 className="text-[16px] font-bold text-white flex items-center justify-center gap-2">
            🎬 {t("announce.scenes_title") || "Escenas: tu video con arco narrativo"}
          </h3>
          <p className="text-[12.5px] text-ink-secondary mt-2 leading-relaxed">
            {t("announce.scenes_body") ||
              "En vez de un fondo único, tu video se arma con varias escenas que cambian con la canción y vuelven en el coro. Lo activás en el paso de Modo."}
          </p>
          <p className="text-[11px] text-brand-light font-medium mt-2">
            {(t("announce.scenes_cost") || "Cuesta {n} créditos por video").replace("{n}", cost)}
          </p>
          <div className="mt-4 flex gap-2 justify-center">
            <button
              onClick={close}
              className="text-[12px] font-medium px-3.5 py-1.5 rounded-lg bg-white/[0.04] hover:bg-white/[0.08] text-gray-300"
            >
              {t("announce.later") || "Más tarde"}
            </button>
            <button
              onClick={close}
              className="text-[12px] font-semibold px-3.5 py-1.5 rounded-lg bg-brand hover:bg-brand-light text-white"
            >
              {t("announce.scenes_cta") || "Entendido, lo pruebo"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
