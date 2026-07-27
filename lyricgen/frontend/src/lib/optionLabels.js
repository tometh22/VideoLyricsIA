// Etiquetas legibles de las opciones de Movimiento y Efecto.
//
// Estaban duplicadas en TRES lugares: el cuerpo de render de UploadZone
// (MOVEMENT_META / EFFECTS), los mapas moveLabel/effectLabel de
// WizardLivePreview, y ahora las necesitaba la ficha del video. Tres copias de
// los mismos strings es cómo un mismo código termina llamándose distinto en dos
// pantallas — el problema que este trabajo entero viene arreglando.
//
// Los CÓDIGOS siguen en lib/catalogCodes.js (contrato de paridad con el
// backend, asertado por lib/renderParity.test.js). Acá sólo la capa de texto.
//
// Son funciones de `t` en vez de constantes porque i18n es un hook: el valor
// depende del idioma activo.

/** code → etiqueta de Movimiento. "" (Auto) incluido. */
export const MOVEMENT_LABELS = (t) => ({
  "": t("upload.movement_auto") || "Auto",
  estatico: t("upload.movement_estatico") || "Estático (cámara fija)",
  sutil: t("upload.movement_sutil") || "Sutil (mínimo movimiento)",
  estandar: t("upload.movement_estandar") || "Cinematográfico",
  "foto-parallax": t("upload.movement_foto_parallax") || "Foto fija",
  animado: t("upload.movement_animado") || "Animado (ilustración)",
});

/** code → etiqueta de Efecto. "" (Ninguno) incluido. */
export const EFFECT_LABELS = (t) => ({
  "": t("upload.effect_none") || "Ninguno",
  snow: t("upload.effect_snow") || "Nieve",
  rain: t("upload.effect_rain") || "Lluvia",
  stars: t("upload.effect_stars") || "Estrellas",
  light: t("upload.effect_light") || "Luces",
  bokeh: t("upload.effect_bokeh") || "Bokeh",
  aurora: t("upload.effect_aurora") || "Aurora",
  dust: t("upload.effect_dust") || "Polvo de luz",
  embers: t("upload.effect_embers") || "Chispas",
  petals: t("upload.effect_petals") || "Pétalos",
  prism: t("upload.effect_prism") || "Prisma",
  confetti: t("upload.effect_confetti") || "Confeti",
  film: t("upload.effect_film") || "Película",
  scanlines: t("upload.effect_scanlines") || "Barrido retro",
  fog: t("upload.effect_fog") || "Niebla",
  shapes: t("upload.effect_shapes") || "Figuras",
  liquid_glass: t("upload.effect_liquid_glass") || "Liquid Glass",
  caustics: t("upload.effect_caustics") || "Reflejos de agua",
  rgb_glitch: t("upload.effect_rgb_glitch") || "RGB Glitch",
  neon_edge: t("upload.effect_neon_edge") || "Bordes neón",
  shadow_play: t("upload.effect_shadow_play") || "Juego de sombras",
  kaleido: t("upload.effect_kaleido") || "Kaleido Drift",
  halftone: t("upload.effect_halftone") || "Semitono",
  ink_reveal: t("upload.effect_ink_reveal") || "Tinta viva",
  heatwave: t("upload.effect_heatwave") || "Onda de calor",
  chromatic_pulse: t("upload.effect_chromatic_pulse") || "Pulso cromático",
  cutout_echo: t("upload.effect_cutout_echo") || "Eco recortado",
  projector: t("upload.effect_projector") || "Proyector",
  bass_pulse: t("upload.effect_bass_pulse") || "Pulso de graves",
  beat_flash: t("upload.effect_beat_flash") || "Flash al beat",
  chromatic_hit: t("upload.effect_chromatic_hit") || "Golpe cromático",
  beat_ripple: t("upload.effect_beat_ripple") || "Onda al beat",
  echo_hit: t("upload.effect_echo_hit") || "Eco de golpe",
});

/**
 * axisKey → (code → etiqueta), para los ejes enum que no tienen catálogo propio.
 *
 * Los CÓDIGOS acá son contrato con el backend, no invención: `text_contrast` es
 * `subtle|medium|strong` (main.py lo valida contra esa tupla y
 * pipeline._CONTRAST_SETTINGS tiene esas keys). Una primera versión de la ficha
 * escribió `low|medium|high` — `low`/`high` no existen y `medium` es el default
 * filtrado, así que los ÚNICOS dos valores que podían aparecer salían crudos.
 * El test tampoco lo vio porque codificaba los mismos códigos inventados.
 */
export const AXIS_VALUE_LABELS = (t) => ({
  // Localizados: el picker del wizard los tiene hardcodeados en español
  // (const TEXT_CASE_OPTS / FRAME_FORMAT_OPTS, fuera del componente y sin
  // acceso a `t`), pero la ficha sí puede traducirlos — un operador en en/pt
  // no tiene por qué leer "todo en minúsculas".
  text_case: {
    upper: t("detail.case_upper") || "Todo en MAYÚSCULAS",
    title: t("detail.case_title") || "Primera letra de Cada Palabra",
    lower: t("detail.case_lower") || "todo en minúsculas",
    sentence: t("detail.case_sentence") || "Primera letra de cada línea",
    original: t("detail.case_original") || "Sin cambios",
  },
  frame_format: {
    full: t("detail.frame_full") || "Pantalla completa (16:9)",
    cine: t("detail.frame_cine") || "Cine — franjas (2.39:1)",
  },
  text_contrast: {
    subtle: t("upload.contrast_subtle") || "Suave",
    medium: t("upload.contrast_medium") || "Medio",
    strong: t("upload.contrast_strong") || "Fuerte",
  },
  lyrics_animation: {
    none: t("upload.anim_none") || "Ninguna",
    karaoke: t("upload.anim_karaoke") || "Karaoke",
    word_reveal: t("upload.anim_reveal") || "Revelado",
    pop: t("upload.anim_pop") || "Pop",
    glow: t("upload.anim_glow") || "Glow",
  },
  line_transition: {
    none: t("upload.trans_none") || "Corte",
    slide_up: t("upload.trans_slide_up") || "Slide ↑",
    slide_side: t("upload.trans_slide_side") || "Slide →",
    wipe: t("upload.trans_wipe") || "Wipe",
    dissolve_blur: t("upload.trans_blur") || "Disolvencia",
  },
  title_template: {
    auto: t("upload.titlecard_auto") || "Auto",
    centered: t("upload.titlecard_centered") || "Centrada",
    lower_third: t("upload.titlecard_lower_third") || "Tercio inferior",
    badge: t("upload.titlecard_badge") || "Badge",
  },
});

/**
 * Género y concepto: los pickers los etiquetan por i18n con la key derivada del
 * código, así que la ficha hace lo mismo en vez de mostrar "hiphop" o
 * "atmosferico" crudos. Si la key no existe, `t` devuelve la key misma → se
 * detecta y se cae al código, que sigue siendo mejor que un string con puntos.
 */
export const dynamicAxisLabel = (t, axisKey, code) => {
  const prefix = { genre: "upload.genre_", concept: "upload.concept_" }[axisKey];
  if (!prefix) return null;
  const key = `${prefix}${String(code).trim().toLowerCase()}`;
  const label = t(key);
  return !label || label === key ? null : label;
};

/**
 * code → nombre de la tipografía.
 *
 * No van por i18n (salvo "Auto"): son nombres de marca y no se traducen.
 * `FONT_BY_CODE` (components/fontCatalog) tiene el CSS y el weight pero NO el
 * label, así que sin este mapa la ficha del video mostraba el código crudo
 * ("poppins-bold"). Los códigos son el contrato con pipeline._FONT_CATALOGUE,
 * asertado por lib/renderParity.test.js.
 */
export const FONT_LABELS = (t) => ({
  "": t("upload.font_auto") || "Auto",
  fredoka: "Fredoka (redondeada)",
  quicksand: "Quicksand (suave)",
  nunito: "Nunito (amigable)",
  "jost-bold": "Jost (estilo Futura)",
  "montserrat-bold": "Montserrat",
  "poppins-bold": "Poppins",
  "outfit-bold": "Outfit (estilo Gilroy)",
  "roboto-bold": "Roboto",
  "bebas-neue": "Bebas Neue",
  "oswald-bold": "Oswald",
  anton: "Anton",
});
