// Payload del POST /jobs/{parent_job_id}/variant desde el wizard de
// "Crear variante".
//
// A diferencia del wizard de EDICIÓN (que manda un DIFF contra el
// baseline, porque parchea un job existente y cada bucket cuesta un
// re-render), la variante es un JOB NUEVO: mandamos el estado ABSOLUTO de
// todos los ejes overridables. La herencia del padre ya está cubierta
// porque el wizard viene sembrado de su render_params, así que "absoluto"
// y "diff + herencia" dan el mismo resultado — pero absoluto no tiene
// ambigüedad (nada depende de que el baseline y la semilla coincidan) y
// no puede caer en el "no cambiaste nada" del flujo de edición.
//
// Los nombres de salida son los del body de VariantJobRequest (snake_case,
// backend main.py `_VARIANT_OVERRIDABLE_FIELDS`). Si un campo se agrega
// acá y no existe allá, el backend lo ignora en silencio: eso es
// exactamente el control "editable pero ignorado" que el PR #977 prohíbe,
// así que los dos lados se tocan juntos.
//
// Función pura: sin fetch, sin React. Testeable sin DOM.

import { backgroundRegenExtras } from "./editWizardDiff.js";

const str = (v) => (v == null ? "" : String(v));

function num(v, fallback) {
  const n = parseFloat(v);
  return Number.isFinite(n) ? n : fallback;
}

/**
 * @param {object} args
 * @param {object} args.review       currentReview del wizard (camelCase)
 * @param {string} args.style        paleta elegida (state top-level de App)
 * @param {string} args.customColors paleta custom (state top-level de App)
 * @param {string} args.bgSelectMode tab activo del paso de fondo
 * @param {number|null} args.backgroundId    asset de Biblioteca elegido
 * @param {string} args.backgroundMode       "as_is" | "variation"
 */
export function buildVariantPayload({
  review,
  style,
  customColors,
  bgSelectMode,
  backgroundId,
  backgroundMode,
} = {}) {
  const r = review || {};

  const payload = {
    // ── Escena / fondo ──────────────────────────────────────────────
    // "" es un CLEAR explícito del prompt del operador, no un "no lo
    // mandes": el backend persiste "" y no revive el hint del padre.
    background_hint: str(r.backgroundHint).trim(),
    concept: str(r.concept).trim(),
    genre: str(r.genre).trim(),
    match_lyrics: !!r.matchLyrics,
    bg_verbatim: !!r.bgVerbatim,
    // El MOTOR (Veo/Imagen) no viaja: lo deriva movement_style
    // ("foto-parallax" → Imagen, resto → Veo). No duplicamos ese eje.
    movement_style: str(r.movementStyle),
    // ── FX + animaciones de letra ───────────────────────────────────
    effect: str(r.effect),
    lyrics_animation: str(r.lyricsAnimation) || "none",
    line_transition: str(r.lineTransition) || "none",
    // ── Tipografía ──────────────────────────────────────────────────
    font: str(r.font),
    font_scale: num(r.fontScale, 1.0),
    text_case: str(r.textCase) || "upper",
    text_contrast: str(r.textContrast) || "medium",
    frame_format: str(r.frameFormat) || "full",
    // ── Portada / title card ────────────────────────────────────────
    title_template: str(r.titleTemplate) || "auto",
    title_size: num(r.titleSize, 1.0),
    title_artist_font: str(r.titleArtistFont),
    title_song_font: str(r.titleSongFont),
    title_song_break: str(r.titleSongBreak),
    // ── Paleta ──────────────────────────────────────────────────────
    // A diferencia de la edición (donde `style` es structural y el
    // control está bloqueado), /variant SÍ acepta el preset: la variante
    // genera un fondo nuevo desde cero.
    style: str(style) || "auto",
    custom_colors: str(customColors).trim(),
  };

  // Biblioteca de fondos: sólo cuenta si el tab activo es "library"
  // (mismo gate que appendBackgroundFields para /generate — un
  // backgroundId residual con el tab en "IA" no puede filtrarse).
  if (bgSelectMode === "library" && backgroundId) {
    payload.background_id = backgroundId;
    payload.background_mode = backgroundMode === "variation" ? "variation" : "as_is";
  }

  // Política de content-validation (fondo-libre). Siempre exactamente uno
  // de bypass/force: si no se manda ninguno el backend fail-closea a
  // force y las cuentas no-UMG pierden el modo fondo-libre.
  Object.assign(payload, backgroundRegenExtras(r));

  return payload;
}

export default buildVariantPayload;
