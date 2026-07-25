// Contrato del submit del wizard de EDICIÓN, como funciones puras.
//
// Por qué existe este módulo: la lógica vivía inline en App.jsx (5188 líneas,
// que ningún test importa), así que lo único testeable era `computeFieldDiff`
// — y el diff NO decide el output. El output lo decide la degradación por
// status que venía después del diff. Resultado: `EditWizardSubmit.test.jsx`
// era un mirror hand-copiado que testeaba N POSTs mientras producción hacía
// UNO consolidado, y pasaba en verde mientras el comportamiento real divergía.
// Es exactamente el patrón que el repo ya retiró (ver la lápida del
// FontScalePicker mirror en lib/renderParity.test.js).
//
// Con `resolveEditSubmission` la UI y el POST leen la MISMA función: lo que el
// wizard muestra como "se va a aplicar" es literalmente lo que se manda.
// Sin eso, cualquier resumen/badge construido sobre el diff vuelve a mentir —
// una capa más arriba que el bug original.

import { computeFieldDiff } from "./editWizardDiff.js";
import { normalizeSegmentsForEdit } from "./lyricsEditSubmit.js";

// Prioridad de edit_type: el más complejo de los presentes gana.
// background_library (swap curado, $0, sin slot) supersede al regen IA;
// background regen Veo (pago); lyrics re-renderiza con nuevos segments;
// metadata sólo title card; typography el último.
export const EDIT_TYPE_PRIORITY = [
  "background_library",
  "background",
  "lyrics",
  "metadata",
  "typography",
];

const BACKGROUND_TYPES = ["background", "background_library"];

/**
 * Siembra los campos del wizard de edición desde la row del Job.
 *
 * `initialFields` es lo que el operador ve/edita (el snap del autosave gana si
 * refrescó mid-edit); `baseline` es el snapshot inmutable de cómo está
 * RENDERIZADO el video ahora — el diff del submit compara contra eso, así que
 * si el operador vuelve un campo a su valor original no se cobra un re-render.
 *
 * Vive acá y no inline en App.jsx para que el test del invariante lea la
 * construcción REAL. Cuando el fixture del test es una copia a mano, los dos
 * lados se ponen de acuerdo entre ellos y no con producción: así se colaron
 * los 5 `title_*` faltantes en `current`.
 *
 * @param {object} job    row del Job (snake_case, con render_params)
 * @param {object|null} snapReview currentReview del snapshot de sesión, si es reusable
 */
export function buildEditReview(job, snapReview = null) {
  const params = (job && job.render_params) || {};
  const snapR = snapReview || null;
  const pickSnapOr = (snapKey, fallback) =>
    (snapR && snapR[snapKey] != null && snapR[snapKey] !== "")
      ? snapR[snapKey]
      : fallback;

  const initialFields = {
    artist: pickSnapOr("artist", job.artist || ""),
    songTitle: pickSnapOr("songTitle", job.song_title || ""),
    font: pickSnapOr("font", params.font || ""),
    textCase: pickSnapOr("textCase", params.text_case || "upper"),
    textContrast: pickSnapOr("textContrast", params.text_contrast || "medium"),
    fontScale: String(pickSnapOr("fontScale", params.font_scale || "1.0")),
    lyricsAnimation: pickSnapOr("lyricsAnimation", params.lyrics_animation || "none"),
    lineTransition: pickSnapOr("lineTransition", params.line_transition || "none"),
    lyricColor: pickSnapOr("lyricColor", params.lyric_color || "#FFFFFF"),
    lyricSungColor: pickSnapOr("lyricSungColor", params.lyric_sung_color || "#FFFFFF"),
    movementStyle: pickSnapOr("movementStyle", params.movement_style || ""),
    // Ejes de escena editables en edición. matchLyrics: default true cuando
    // el render_params no lo trae (paridad con el backend match_lyrics=True).
    genre: pickSnapOr("genre", params.genre || ""),
    concept: pickSnapOr("concept", params.concept || ""),
    matchLyrics: snapR?.matchLyrics != null ? !!snapR.matchLyrics : (params.match_lyrics !== false),
    effect: pickSnapOr("effect", params.effect || ""),
    backgroundHint: pickSnapOr("backgroundHint", params.background_hint || ""),
    bgVerbatim: snapR?.bgVerbatim != null ? !!snapR.bgVerbatim : !!params.bg_verbatim,
    backgroundMode: pickSnapOr("backgroundMode", params.background_mode || ""),
    // frameFormat: lo sembraba SOLO el bootstrap de variante. En edición
    // quedaba undefined, así que `wizardFields` (que lo incluye) escribía
    // "full" fijo y un job `cine` perdía las barras en el preview.
    frameFormat: pickSnapOr("frameFormat", params.frame_format || "full"),
    // Title card customization (Full Rotor v1).
    titleTemplate: pickSnapOr("titleTemplate", params.title_template || "auto"),
    titleSize: String(pickSnapOr("titleSize", params.title_size || "1.0")),
    titleArtistFont: pickSnapOr("titleArtistFont", params.title_artist_font || ""),
    titleSongFont: pickSnapOr("titleSongFont", params.title_song_font || ""),
    // UI v1.1 (2026-05-30): manual song-title line break — "" = auto.
    titleSongBreak: pickSnapOr("titleSongBreak", params.title_song_break || ""),
  };

  const baseline = {
    artist: job.artist || "",
    songTitle: job.song_title || "",
    font: params.font || "",
    textCase: params.text_case || "upper",
    textContrast: params.text_contrast || "medium",
    fontScale: String(params.font_scale || "1.0"),
    lyricsAnimation: params.lyrics_animation || "none",
    lineTransition: params.line_transition || "none",
    lyricColor: params.lyric_color || "#FFFFFF",
    lyricSungColor: params.lyric_sung_color || "#FFFFFF",
    movementStyle: params.movement_style || "",
    genre: params.genre || "",
    concept: params.concept || "",
    matchLyrics: params.match_lyrics !== false,
    effect: params.effect || "",
    backgroundHint: params.background_hint || "",
    bgVerbatim: !!params.bg_verbatim,
    backgroundMode: params.background_mode || "",
    frameFormat: params.frame_format || "full",
    // Title card customization (Full Rotor v1) — baseline for the diff.
    titleTemplate: params.title_template || "auto",
    titleSize: String(params.title_size || "1.0"),
    titleArtistFont: params.title_artist_font || "",
    titleSongFont: params.title_song_font || "",
    // UI v1.1: baseline for the manual break.
    titleSongBreak: params.title_song_break || "",
    segments: JSON.parse(JSON.stringify(job.segments_json || [])),
  };

  return { initialFields, baseline };
}

/**
 * Snapshot del estado del wizard, en la forma que `computeFieldDiff` espera.
 *
 * TIENE que cubrir todas las claves de `baseline`: si una está en baseline y
 * no acá, `strEq(valor, undefined)` difea contra "" y el bucket se emite en
 * TODAS las ediciones. Eso es lo que pasaba con los 5 `title_*` y por eso la
 * guarda "No cambiaste nada" estaba muerta en edición (y un cambio de fondo en
 * un job `done` se degradaba a edición de letra en silencio, sin aviso).
 * El test estructural de editSubmission.test.js fija ese invariante.
 *
 * @param {object} review currentReview del wizard (camelCase)
 * @param {object} opts { editedSegments, bgSelectMode, backgroundId }
 */
export function buildEditCurrent(review, opts = {}) {
  const r = review || {};
  const { editedSegments, bgSelectMode, backgroundId } = opts;
  return {
    artist: r.artist,
    songTitle: r.songTitle,
    font: r.font,
    fontScale: r.fontScale,
    textCase: r.textCase,
    textContrast: r.textContrast,
    lyricsAnimation: r.lyricsAnimation,
    lineTransition: r.lineTransition,
    effect: r.effect,
    backgroundHint: r.backgroundHint,
    bgVerbatim: r.bgVerbatim,
    backgroundMode: r.backgroundMode,
    movementStyle: r.movementStyle,
    frameFormat: r.frameFormat,
    // lyricColor/lyricSungColor: `computeFieldDiff` no tiene rama para ellos
    // (nunca salen del browser), pero van acá igual para que el contrato
    // baseline↔current sea simétrico — el test estructural lo exige, y si
    // algún día se cablean no van a pisar el valor del job.
    lyricColor: r.lyricColor,
    lyricSungColor: r.lyricSungColor,
    // Ejes de escena editables en edición (cableados 2026-07-24). Llegan a
    // r.* vía onEditFieldChange (genre/concept por updateBatchDefault;
    // matchLyrics por selectSceneMode). baseline los siembra del mismo
    // render_params → un valor sin tocar no difea.
    genre: r.genre,
    concept: r.concept,
    matchLyrics: r.matchLyrics,
    // Portada: faltaban acá mientras baseline SÍ los tenía (bug de arriba).
    titleTemplate: r.titleTemplate,
    titleSize: r.titleSize,
    titleArtistFont: r.titleArtistFont,
    titleSongFont: r.titleSongFont,
    titleSongBreak: r.titleSongBreak,
    segments: editedSegments,
    // Pick de biblioteca en edit mode (PR #940 backend): la grilla del paso de
    // fondo YA escribía backgroundId en App, pero el submit lo ignoraba — el
    // operador "elegía" un fondo que nunca viajaba. Solo cuenta con el tab
    // Library activo; volver a "IA Auto" lo anula (null → sin bucket).
    editBackgroundId:
      (bgSelectMode === "library" && backgroundId) ? backgroundId : null,
    // "Regenerar fondo (nueva versión)": acción explícita del wizard para
    // forzar un re-render del fondo con el MISMO hint aunque no haya cambiado
    // ningún campo. Es una intención, no un campo del baseline.
    forceBackgroundRegen: !!r.forceBackgroundRegen,
  };
}

/**
 * Decide QUÉ se va a mandar y qué se va a descartar, sin hacer I/O.
 *
 * Los gates del backend (main.py): `lyrics` y `metadata` aceptan
 * done|pending_review|rejected; `typography`, `background` y
 * `background_library` exigen pending_review. Y `background`/
 * `background_library` se rechazan con 400 en jobs multi-escena.
 *
 * @returns {{
 *   editType: string|null,
 *   payload: object|null,
 *   willApply: object,          // bucket -> campos que el backend VA a aplicar
 *   willDrop: string[],         // buckets presentes en el diff que se descartan
 *   blocked: null | { reason: string, buckets: string[] },
 *   diff: object,
 *   presentBuckets: string[],
 * }}
 */
export function resolveEditSubmission({
  baseline,
  current,
  jobStatus,
  scenePlan,
} = {}) {
  const diff = computeFieldDiff(baseline || {}, current || {});
  const presentBuckets = Object.keys(diff);

  const empty = {
    editType: null,
    payload: null,
    willApply: {},
    willDrop: [],
    blocked: null,
    diff,
    presentBuckets,
  };

  if (presentBuckets.length === 0) return empty;

  let editType = EDIT_TYPE_PRIORITY.find((k) => diff[k]) || null;
  const willDrop = [];

  const isPendingReview = jobStatus === "pending_review";
  const hasScenes = !!(
    scenePlan &&
    Array.isArray(scenePlan.scenes) &&
    scenePlan.scenes.length > 0
  );

  const bgBucketsPresent = BACKGROUND_TYPES.filter((k) => diff[k]);

  // Multi-escena: el fondo es un timeline; el backend rechaza el edit con 400
  // (main.py). Se resuelve ANTES del gate de status porque el motivo que el
  // operador necesita leer es otro (regenerá la escena desde el filmstrip) y
  // aplica incluso en pending_review.
  if (hasScenes && bgBucketsPresent.length > 0) {
    if (presentBuckets.length === bgBucketsPresent.length) {
      return {
        ...empty,
        blocked: { reason: "scenes", buckets: bgBucketsPresent },
      };
    }
    willDrop.push(...bgBucketsPresent);
    editType = EDIT_TYPE_PRIORITY.find(
      (k) => diff[k] && !BACKGROUND_TYPES.includes(k),
    ) || null;
  } else if (!isPendingReview) {
    const isBgType = BACKGROUND_TYPES.includes(editType);
    if (isBgType && presentBuckets.length === bgBucketsPresent.length) {
      // Sólo fondo en un job ya aprobado: el backend lo rechaza. La salida
      // real es crear una variante.
      return {
        ...empty,
        blocked: { reason: "status", buckets: bgBucketsPresent },
      };
    }
    if (isBgType) {
      // Fondo + otra cosa: bajar a la siguiente prioridad permitida y
      // REGISTRAR que el fondo se descarta (antes se descartaba en silencio y
      // la telemetría igual reportaba "background").
      willDrop.push(...bgBucketsPresent);
      editType = EDIT_TYPE_PRIORITY.find(
        (k) => diff[k] && !BACKGROUND_TYPES.includes(k),
      ) || null;
    }
  }

  // typography standalone no se acepta en done/rejected: piggyback en una
  // lyrics edit con los segments actuales (font/case/etc son ungated y se
  // aplican en cualquier edit_type).
  if (!isPendingReview && editType === "typography") {
    editType = "lyrics";
    if (!diff.lyrics) {
      const segs = (current && current.segments) || [];
      diff.lyrics = { segments: normalizeSegmentsForEdit(segs) };
    }
  }

  if (!editType) {
    return { ...empty, willDrop };
  }

  // UN único payload con todos los campos cambiados. El backend aplica los
  // ungated en cualquier edit_type; los gated (background_*, segments para
  // lyrics) sólo si el edit_type matchea.
  const payload = { edit_type: editType };
  const willApply = {};
  for (const bucket of ["metadata", "typography", "lyrics", "background", "background_library"]) {
    if (!diff[bucket]) continue;
    Object.assign(payload, diff[bucket]);
    if (!willDrop.includes(bucket)) willApply[bucket] = diff[bucket];
  }

  return {
    editType,
    payload,
    willApply,
    willDrop,
    blocked: null,
    diff,
    presentBuckets,
  };
}

export default resolveEditSubmission;
