import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { useI18n } from "../i18n";
import { useServiceStatusSummary } from "../hooks/useServiceStatusSummary";
import {
  bannerDismissKey,
  componentLabelKey,
  incidentStatusLabelKey,
} from "../lib/serviceStatus";

// Barra horizontal de incidente. Vive arriba del contenido, debajo del
// topbar (mismo lugar que PastDueBanner).
//
// Dos fuentes, en este orden de prioridad:
//   1. Un incidente REDACTADO por un operador → se muestra su título. Un
//      humano explica mejor qué pasa que cualquier heurística.
//   2. Nada redactado pero la sonda ve una caída (`auto_affected`) → copy
//      genérico con los servicios afectados. Es el caso de las 4 AM.
//
// SIN AUTH a propósito: `/service-status/summary` es público, así que la
// barra también aparece en la landing y —clave— le sigue apareciendo a
// quien tiene el token vencido por culpa del mismo outage.
//
// Se descarta por NOVEDAD, no por incidente (ver `bannerDismissKey`): si
// se cerrara por id, quien la cierra temprano no vería nunca el aviso de
// que el incidente empeoró.
export default function ServiceStatusBanner({ variant = "app" }) {
  const { t } = useI18n();
  // El poll vive en el hook y se comparte con el punto de estado del
  // sidebar: un solo request por pestaña, no uno por componente.
  const summary = useServiceStatusSummary();
  const [dismissedKey, setDismissedKey] = useState(null);

  const key = bannerDismissKey(summary);

  useEffect(() => {
    if (!key) return;
    try {
      if (localStorage.getItem(key) === "1") setDismissedKey(key);
    } catch { /* storage bloqueado (Safari privado) → la barra se muestra */ }
  }, [key]);

  if (!summary || !summary.banner || !key || dismissedKey === key) return null;

  const critical = summary.severity === "critical";
  const incident = summary.incident;
  const affected = summary.auto_affected || [];

  const close = () => {
    try { localStorage.setItem(key, "1"); } catch { /* storage bloqueado */ }
    setDismissedKey(key);
  };

  const title = incident
    ? incident.title
    : t("service_status.banner_auto_title") || "Estamos con problemas en el servicio";

  const detail = incident
    ? `${t(incidentStatusLabelKey(incident.status)) || incident.status}${
        (incident.components || []).length
          ? ` · ${incident.components.map((c) => t(componentLabelKey(c)) || c).join(", ")}`
          : ""
      }`
    : affected.map((c) => t(componentLabelKey(c)) || c).join(", ");

  const tone = critical
    ? "bg-red-500/[0.12] ring-red-500/30 text-red-100"
    : "bg-amber-500/10 ring-amber-500/30 text-amber-100";
  const iconTone = critical ? "text-red-400" : "text-amber-400";
  const linkTone = critical
    ? "text-red-200 hover:text-white"
    : "text-amber-200 hover:text-white";

  // En la landing la barra va pegada arriba de todo y a full width; dentro
  // de la app respeta el padding del contenido, como PastDueBanner.
  const wrapper = variant === "landing"
    ? "px-4 md:px-8 pt-3"
    : "relative z-10 px-4 md:px-8 pt-4";

  return (
    <div className={wrapper}>
      <div
        role="status"
        aria-live="polite"
        className={`flex flex-col sm:flex-row sm:items-center gap-3 px-4 py-3 rounded-card ring-1 ${tone}`}
      >
        <svg className={`w-5 h-5 shrink-0 ${iconTone}`} fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24" aria-hidden="true">
          <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m0 3.75h.008M10.34 3.94l-7.5 12.99A1.5 1.5 0 004.14 19.5h15.72a1.5 1.5 0 001.3-2.57l-7.5-12.99a1.5 1.5 0 00-2.6 0z" />
        </svg>
        <div className="flex-1 min-w-0 text-ui">
          <span className="font-semibold">{title}</span>
          {detail && <span className="opacity-80">{" · "}{detail}</span>}
        </div>
        <div className="flex items-center gap-3 shrink-0">
          {/* En la landing va un <a> y no un <Link>: Landing es
              router-agnóstica por diseño (recibe onLogin/onStart en vez de
              navegar) y se renderiza suelta en sus tests. Una navegación
              completa a /status desde marketing no cuesta nada; dentro de
              la app sí importa no perder el estado del workspace. */}
          {variant === "landing" ? (
            <a
              href={incident ? `/status?incident=${incident.id}` : "/status"}
              className={`text-ui font-semibold underline underline-offset-2 transition-colors duration-brand ${linkTone}`}
            >
              {t("service_status.banner_cta") || "Ver estado del servicio"}
            </a>
          ) : (
            <Link
              to={incident ? `/status?incident=${incident.id}` : "/status"}
              className={`text-ui font-semibold underline underline-offset-2 transition-colors duration-brand ${linkTone}`}
            >
              {t("service_status.banner_cta") || "Ver estado del servicio"}
            </Link>
          )}
          <button
            type="button"
            onClick={close}
            aria-label={t("common.close") || "Cerrar"}
            className="w-7 h-7 rounded-lg flex items-center justify-center opacity-60 hover:opacity-100 transition-opacity"
          >
            ✕
          </button>
        </div>
      </div>
    </div>
  );
}
