// One vocabulary for every campaign screen. The backend computes one stage per
// song (campaign_pipeline.py); this module only names, colors and orders them.
// Every class string is literal so Tailwind keeps it in the build.

export const STAGES = [
  { key: "audio", label: "Audio", title: "Preparando audio", hint: "Subida, separación y transcripción automática.", tone: "slate", human: false },
  { key: "lyrics", label: "Letra", title: "Letra por revisar", hint: "Escuchar y confirmar letra y tiempos.", tone: "amber", human: true },
  { key: "ready", label: "Lista", title: "Lista para generar", hint: "Letra aprobada, todavía sin video.", tone: "cyan", human: true },
  { key: "rendering", label: "Generando", title: "Generando video", hint: "Fondo y render en curso.", tone: "blue", human: false },
  { key: "qc", label: "QC video", title: "Video por revisar", hint: "Reproducir el video completo y aprobarlo.", tone: "amber", human: true },
  { key: "approved", label: "Aprobada", title: "Aprobada, sin enviar", hint: "Video aprobado que todavía no está en un portal.", tone: "emerald", human: true },
  { key: "delivered", label: "Entregada", title: "Entregada", hint: "Publicada en el portal del cliente.", tone: "violet", human: false },
];

export const EXTRA_STAGES = [
  { key: "attention", label: "Atención", title: "Requiere atención", hint: "Falló un paso automático; se puede reintentar.", tone: "red", human: true },
  { key: "discarded", label: "Descartadas", title: "Descartada", hint: "Fuera de la campaña; se puede recuperar.", tone: "muted", human: false },
];

const ALL = [...STAGES, ...EXTRA_STAGES];
const BY_KEY = Object.fromEntries(ALL.map((stage) => [stage.key, stage]));

export const TONES = {
  slate: { bar: "bg-slate-400/70", dot: "bg-slate-400", chip: "bg-slate-400/10 text-slate-200 ring-slate-300/20", text: "text-slate-200" },
  amber: { bar: "bg-amber-400", dot: "bg-amber-400", chip: "bg-amber-400/10 text-amber-200 ring-amber-300/25", text: "text-amber-200" },
  cyan: { bar: "bg-cyan-400", dot: "bg-cyan-400", chip: "bg-cyan-400/10 text-cyan-200 ring-cyan-300/25", text: "text-cyan-200" },
  blue: { bar: "bg-blue-400", dot: "bg-blue-400", chip: "bg-blue-400/10 text-blue-200 ring-blue-300/25", text: "text-blue-200" },
  emerald: { bar: "bg-emerald-400", dot: "bg-emerald-400", chip: "bg-emerald-400/10 text-emerald-200 ring-emerald-300/25", text: "text-emerald-200" },
  violet: { bar: "bg-brand", dot: "bg-brand", chip: "bg-brand/15 text-violet-200 ring-brand/35", text: "text-violet-200" },
  red: { bar: "bg-red-400", dot: "bg-red-400", chip: "bg-red-400/10 text-red-200 ring-red-300/25", text: "text-red-200" },
  muted: { bar: "bg-white/20", dot: "bg-white/30", chip: "bg-white/[0.05] text-ink-secondary ring-white/10", text: "text-ink-secondary" },
};

export function stageMeta(key) {
  return BY_KEY[key] || BY_KEY.attention;
}

export function toneOf(key) {
  return TONES[stageMeta(key).tone];
}

/** Art tracks skip lyrics: audio → video → QC → delivery. */
export function stagesFor(kind) {
  return kind === "art_track" ? STAGES.filter((stage) => !["lyrics", "ready"].includes(stage.key)) : STAGES;
}

export const STAGE_KEYS = ALL.map((stage) => stage.key);

// Legacy list payloads (before the pipeline field) still expose phase
// counters. Map them conservatively: approved+delivered collapse into
// "approved" because the portal was not consulted.
const PHASE_TO_STAGE = {
  waiting_upload: "audio", uploading: "audio", waiting_processing: "audio", transcribing: "audio",
  separating: "audio", separation_ready: "audio", lyrics_ready: "lyrics", lyrics_approved: "ready",
  rendering: "rendering", final_review: "qc", done: "approved", failed: "attention", discarded: "discarded",
};

export function campaignCounts(campaign) {
  const counts = Object.fromEntries(STAGE_KEYS.map((key) => [key, 0]));
  if (campaign?.pipeline?.counts) return { ...counts, ...campaign.pipeline.counts };
  for (const [phase, value] of Object.entries(campaign?.counters || {})) {
    const stage = PHASE_TO_STAGE[phase];
    if (stage) counts[stage] += Number(value) || 0;
  }
  return counts;
}

export function totalOf(counts, { includeDiscarded = false } = {}) {
  return STAGE_KEYS.reduce((sum, key) => (
    key === "discarded" && !includeDiscarded ? sum : sum + (Number(counts?.[key]) || 0)
  ), 0);
}

/** Songs that are finished from the operator's point of view. */
export function doneCount(counts) {
  return (Number(counts?.approved) || 0) + (Number(counts?.delivered) || 0);
}

/** The single most useful next step for the whole campaign. */
export function campaignNextStep(counts, kind = "lyric_video", { portalSends = true } = {}) {
  if (kind !== "art_track" && counts.lyrics > 0) return { stage: "lyrics", label: `Revisar ${counts.lyrics} ${counts.lyrics === 1 ? "letra" : "letras"}` };
  if (counts.qc > 0) return { stage: "qc", label: `Revisar ${counts.qc} ${counts.qc === 1 ? "video" : "videos"}` };
  if (kind !== "art_track" && counts.ready > 0) return { stage: "ready", label: `Generar ${counts.ready} ${counts.ready === 1 ? "video" : "videos"}` };
  if (portalSends && counts.approved > 0) return { stage: "approved", label: `Enviar ${counts.approved} ${counts.approved === 1 ? "aprobada" : "aprobadas"}` };
  if (counts.attention > 0) return { stage: "attention", label: `Resolver ${counts.attention} ${counts.attention === 1 ? "problema" : "problemas"}` };
  if (counts.audio > 0 || counts.rendering > 0) return { stage: counts.audio > 0 ? "audio" : "rendering", label: "Procesando…", passive: true };
  return null;
}

// `?view=config` (not "settings": the app shell treats `view=settings` on any
// route as the account settings deep link from billing emails).
// `?view=` values used before the pipeline redesign stay valid so copied
// links, saved return paths and editor "Volver" keep landing somewhere sane.
const LEGACY_VIEWS = { review: "lyrics", creative: "ready", history: "qc", deliveries: "approved", contract: "config" };
const LEGACY_TABS = { pending: "lyrics", drafts: "lyrics", approved: "all", all: "all", discarded: "discarded" };
export const DETAIL_VIEWS = ["all", ...STAGE_KEYS, "changes", "config"];

export function resolveView(params, kind = "lyric_video") {
  const raw = params.get("view");
  if (raw && DETAIL_VIEWS.includes(raw)) return raw;
  if (raw && LEGACY_VIEWS[raw]) return LEGACY_VIEWS[raw];
  const tab = params.get("tab");
  if (tab && LEGACY_TABS[tab]) return kind === "art_track" && LEGACY_TABS[tab] === "lyrics" ? "qc" : LEGACY_TABS[tab];
  return "all";
}

/** Hours elapsed since `value`, skipping Saturdays and Sundays (local time). */
export function businessHoursSince(value, now = Date.now()) {
  if (value == null || value === "") return 0;
  const start = new Date(value).getTime();
  if (!Number.isFinite(start) || start >= now) return 0;
  let total = 0;
  let cursor = start;
  while (cursor < now) {
    const day = new Date(cursor);
    const nextMidnight = new Date(day.getFullYear(), day.getMonth(), day.getDate() + 1).getTime();
    const end = Math.min(now, nextMidnight);
    if (day.getDay() !== 0 && day.getDay() !== 6) total += end - cursor;
    cursor = end;
  }
  return total / 3600000;
}

/** Answer within 48 business hours; warn from 24. */
export const CHANGE_SLA_HOURS = 48;
export function changeSlaTone(hours) {
  if (hours >= CHANGE_SLA_HOURS) return "danger";
  if (hours >= CHANGE_SLA_HOURS / 2) return "warning";
  return "neutral";
}

export function formatDuration(seconds) {
  const value = Number(seconds);
  if (!Number.isFinite(value) || value <= 0) return "—";
  const rounded = Math.round(value);
  return `${Math.floor(rounded / 60)}:${String(rounded % 60).padStart(2, "0")}`;
}

export const PORTALS = {
  argentina: { label: "Argentina", host: "umg.genly.pro" },
  chile: { label: "Chile", host: "umgchile.genly.pro" },
};

/**
 * Catalog codes (ARF/ARUM, ISRC-like) are short. Some campaigns stored a
 * content hash in `technical_code`; it identifies nothing for a person, so
 * it is not shown in lists (the drawer still shows the raw value).
 */
export function displayCode(code) {
  const value = String(code || "").trim();
  if (!value || value.length > 20 || /^[0-9a-f]{24,}$/i.test(value)) return "";
  return value;
}

export function portalLabel(id) {
  return PORTALS[id]?.label || id;
}

/** Art-track campaigns may deliver loose files instead of a client portal. */
export const FILES_DESTINATION = "files";
export const ART_TRACK_PRESETS = [
  { key: "waveform", label: "Portada + onda", description: "La portada con barras que laten con el audio." },
  { key: "colombia_static", label: "Portada fija + título", description: "Portada quieta con título y artista, sin animación." },
];

export function artTrackPresetLabel(key) {
  return ART_TRACK_PRESETS.find((preset) => preset.key === key)?.label || ART_TRACK_PRESETS[0].label;
}

export function relativeDate(value, now = Date.now()) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const minutes = Math.round((now - date.getTime()) / 60000);
  if (minutes < 1) return "recién";
  if (minutes < 60) return `hace ${minutes} min`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `hace ${hours} h`;
  const days = Math.round(hours / 24);
  if (days < 8) return `hace ${days} ${days === 1 ? "día" : "días"}`;
  return date.toLocaleDateString("es-AR", { day: "numeric", month: "short" });
}
