// Resumen legible de los ajustes con los que se RENDERIZÓ un video.
//
// Por qué existe: hasta ahora el operador no tenía forma de ver con qué se hizo
// un video. `JobDetail` no leía `render_params` (salvo `art_track`), y la única
// lista de ajustes vivía en el panel ADMIN — como un dump crudo de
// `Object.entries(render_params)` con keys snake_case. En el reclamo que originó
// esto, el operador regeneró el fondo SIETE veces sin poder ver que el video
// tenía guardado `movement_style: "animado"`. Con la ficha visible eso se
// detecta en dos segundos.
//
// El contrato de "qué ejes importan" es `_VARIANT_OVERRIDABLE_FIELDS`
// (backend/main.py): son exactamente los que el operador puede setear. NO se usa
// `admin_insights.job_choices()` —que era el candidato obvio— porque devuelve
// `background_hint` como BOOLEANO y omite font_scale, text_contrast,
// frame_format, genre, concept, match_lyrics y title_size: justo varios de los
// ejes que este panel existe para mostrar.
//
// Las ETIQUETAS se resuelven con el catálogo que ya usan los pickers, así que
// la ficha y el wizard nunca pueden nombrar distinto a la misma opción.

/**
 * Ejes que el operador puede setear, agrupados como los ve en el wizard.
 * Espeja _VARIANT_OVERRIDABLE_FIELDS; el orden es el de lectura, no el del
 * backend. `key` es la key de render_params (snake_case).
 */
export const SETTINGS_GROUPS = [
  {
    id: "fondo",
    labelKey: "detail.settings_group_bg",
    labelFallback: "Fondo",
    axes: [
      { key: "movement_style", labelKey: "detail.axis_movement", labelFallback: "Movimiento", kind: "movement" },
      { key: "effect", labelKey: "detail.axis_effect", labelFallback: "Efecto", kind: "effect" },
      { key: "genre", labelKey: "detail.axis_genre", labelFallback: "Género", kind: "raw" },
      { key: "concept", labelKey: "detail.axis_concept", labelFallback: "Concepto", kind: "raw" },
      { key: "background_hint", labelKey: "detail.axis_prompt", labelFallback: "Prompt", kind: "prompt" },
    ],
  },
  {
    id: "letra",
    labelKey: "detail.settings_group_lyrics",
    labelFallback: "Letra",
    axes: [
      { key: "font", labelKey: "detail.axis_font", labelFallback: "Tipografía", kind: "font" },
      { key: "font_scale", labelKey: "detail.axis_font_scale", labelFallback: "Tamaño", kind: "scale" },
      { key: "text_case", labelKey: "detail.axis_text_case", labelFallback: "Mayúsculas", kind: "raw" },
      { key: "text_contrast", labelKey: "detail.axis_contrast", labelFallback: "Contraste", kind: "raw" },
      { key: "lyrics_animation", labelKey: "detail.axis_animation", labelFallback: "Animación", kind: "raw" },
      { key: "line_transition", labelKey: "detail.axis_transition", labelFallback: "Transición", kind: "raw" },
      { key: "frame_format", labelKey: "detail.axis_frame_format", labelFallback: "Formato", kind: "raw" },
    ],
  },
  {
    id: "portada",
    labelKey: "detail.settings_group_title",
    labelFallback: "Portada",
    axes: [
      { key: "title_template", labelKey: "detail.axis_title_template", labelFallback: "Disposición", kind: "raw" },
      { key: "title_size", labelKey: "detail.axis_title_size", labelFallback: "Tamaño del título", kind: "scale" },
      { key: "title_artist_font", labelKey: "detail.axis_title_artist_font", labelFallback: "Fuente del artista", kind: "font" },
      { key: "title_song_font", labelKey: "detail.axis_title_song_font", labelFallback: "Fuente del tema", kind: "font" },
    ],
  },
];

// Valores que significan "sin elección explícita": no se muestran como chip,
// porque un chip `Concepto: —` es ruido, no información.
const EMPTY_VALUES = new Set(["", "auto", "none", "ninguno"]);

// Defaults POR EJE que tampoco se muestran. Van aparte de EMPTY_VALUES porque
// no son "vacío": son valores reales que el pipeline usa cuando el operador no
// eligió nada. Sin esto la ficha mostraba `Formato: full` y `Contraste: medium`
// en casi todos los videos — ruido, y encima con el código interno a la vista.
const AXIS_DEFAULTS = {
  frame_format: "full",
  text_case: "upper",
  text_contrast: "medium",
};

// Etiquetas de los ejes enum que no tienen un catálogo compartido propio.
// Son las MISMAS que muestran los pickers del wizard (TEXT_CASE_OPTS,
// FRAME_FORMAT_OPTS y las listas de animación/transición de UploadZone): sin
// esto la ficha le mostraba "lower", "full" o "medium" —códigos internos en
// inglés— a un operador que trabaja en español.
const VALUE_LABELS = {
  text_case: {
    upper: "Todo en MAYÚSCULAS",
    title: "Primera letra de Cada Palabra",
    lower: "todo en minúsculas",
    sentence: "Primera letra de cada Línea",
    original: "Sin cambios",
  },
  frame_format: {
    full: "Pantalla completa (16:9)",
    cine: "Cine — franjas (2.39:1)",
  },
  text_contrast: { low: "Bajo", medium: "Medio", high: "Alto" },
  lyrics_animation: {
    none: "Ninguna", karaoke: "Karaoke", word_reveal: "Revelado",
    pop: "Pop", glow: "Glow",
  },
  line_transition: {
    none: "Corte", slide_up: "Slide ↑", slide_side: "Slide →",
    wipe: "Wipe", dissolve_blur: "Disolvencia",
  },
};

function isEmptyish(value, axisKey) {
  if (value == null) return true;
  const s = String(value).trim().toLowerCase();
  if (s === "" || EMPTY_VALUES.has(s)) return true;
  return axisKey != null && AXIS_DEFAULTS[axisKey] === s;
}

/**
 * Construye los chips a mostrar.
 *
 * @param {object} params  job.render_params
 * @param {object} deps    { t, movementLabel, effectLabel, fontLabel }
 *   Los resolvers vienen del catálogo real de los pickers para que la ficha y el
 *   wizard no puedan nombrar distinto la misma opción.
 * @returns {Array<{id, label, chips: Array<{key,label,value,isPrompt}>}>}
 */
export function buildSettingsSummary(params, deps = {}) {
  const p = params || {};
  const t = deps.t || ((_k, fb) => fb);
  const resolvers = {
    movement: deps.movementLabel,
    effect: deps.effectLabel,
    font: deps.fontLabel,
  };

  return SETTINGS_GROUPS.map((group) => {
    const chips = [];
    for (const axis of group.axes) {
      const raw = p[axis.key];
      if (isEmptyish(raw, axis.key)) continue;

      let value;
      if (VALUE_LABELS[axis.key]) {
        // Ejes enum con etiqueta propia: nunca mostrar el código interno.
        value = VALUE_LABELS[axis.key][String(raw).trim().toLowerCase()] || String(raw);
      } else if (axis.kind === "scale") {
        // 1.15 → "1.15×". Un 1.0 es el default y no se muestra: no pasa por
        // EMPTY_VALUES (es numérico), así que se filtra explícitamente acá.
        const n = parseFloat(raw);
        if (!Number.isFinite(n) || Math.abs(n - 1) < 1e-6) continue;
        value = `${n}×`;
      } else if (axis.kind === "prompt") {
        value = String(raw);
      } else {
        const resolve = resolvers[axis.kind];
        value = (resolve && resolve(String(raw))) || String(raw);
      }

      chips.push({
        key: axis.key,
        label: t(axis.labelKey, undefined) || axis.labelFallback,
        value,
        isPrompt: axis.kind === "prompt",
      });
    }
    return {
      id: group.id,
      label: t(group.labelKey, undefined) || group.labelFallback,
      chips,
    };
  }).filter((g) => g.chips.length > 0);
}

/**
 * Cómo se generó la escena, en una frase. Deriva de los mismos campos que
 * background_policy.resolve_creative_mode usa, así que dice la verdad sobre lo
 * que el worker hizo: un prompt no vacío le GANA a match_lyrics.
 */
export function describeSceneSource(params, t = (_k, fb) => fb) {
  const p = params || {};
  const hint = String(p.background_hint || "").trim();
  if (hint) {
    return p.bg_verbatim
      ? (t("detail.scene_prompt_verbatim") || "Tu prompt, tal cual")
      : (t("detail.scene_prompt_improved") || "Tu prompt, mejorado con IA");
  }
  return p.match_lyrics === false
    ? (t("detail.scene_auto") || "Escena automática (género y mood)")
    : (t("detail.scene_lyrics") || "Escena inspirada en la letra");
}

export default buildSettingsSummary;
