// Per-step display + ETA metadata for the JobDetail progress panel.
//
// The backend (pipeline.py:run_pipeline) emits a small set of
// `current_step` values via update_job. The frontend turns those
// machine names into human Spanish + a "tipically takes ~X" hint so
// users can tell "this is taking a while" from "something's wrong"
// without having to ask an operator.
//
// `expectedSeconds` is the p75 we've observed in production, not the
// p99 — the goal is to set realistic expectations. If a step blows
// past 2× expectedSeconds, JobDetail surfaces the "tardando más de lo
// usual" banner with a force-fail escape hatch.
//
// Keep this list in sync with pipeline.py — every `update_job(
// current_step="X", ...)` must have a matching entry here. Missing
// entries fall back to the raw step name + a generic message.

export const STEP_META = {
  starting: {
    label: "Preparando",
    hint: "Descargando audio y configurando el worker",
    expectedSeconds: 30,
  },
  whisper: {
    label: "Transcribiendo el audio",
    hint: "Whisper analiza la canción y saca la letra con timestamps",
    expectedSeconds: 90,
  },
  lyrics_analysis: {
    label: "Analizando letra con IA",
    hint: "Gemini decide el estilo visual del fondo",
    expectedSeconds: 5,
  },
  background: {
    label: "Generando fondo con Veo",
    hint: "Veo 3.1 genera el clip de fondo · tarda ~80 seg",
    expectedSeconds: 120,
  },
  validation: {
    label: "Validando contenido del fondo",
    hint: "Gemini Vision chequea frames contra políticas UMG",
    expectedSeconds: 15,
  },
  video: {
    label: "Renderizando el video",
    hint: "moviepy + ffmpeg superponen letra al fondo · el paso más largo",
    expectedSeconds: 480,        // 8 min HD; 4K se va a ~20 min
  },
  short: {
    label: "Generando el Short",
    hint: "Corte vertical del estribillo a 1080×1920",
    expectedSeconds: 60,
  },
  thumbnail: {
    label: "Generando miniatura",
    hint: "Frame del título a 1280×720",
    expectedSeconds: 10,
  },
};

// Returns a human-readable step label, falling back to the raw key.
export function stepLabel(step) {
  return STEP_META[step]?.label || step || "Procesando";
}

// Returns the secondary "hint" line for the step, or a generic one.
export function stepHint(step) {
  return STEP_META[step]?.hint || "Trabajando en tu video";
}

// Returns expectedSeconds (number) for the step, defaulting to 120
// when we don't have data. Used by the "tardando" banner to decide
// when to surface the soft warning.
export function stepExpectedSeconds(step) {
  return STEP_META[step]?.expectedSeconds || 120;
}

// Formats a duration in seconds as "1 min", "5 min", "1 hs", etc.
// Optimized for Spanish operator UI — no decimals, no precision
// beyond what's useful at the scale we're showing.
export function formatElapsed(seconds) {
  if (seconds < 60) return `${Math.round(seconds)} seg`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} min`;
  return `${(seconds / 3600).toFixed(1)} hs`;
}

// "Empezó hace X" — used in the progress panel header so the user
// can tell at a glance whether a render is 1 minute in or 30 min in.
export function elapsedSinceLabel(createdAtIso) {
  if (!createdAtIso) return "";
  const startedMs = Date.parse(createdAtIso);
  if (Number.isNaN(startedMs)) return "";
  const secs = Math.max(0, (Date.now() - startedMs) / 1000);
  return `hace ${formatElapsed(secs)}`;
}
