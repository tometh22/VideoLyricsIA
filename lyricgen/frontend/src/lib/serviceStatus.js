// Vocabulario compartido de la página de status y del banner de la home.
//
// Existe para que el banner, la página pública y la sección de admin no
// puedan discrepar sobre de qué color es un estado. Es el mismo motivo por
// el que `JOB_STATUS` vive en un solo lugar dentro del admin: si un estado
// se pinta distinto en dos pantallas, es un bug, no una decisión.
//
// El backend (`status_page.py`) manda los ids; acá viven solamente la
// presentación y las claves de i18n.

// Peor a mejor. Espeja COMPONENT_STATUS_RANK del backend: si se agrega un
// estado allá hay que agregarlo acá o el frontend lo dibuja como neutro.
export const STATUS_RANK = {
  operational: 0,
  maintenance: 1,
  degraded: 2,
  partial_outage: 3,
  major_outage: 4,
};

export const COMPONENT_IDS = [
  "api",
  "transcription",
  "render",
  "backgrounds",
  "storage",
];

export const INCIDENT_STATUSES = ["investigating", "identified", "monitoring", "resolved"];
export const INCIDENT_IMPACTS = ["none", "minor", "major", "critical"];

// Un solo lugar decide el color. `dot` para el punto de estado, `bar` para
// las barras del historial de 90 días, `text` para el label.
//
// `bar` de "operativo" NO es el teal del accent aunque el dot y el label sí
// lo sean: son 90 barras × 5 servicios = 450 elementos teal en una sola
// pantalla, y la guía de marca reserva el teal para estados activos,
// progreso de render y el micro-acento del logo (regla de ≤10% de la
// superficie). El emerald además es lo que se lee universalmente como
// "barra de uptime" y deja la escalada amber → orange → red coherente.
export const STATUS_STYLES = {
  operational:    { dot: "bg-accent",      bar: "bg-emerald-500/55", text: "text-accent",      ring: "ring-accent/20" },
  maintenance:    { dot: "bg-brand-light", bar: "bg-brand-light/70", text: "text-brand-light", ring: "ring-brand/25" },
  degraded:       { dot: "bg-amber-400",   bar: "bg-amber-400/80",  text: "text-amber-400",   ring: "ring-amber-500/25" },
  partial_outage: { dot: "bg-orange-400",  bar: "bg-orange-400/85", text: "text-orange-400",  ring: "ring-orange-500/30" },
  major_outage:   { dot: "bg-red-400",     bar: "bg-red-400/90",    text: "text-red-400",     ring: "ring-red-500/30" },
  // Ausencia de dato, NO un estado intermedio: gris, nunca verde.
  unknown:        { dot: "bg-gray-600",    bar: "bg-white/10",      text: "text-gray-500",    ring: "ring-white/10" },
  no_data:        { dot: "bg-gray-600",    bar: "bg-white/[0.07]",  text: "text-gray-500",    ring: "ring-white/10" },
};

export function statusStyle(status) {
  return STATUS_STYLES[status] || STATUS_STYLES.unknown;
}

// Clave de i18n para el label de un estado de componente.
export function statusLabelKey(status) {
  return `service_status.state.${status || "unknown"}`;
}

export function componentLabelKey(id) {
  return `service_status.component.${id}`;
}

export function incidentStatusLabelKey(status) {
  return `service_status.incident_status.${status || "investigating"}`;
}

export function impactLabelKey(impact) {
  return `service_status.impact.${impact || "minor"}`;
}

/** El peor de una lista de estados. `unknown` pierde contra cualquier dato
 * real, igual que en el backend: un hueco de observación no es una caída. */
export function worstStatus(statuses) {
  let worst = null;
  for (const s of statuses || []) {
    if (!s || s === "unknown" || s === "no_data") continue;
    if (worst === null || (STATUS_RANK[s] ?? 0) > (STATUS_RANK[worst] ?? 0)) worst = s;
  }
  return worst || "unknown";
}

/** Clave de dismiss del banner.
 *
 * Incluye `updated_at` a propósito: si la key fuera sólo el id, un usuario
 * que cierra la barra dejaría de ver TODAS las actualizaciones siguientes
 * del mismo incidente — incluida la que dice que empeoró. Cada novedad
 * publicada vuelve a mostrarse una vez.
 */
export function bannerDismissKey(summary) {
  if (!summary) return null;
  if (summary.incident) {
    return `genly_status_dismiss:i${summary.incident.id}:${summary.incident.updated_at || ""}`;
  }
  if ((summary.auto_affected || []).length) {
    return `genly_status_dismiss:auto:${[...summary.auto_affected].sort().join(",")}`;
  }
  return null;
}

/** Formatea el uptime junto con su cobertura.
 *
 * Nunca devuelve un porcentaje pelado: un 100% calculado sobre 3% de
 * cobertura no es un 100%, y esconder ese dato es exactamente la clase de
 * mentira que vuelve inútil una página de status.
 */
export function formatUptime(uptimePct, coveragePct) {
  if (uptimePct === null || uptimePct === undefined) return null;
  return {
    value: `${Number(uptimePct).toFixed(2)}%`,
    lowCoverage: Number(coveragePct || 0) < 50,
    coverage: `${Math.round(Number(coveragePct || 0))}%`,
  };
}
