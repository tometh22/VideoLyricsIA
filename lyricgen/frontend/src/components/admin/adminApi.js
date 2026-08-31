// Helpers compartidos del Admin Panel v2.
//
// Todo lo que antes vivía repetido dentro del monolito AdminPanel.jsx
// (2.100+ líneas) vive acá una sola vez: auth, fetch con manejo de error,
// formateadores y mapas de estado. Las secciones importan de acá — si algo
// se formatea distinto entre secciones es un bug, no una decisión.

export const API = import.meta.env.VITE_API_URL || "";

export function authHeaders() {
  const token = localStorage.getItem("genly_token");
  return token ? { Authorization: `Bearer ${token}` } : {};
}

// fetch + auth + chequeo de res.ok en un solo lugar. Lanza Error con el
// detail del backend para que el caller haga flashError(err.message).
// (El gate check:fetch exige `if (!res.ok)` cerca de cada `await fetch` —
// los callers de fetchJson no contienen `fetch(` así que no lo disparan.)
export async function fetchJson(url, opts = {}) {
  const res = await fetch(url, {
    ...opts,
    headers: { ...authHeaders(), ...(opts.headers || {}) },
  });
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.detail || `HTTP ${res.status}`);
  }
  return res.json();
}

// --- Formateadores ----------------------------------------------------------

export function fmtDate(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleDateString("es-AR", { day: "2-digit", month: "short", year: "numeric" });
}

// Tiempo relativo compacto ("hace 5 min"). Fecha absoluta si pasó más de
// una semana.
export function fmtAgo(iso) {
  if (!iso) return "—";
  const mins = Math.floor((Date.now() - new Date(iso).getTime()) / 60000);
  if (mins < 1) return "ahora";
  if (mins < 60) return `hace ${mins} min`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `hace ${hours} h`;
  const days = Math.floor(hours / 24);
  if (days <= 7) return `hace ${days} d`;
  return fmtDate(iso);
}

// Duración compacta para "tiempo en la app": 45m, 2h 14m.
export function fmtDuration(seconds) {
  const s = Math.max(0, Number(seconds) || 0);
  const mins = Math.round(s / 60);
  if (mins < 60) return `${mins}m`;
  const hours = Math.floor(mins / 60);
  return `${hours}h ${mins % 60}m`;
}

export function fmtMoney(usd) {
  return `$${(Number(usd) || 0).toFixed(2)}`;
}

/** Plata que puede no existir. `null` → "—", NUNCA "$0.00".
 *
 * El backend devuelve `null` a propósito cuando la división no se puede
 * hacer: `cost_per_deliverable` con 0 entregables, `rejection_rate` sin
 * jobs terminados, `cost_per_client_song` sin ningún snapshot de costo.
 * `fmtMoney` los aplasta a "$0.00" —`Number(null) || 0`— y un "$0.00/video"
 * se lee como margen del 100%, que es exactamente al revés de lo que pasa.
 *
 * Un guión obliga a preguntar por qué; un cero no.
 */
export function fmtMoneyOrDash(usd) {
  if (usd === null || usd === undefined || Number.isNaN(Number(usd))) return "—";
  return fmtMoney(usd);
}

// --- Métricas de actividad --------------------------------------------------

// Suma de señales de retrabajo de una fila de /admin/activity.
// corrected_jobs = videos distintos con corrección manual (no autosaves).
export function reworkTotal(rw) {
  if (!rw) return 0;
  return (rw.variants || 0) + (rw.total_edits || 0) + (rw.retries || 0)
    + (rw.corrected_jobs || 0) + (rw.abandoned_recreated || 0);
}

// "Sin actividad" = no creó videos ni hizo retrabajos en la ventana.
export function hasActivity(u) {
  return (u.videos?.total || 0) > 0 || reworkTotal(u.rework) > 0;
}

// --- Mapas de estado y labels ------------------------------------------------

// Labels en español de las categorías de error (error_taxonomy.py del backend).
export const ERROR_CATEGORY_LABELS = {
  veo: "Fondo IA (Veo)",
  render: "Render / ffmpeg",
  upload: "Storage / R2",
  timing: "Timing de letra",
  validation: "Validación / política",
  reaper: "Worker colgado",
  timeout: "Timeout",
  unknown: "Otro",
};

// Status de jobs → label + clases. Único lugar donde se decide de qué color
// es cada estado en TODO el admin.
export const JOB_STATUS = {
  done: { label: "Listo", classes: "bg-accent/10 text-accent ring-accent/20" },
  pending_review: { label: "En revisión", classes: "bg-amber-500/10 text-amber-400 ring-amber-500/20" },
  editing: { label: "Editando", classes: "bg-amber-500/10 text-amber-400 ring-amber-500/20" },
  processing: { label: "Procesando", classes: "bg-brand/10 text-brand-light ring-brand/20" },
  queued: { label: "En cola", classes: "bg-brand/10 text-brand-light ring-brand/20" },
  transcribed_pending: { label: "Transcripto", classes: "bg-brand/10 text-brand-light ring-brand/20" },
  transcribed: { label: "Transcripto", classes: "bg-brand/10 text-brand-light ring-brand/20" },
  awaiting_upload: { label: "Esperando audio", classes: "bg-gray-500/10 text-gray-400 ring-gray-500/20" },
  error: { label: "Error", classes: "bg-red-500/10 text-red-400 ring-red-500/20" },
  validation_failed: { label: "Validación falló", classes: "bg-red-500/10 text-red-400 ring-red-500/20" },
  transcription_failed: { label: "Transcripción falló", classes: "bg-red-500/10 text-red-400 ring-red-500/20" },
  rejected: { label: "Rechazado", classes: "bg-red-500/10 text-red-400 ring-red-500/20" },
};

// Status de invoices → label + clases.
export const INVOICE_STATUS = {
  paid: { label: "Pagada", classes: "bg-accent/10 text-accent ring-accent/20" },
  pending: { label: "Pendiente", classes: "bg-amber-500/10 text-amber-400 ring-amber-500/20" },
  failed: { label: "Falló", classes: "bg-red-500/10 text-red-400 ring-red-500/20" },
  void: { label: "Anulada", classes: "bg-gray-500/10 text-gray-400 ring-gray-500/20" },
};

// Status de los checks de compliance UMG → label + clases.
export const COMPLIANCE_STATUS = {
  ok: { label: "OK", classes: "bg-accent/10 text-accent ring-accent/20" },
  confirmed: { label: "Confirmado", classes: "bg-accent/10 text-accent ring-accent/20" },
  pending: { label: "Pendiente", classes: "bg-amber-500/10 text-amber-400 ring-amber-500/20" },
  warning: { label: "Atención", classes: "bg-amber-500/10 text-amber-400 ring-amber-500/20" },
  error: { label: "Error", classes: "bg-red-500/10 text-red-400 ring-red-500/20" },
  fail: { label: "Falla", classes: "bg-red-500/10 text-red-400 ring-red-500/20" },
};

// Fallback neutral para status desconocidos.
export const STATUS_FALLBACK = { label: null, classes: "bg-gray-500/10 text-gray-400 ring-gray-500/20" };
