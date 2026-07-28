/**
 * Option-catalog CODES shared between the pickers and the parity gate.
 *
 * These mirror the backend catalogs (worker renders what the dropdown
 * promised): CONCEPT_CODES ↔ pipeline._CONCEPT_SCENE_GUIDE keys,
 * MOVEMENT_CODES ↔ pipeline._MOVEMENT_STYLE_RULES keys. Labels, descs and
 * sample assets stay in the components (they're i18n/UI concerns); only the
 * codes are the cross-language contract, asserted by lib/renderParity.test.js
 * against the generated shared/renderParity.json. The "" (Auto) option is
 * frontend-only and deliberately NOT part of the contract.
 */

export const CONCEPT_CODES = [
  "naturaleza",
  "tropical",
  "acuatico",
  "ciudad",
  "urbano",
  "industrial",
  "abstracto",
  "cosmico",
  "atmosferico",
  "romantico",
  "vintage",
  "cinematic",
  "club",
  "lujo",
  "minimalista",
];

export const MOVEMENT_CODES = [
  "estatico",
  "sutil",
  "estandar",
  "foto-parallax",
  "animado",
];

// Composable visual effects (overlay loops plus Foto viva's generative-first
// transform). Mirrors fx_compositor.EFFECTS and is asserted against the
// generated backend fixture in renderParity.test.js.
export const EFFECT_CODES = [
  "snow",
  "rain",
  "stars",
  "bokeh",
  "light",
  "aurora",
  "dust",
  "embers",
  "petals",
  "prism",
  "confetti",
  "film",
  "scanlines",
  "fog",
  "shapes",
  "liquid_glass",
  "caustics",
  "rgb_glitch",
  "neon_edge",
  "shadow_play",
  "kaleido",
  "halftone",
  "ink_reveal",
  "heatwave",
  "chromatic_pulse",
  "cutout_echo",
  "projector",
  "foto_viva",
  "bass_pulse",
  "beat_flash",
  "chromatic_hit",
  "beat_ripple",
  "echo_hit",
];

/**
 * Espejo JS de pipeline._normalize_movement_style.
 *
 * Por qué hace falta: el backend acepta `movement_style` como TEXTO LIBRE
 * (main.py lo documenta así) y lo persiste CRUDO en render_params. El que
 * normaliza es el pipeline, al renderizar. Así que un job puede tener
 * "dinamico" (lo mandan el editor de escena y el derivado por energía),
 * "static", "fija", "locked"… y ninguno matchea un código canónico.
 *
 * Sin este espejo, sembrar el valor del job en el wizard deja CERO tarjetas
 * resaltadas (ninguna `m.code === "dinamico"`), y si el operador "corrige" a
 * Cinematográfico el diff emite `estandar` — semánticamente idéntico tras
 * normalizar, o sea un render Veo pago que no cambia nada.
 *
 * Mantener sincronizado con pipeline._normalize_movement_style.
 */
const MOVEMENT_ALIASES = {
  static: "estatico",
  estatica: "estatico",
  "estática": "estatico",
  fija: "estatico",
  fixed: "estatico",
  tripod: "estatico",
  locked: "estatico",
  still: "estatico",
  "camara-fija": "estatico",
  subtle: "sutil",
  minimal: "sutil",
  minimo: "sutil",
  standard: "estandar",
  default: "estandar",
  // "dinamico" lo usan SceneEditModal y el derivado por energía. No es una
  // clave real del catálogo: normaliza a "estandar" (cinematográfico).
  dinamico: "estandar",
  "dinámico": "estandar",
  dynamic: "estandar",
  photo: "foto-parallax",
  parallax: "foto-parallax",
  "foto+parallax": "foto-parallax",
  foto_parallax: "foto-parallax",
  animated: "animado",
  illustration: "animado",
  cartoon: "animado",
};

/** Devuelve un código de MOVEMENT_CODES, o "" (Auto) si no se reconoce. */
export function normalizeMovementCode(value) {
  const s = String(value == null ? "" : value).trim().toLowerCase();
  if (!s) return "";
  // hasOwnProperty y no `MOVEMENT_ALIASES[s]`: un input como "constructor" o
  // "__proto__" resolvía contra Object.prototype y devolvía una FUNCIÓN, que
  // terminaba en el payload del edit. El backend devuelve "" para esos.
  if (Object.prototype.hasOwnProperty.call(MOVEMENT_ALIASES, s)) return MOVEMENT_ALIASES[s];
  if (MOVEMENT_CODES.includes(s)) return s;
  return "";
}
