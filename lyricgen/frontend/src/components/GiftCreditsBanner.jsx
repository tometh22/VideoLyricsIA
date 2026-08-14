import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useI18n } from "../i18n";

const API = import.meta.env.VITE_API_URL || "";

// Aviso one-time del regalo de créditos. Lee /usage (datos reales del usuario:
// saldo + vencimiento) y muestra un banner celebratorio cuando hay créditos de
// regalo sin "ver". Se descarta por grant (key por vencimiento), así una promo
// nueva vuelve a avisar. Integra con el medidor de la sidebar (misma fuente).
//
// Vive dentro de <main> (al lado del <Outlet/>), así no remonta por navegación.
export default function GiftCreditsBanner({ user }) {
  const { t } = useI18n();
  const navigate = useNavigate();
  const [usage, setUsage] = useState(null);
  const [dismissed, setDismissed] = useState(false);

  useEffect(() => {
    if (!user) return undefined;
    const tok = localStorage.getItem("genly_token");
    if (!tok) return undefined;
    let alive = true;
    fetch(`${API}/usage`, { headers: { Authorization: `Bearer ${tok}` } })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => { if (alive && d) setUsage(d); })
      .catch(() => { /* silencioso: el banner ausente es un fallback válido */ });
    return () => { alive = false; };
  }, [user]);

  if (!user || !usage) return null;
  const remaining = usage.bonus_remaining || 0;
  if (remaining <= 0 || dismissed) return null;

  // Una vez por grant: la key cambia con el vencimiento → una promo nueva
  // (otro vto) vuelve a avisar.
  const key = `genly_gift_seen:${usage.bonus_expires_at || "x"}`;
  let seen = false;
  try { seen = localStorage.getItem(key) === "1"; } catch { /* storage bloqueado */ }
  if (seen) return null;

  const cost = usage.scenes_credit_cost || 3;
  const escenas = Math.floor(remaining / (cost || 1));
  let days = null;
  if (usage.bonus_expires_at) {
    const ms = new Date(usage.bonus_expires_at).getTime() - Date.now();
    days = Number.isFinite(ms) ? Math.max(0, Math.ceil(ms / 86_400_000)) : null;
  }

  const close = () => {
    try { localStorage.setItem(key, "1"); } catch { /* storage bloqueado */ }
    setDismissed(true);
  };

  const expText = days == null
    ? ""
    : days === 0
      ? (t("gift.banner_today") || "Vencen hoy.")
      : (t("gift.banner_expires") || "Vencen en {d} días.").replace("{d}", days);

  return (
    <div className="mb-6 rounded-card bg-gradient-to-r from-emerald-500/15 to-brand/10 ring-1 ring-emerald-400/30 px-5 py-4 flex items-start gap-3 animate-fade-in">
      <span className="text-2xl leading-none mt-0.5" aria-hidden="true">🎁</span>
      <div className="flex-1 min-w-0">
        <p className="font-semibold text-white">
          {(t("gift.banner_title") || "¡Te regalamos {n} créditos!").replace("{n}", remaining)}
        </p>
        <p className="text-ui text-emerald-100/80 mt-0.5">
          {(t("gift.banner_body") || "Te alcanzan para {e} videos con Escenas.").replace("{e}", escenas)}{" "}
          {expText}
        </p>
      </div>
      <button
        type="button"
        onClick={() => { close(); navigate("/new"); }}
        className="shrink-0 px-3 py-1.5 rounded-lg bg-emerald-500 text-white text-ui font-medium hover:bg-emerald-400 transition-colors"
      >
        {t("gift.banner_cta") || "Probar Escenas"}
      </button>
      <button
        type="button"
        onClick={close}
        aria-label={t("gift.banner_dismiss") || "Cerrar"}
        className="shrink-0 text-emerald-200/60 hover:text-white px-1"
      >
        ✕
      </button>
    </div>
  );
}
