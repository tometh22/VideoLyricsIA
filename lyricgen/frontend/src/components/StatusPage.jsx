import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { useI18n } from "../i18n";
import { fetchWithTimeout } from "../fetchWithTimeout";
import BrandLockup from "./BrandLockup";
import {
  componentLabelKey,
  formatUptime,
  impactLabelKey,
  incidentStatusLabelKey,
  statusLabelKey,
  statusStyle,
} from "../lib/serviceStatus";

const API = import.meta.env.VITE_API_URL || "";
const POLL_MS = 60_000;

// Página pública de estado del servicio (`/status`). Sin login: el cliente
// que no puede entrar por el outage tiene que poder verla.
//
// Responde una sola pregunta —"¿es mío o es de ellos?"— y de eso sale toda
// la jerarquía: primero el veredicto en una línea, después los incidentes
// abiertos con su relato, después el detalle por servicio, y al final el
// historial. Un cliente enojado no scrollea.
//
// EL CASO QUE JUSTIFICA LA PÁGINA: si la API no contesta, no se muestra un
// spinner infinito ni un error genérico — se muestra un estado rojo
// explícito. Esta página vive en Vercel y la API en Railway, así que una
// caída de Railway la deja EN PIE dando exactamente la respuesta que el
// visitante vino a buscar.

function StatusDot({ status, className = "" }) {
  return (
    <span
      className={`inline-block w-2.5 h-2.5 rounded-full shrink-0 ${statusStyle(status).dot} ${className}`}
      aria-hidden="true"
    />
  );
}

// Barras del historial. Una por día, la más vieja a la izquierda.
function UptimeBars({ days }) {
  const { t } = useI18n();
  if (!days || !days.length) return null;
  return (
    <div className="flex items-stretch gap-[2px] h-8" role="img"
         aria-label={t("service_status.uptime_bars_label") || "Historial de disponibilidad por día"}>
      {days.map((d) => {
        const style = statusStyle(d.status);
        const label = d.status === "no_data"
          ? (t("service_status.state.no_data") || "Sin datos")
          : (t(statusLabelKey(d.status)) || d.status);
        return (
          <span
            key={d.day}
            title={`${d.day} — ${label}${d.low_coverage ? ` (${t("service_status.low_coverage_short") || "cobertura parcial"})` : ""}`}
            className={`flex-1 min-w-[2px] rounded-sm ${style.bar} ${d.low_coverage ? "opacity-50" : ""}`}
          />
        );
      })}
    </div>
  );
}

function ComponentRow({ component }) {
  const { t } = useI18n();
  const style = statusStyle(component.status);
  const uptime = formatUptime(component.uptime_pct, component.coverage_pct);
  const label = t(componentLabelKey(component.id)) || component.label || component.id;

  return (
    <div className="px-5 py-4 border-t border-white/[0.06] first:border-t-0">
      <div className="flex items-center justify-between gap-3 mb-2 flex-wrap">
        <div className="flex items-center gap-2.5 min-w-0">
          <StatusDot status={component.status} />
          <span className="font-semibold text-ink-primary truncate">{label}</span>
        </div>
        <span className={`text-ui font-semibold ${style.text}`}>
          {t(statusLabelKey(component.status)) || component.status}
        </span>
      </div>
      <UptimeBars days={component.days} />
      <div className="flex items-center justify-between gap-3 mt-2 text-caption text-ink-secondary/70">
        <span>{t("service_status.days_ago", { n: (component.days || []).length }) || `${(component.days || []).length} días`}</span>
        {uptime ? (
          <span className={uptime.lowCoverage ? "text-amber-300/80" : ""}>
            {t("service_status.uptime_value", { pct: uptime.value }) || `${uptime.value} de disponibilidad`}
            {/* La cobertura se publica junto al porcentaje cuando es baja.
                Un 100% sobre 3% de observaciones no es un 100%, y ocultar
                el denominador es la forma más fácil de que esta página
                mienta sin decir una sola cosa falsa. */}
            {uptime.lowCoverage && (
              <span> · {t("service_status.low_coverage", { pct: uptime.coverage })
                || `medido sobre ${uptime.coverage} del período`}</span>
            )}
          </span>
        ) : (
          <span>{t("service_status.no_uptime_data") || "Todavía sin datos suficientes"}</span>
        )}
      </div>
    </div>
  );
}

function IncidentCard({ incident, highlighted, defaultOpen }) {
  const { t, lang } = useI18n();
  const [open, setOpen] = useState(Boolean(defaultOpen));
  const impactStatus = {
    none: "maintenance", minor: "degraded",
    major: "partial_outage", critical: "major_outage",
  }[incident.impact] || "degraded";
  const style = statusStyle(impactStatus);
  // Al resolverse, el anillo y el label de impacto pierden el color: el
  // dato sigue publicado (impacto e historial) sin gritar.
  const shellStyle = incident.resolved ? statusStyle("unknown") : style;
  const updates = incident.updates || [];
  const visible = open ? updates : updates.slice(0, 1);

  const fmt = (iso) => {
    if (!iso) return "";
    try {
      return new Date(iso).toLocaleString(lang === "es" ? "es-AR" : lang, {
        day: "2-digit", month: "short", year: "numeric",
        hour: "2-digit", minute: "2-digit",
      });
    } catch {
      return iso;
    }
  };

  return (
    <article
      id={`incident-${incident.id}`}
      className={`rounded-card overflow-hidden ring-1 ${shellStyle.ring} ${
        highlighted ? "ring-2" : ""
      } bg-surface-1`}
    >
      {/* Un incidente RESUELTO va con header neutro. Con el mismo fondo
          fuerte que uno activo, una página con diez incidentes cerrados se
          lee como diez incendios simultáneos y el que está en curso deja de
          destacarse — que es justo lo único que el visitante necesita ver. */}
      <header className={`px-5 py-4 ${
        incident.resolved ? "bg-white/[0.03]"
          : impactStatus === "major_outage" ? "bg-red-500/[0.12]"
          : impactStatus === "partial_outage" ? "bg-orange-500/[0.12]"
          : impactStatus === "degraded" ? "bg-amber-500/[0.10]"
          : "bg-brand/[0.10]"
      }`}>
        <div className="flex items-start justify-between gap-3 flex-wrap">
          <h3 className="font-semibold text-ink-primary text-base min-w-0">{incident.title}</h3>
          <span className={`text-caption font-bold uppercase tracking-wider ${shellStyle.text}`}>
            {t(impactLabelKey(incident.impact)) || incident.impact}
          </span>
        </div>
        <p className="text-caption text-ink-secondary mt-1">
          <span className={incident.resolved ? "text-accent" : style.text}>
            {t(incidentStatusLabelKey(incident.status)) || incident.status}
          </span>
          {(incident.components || []).length > 0 && (
            <> · {incident.components.map((c) => t(componentLabelKey(c)) || c).join(", ")}</>
          )}
          {" · "}
          {t("service_status.started_at", { when: fmt(incident.started_at) })
            || `desde ${fmt(incident.started_at)}`}
          {incident.resolved_at && <> · {t("service_status.resolved_at", { when: fmt(incident.resolved_at) })
            || `resuelto ${fmt(incident.resolved_at)}`}</>}
        </p>
      </header>

      <div className="px-5 py-4">
        <ol className="space-y-4">
          {visible.map((u) => (
            <li key={u.id} className="flex gap-3">
              <StatusDot status={u.status === "resolved" ? "operational" : impactStatus}
                         className="mt-1.5" />
              <div className="min-w-0">
                <p className="text-ui text-ink-primary">
                  <span className="font-semibold">
                    {t(incidentStatusLabelKey(u.status)) || u.status}
                  </span>
                  {" — "}
                  {u.body}
                </p>
                <p className="text-caption text-ink-secondary/70 mt-0.5">{fmt(u.created_at)}</p>
              </div>
            </li>
          ))}
        </ol>
        {updates.length > 1 && (
          <button
            type="button"
            onClick={() => setOpen(!open)}
            className="mt-3 text-caption font-semibold text-brand-light hover:text-white transition-colors duration-brand"
          >
            {open
              ? (t("service_status.hide_updates") || "Ocultar actualizaciones")
              : (t("service_status.view_all_updates", { n: updates.length })
                 || `Ver las ${updates.length} actualizaciones`)}
          </button>
        )}
      </div>
    </article>
  );
}

export default function StatusPage() {
  const { t } = useI18n();
  const [params] = useSearchParams();
  const focusedIncident = Number(params.get("incident")) || null;

  const [data, setData] = useState(null);
  // "loading" | "ok" | "unreachable". `unreachable` NO es un error de la
  // página: es el resultado más informativo que puede dar.
  const [phase, setPhase] = useState("loading");

  const load = useCallback(async () => {
    try {
      const res = await fetchWithTimeout(`${API}/service-status`, {}, 10_000);
      if (!res.ok) {
        setPhase("unreachable");
        return;
      }
      setData(await res.json());
      setPhase("ok");
    } catch {
      setPhase("unreachable");
    }
  }, []);

  useEffect(() => {
    load();
    const timer = setInterval(load, POLL_MS);
    return () => clearInterval(timer);
  }, [load]);

  useEffect(() => {
    document.title = t("service_status.meta_title") || "Estado del servicio — GenLy";
  }, [t]);

  const indicator = phase === "unreachable" ? "major_outage" : (data?.indicator || "unknown");
  const headline = useMemo(() => {
    if (phase === "loading") return t("service_status.loading") || "Consultando el estado…";
    if (phase === "unreachable") {
      return t("service_status.unreachable_headline")
        || "No podemos contactar la API de GenLy";
    }
    return t(`service_status.headline.${indicator}`)
      || (indicator === "operational"
        ? "Todos los sistemas operativos"
        : "Hay un problema en curso");
  }, [phase, indicator, t]);

  const style = statusStyle(phase === "loading" ? "unknown" : indicator);

  return (
    <div className="min-h-screen bg-surface text-ink-primary">
      <div className="mx-auto w-full max-w-3xl px-4 md:px-6 py-10 md:py-14">
        <header className="flex items-center justify-between gap-4 mb-8 flex-wrap">
          <Link to="/" className="flex items-center gap-3" aria-label="GenLy">
            <BrandLockup />
          </Link>
          <Link
            to="/"
            className="text-ui text-ink-secondary hover:text-white transition-colors duration-brand"
          >
            {t("service_status.back_to_app") || "Ir a la app"}
          </Link>
        </header>

        <h1 className="text-2xl md:text-3xl font-bold mb-1">
          {t("service_status.title") || "Estado del servicio"}
        </h1>
        <p className="text-ui text-ink-secondary mb-8">
          {t("service_status.subtitle")
            || "Estado en vivo de la plataforma y de los incidentes en curso."}
        </p>

        {/* Veredicto en una línea: es lo único que el 90% de las visitas
            necesita leer. */}
        <div className={`rounded-card ring-1 ${style.ring} bg-surface-1 px-5 py-5 flex items-center gap-3 mb-8`}>
          <StatusDot status={phase === "loading" ? "unknown" : indicator} className="w-3 h-3" />
          <div className="min-w-0 flex-1">
            <p className={`font-semibold ${style.text}`}>{headline}</p>
            {phase === "unreachable" && (
              <p className="text-caption text-ink-secondary mt-1">
                {/* Esta página se sirve desde Vercel y la API desde
                    Railway. Que la página cargue y la API no contesta ES
                    el diagnóstico, no una falla de la página. */}
                {t("service_status.unreachable_body")
                  || "Esta página cargó, pero nuestra API no responde. Es un problema nuestro, no de tu conexión. Ya estamos avisados."}
              </p>
            )}
            {phase === "ok" && data?.updated_at && (
              <p className="text-caption text-ink-secondary/70 mt-1">
                {t("service_status.updated_at", {
                  when: new Date(data.updated_at).toLocaleTimeString("es-AR", {
                    hour: "2-digit", minute: "2-digit",
                  }),
                }) || "Actualizado recién"}
                {" · "}
                {t("service_status.auto_refresh") || "se actualiza cada minuto"}
              </p>
            )}
          </div>
        </div>

        {phase === "ok" && (data.active_incidents || []).length > 0 && (
          <section className="mb-8">
            <h2 className="text-section uppercase font-bold tracking-wider text-ink-secondary mb-3">
              {t("service_status.active_incidents") || "Incidentes en curso"}
            </h2>
            <div className="space-y-4">
              {data.active_incidents.map((inc) => (
                <IncidentCard
                  key={inc.id}
                  incident={inc}
                  highlighted={focusedIncident === inc.id}
                  defaultOpen
                />
              ))}
            </div>
          </section>
        )}

        {phase === "ok" && (
          <section className="mb-8">
            <h2 className="text-section uppercase font-bold tracking-wider text-ink-secondary mb-3">
              {t("service_status.components_title") || "Servicios"}
            </h2>
            <div className="rounded-card ring-1 ring-white/[0.08] bg-surface-1 overflow-hidden">
              {(data.components || []).map((c) => (
                <ComponentRow key={c.id} component={c} />
              ))}
            </div>
            <p className="text-caption text-ink-secondary/60 mt-2">
              {t("service_status.history_note", { n: data.history_days })
                || `Historial de los últimos ${data.history_days} días.`}
            </p>
          </section>
        )}

        {phase === "ok" && (
          <section>
            <h2 className="text-section uppercase font-bold tracking-wider text-ink-secondary mb-3">
              {t("service_status.past_incidents") || "Incidentes resueltos"}
            </h2>
            {(data.past_incidents || []).length === 0 ? (
              <p className="text-ui text-ink-secondary rounded-card ring-1 ring-white/[0.08] bg-surface-1 px-5 py-4">
                {t("service_status.no_past_incidents")
                  || "Sin incidentes registrados en este período."}
              </p>
            ) : (
              <div className="space-y-4">
                {data.past_incidents.map((inc) => (
                  <IncidentCard
                    key={inc.id}
                    incident={inc}
                    highlighted={focusedIncident === inc.id}
                    defaultOpen={focusedIncident === inc.id}
                  />
                ))}
              </div>
            )}
          </section>
        )}

        <footer className="mt-10 pt-6 border-t border-white/[0.06] text-caption text-ink-secondary/70">
          {t("service_status.support_note") || "¿Algo no aparece acá y te está afectando? Escribinos a"}{" "}
          <a href="mailto:soporte@genly.pro" className="text-brand-light hover:text-white">
            soporte@genly.pro
          </a>
        </footer>
      </div>
    </div>
  );
}
