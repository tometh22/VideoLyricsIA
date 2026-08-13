import { useState, useMemo, useRef, useEffect, useCallback } from "react";
import { createPortal } from "react-dom";
import { useI18n } from "../i18n";
import { EditorTour } from "./OnboardingTour";
import { useToast } from "./ToastProvider";
import HelpTip from "./HelpCenter/HelpTip";
import LyricsTimeline from "./LyricsTimeline";
import LyricVideoPreview from "./LyricVideoPreview";
import { tierForLength } from "../lib/lyricTiers";
import { activeWordIndex } from "../lib/karaokeTiming";
import { prettifySongTitle } from "../lib/prettifySongTitle";
import { reseedPreservingIds } from "../lib/segmentIds";
import {
  clampBlockShiftDelta,
  shiftBlockWithinDuration,
} from "../lib/segmentTiming";
import { useJobSegments, segmentsStore } from "../state/segmentsStore";
import { useUiStormDetector, recordEditorAction } from "../hooks/useUiStormDetector";
import { splitWordsAtCharOffset, firstWordStart, lastWordEnd } from "../lib/splitWords";
import useLocalStorage from "../hooks/useLocalStorage";
import { useEditorDocument } from "../hooks/useEditorDocument";
import { useEditorAutosave } from "../hooks/useEditorAutosave";
import { mergeThreeWay, segmentsEquivalent } from "../editorMerge";
import { createSaveQueue } from "../lib/saveQueue";
import ConflictDialog from "./ConflictDialog";
import VersionHistory from "./VersionHistory";
import WrapWarningDialog from "./WrapWarningDialog";

// Copy honesto del fallo de respaldo (autosave), por CAUSA real. El banner
// + el confirm de "Aprobar" antes decían "problema de red" para cualquier
// fallo (PR A). Pero persistSegmentsToBackend (App.jsx) distingue el motivo
// via { reason, status }: un 401 = sesión vencida (el auto-retry NO lo
// arregla — hay que reingresar), un 404 = job expirado por el reaper, etc.
// Decir siempre "red" es deshonesto y manda a la operadora por el camino
// equivocado. `server` es el fallback = el copy original (comportamiento sin
// cambios cuando el motivo es desconocido). El núcleo tranquilizador
// («Aprobar y generar» usa lo de pantalla) se mantiene en TODAS las causas —
// eso sigue siendo verdad porque el approve manda los segments en el body.
const _SAVE_ERROR_COPY = {
  network: {
    short: "No pudimos respaldar tu última edición (problema de red)",
    detail:
      "Tus cambios siguen acá y «Aprobar y generar» usa lo que ves en pantalla. Reintentamos automáticamente; evitá cerrar la pestaña hasta ver «Guardado».",
    confirm:
      "Tu última edición no se pudo respaldar en el servidor (problema de red). Podés aprobar igual: el video se genera con lo que ves en pantalla. Solo el respaldo para reanudar la sesión queda desactualizado. ¿Continuar?",
  },
  session: {
    short: "No pudimos respaldar tu última edición (tu sesión venció)",
    detail:
      "Tus cambios siguen acá y «Aprobar y generar» usa lo que ves en pantalla. El reintento automático no alcanza si la sesión expiró: reingresá en otra pestaña para que el respaldo vuelva a guardarse.",
    confirm:
      "Tu última edición no se pudo respaldar (tu sesión venció). Podés aprobar igual: el video se genera con lo que ves en pantalla. Pero si vas a cerrar y reanudar después, reingresá primero para no perder el respaldo. ¿Continuar?",
  },
  "job-gone": {
    short: "No pudimos respaldar: este trabajo ya no está en el servidor",
    detail:
      "Puede haber expirado por inactividad. Tus cambios siguen acá y «Aprobar y generar» usa lo que ves en pantalla, pero al generar el servidor podría rechazarlo. Si falla, volvé a subir la canción.",
    confirm:
      "Este trabajo ya no está en el servidor (pudo expirar por inactividad). Tus cambios siguen en pantalla, pero al generar podría fallar. ¿Intentar aprobar igual?",
  },
  "draft-corrupt": {
    short: "Encontramos un borrador local que necesita revisión",
    detail:
      "No lo enviamos ni sobrescribimos la versión del equipo. El borrador permanece en este navegador para recuperación manual.",
    confirm: "Revisá el borrador local antes de aprobar.",
  },
  offline: {
    short: "Estás sin conexión",
    detail: "Tus cambios siguen guardados localmente. Al volver la conexión compararemos primero la versión del equipo.",
    confirm: "Esperá a recuperar la conexión antes de aprobar.",
  },
  server: {
    short: "No pudimos respaldar tu última edición en el servidor",
    detail:
      "Tus cambios siguen acá y «Aprobar y generar» usa lo que ves en pantalla. Reintentamos automáticamente; evitá cerrar la pestaña hasta ver «Guardado».",
    confirm:
      "Tu última edición no se pudo respaldar en el servidor. Podés aprobar igual: el video se genera con lo que ves en pantalla. Solo el respaldo para reanudar la sesión queda desactualizado. ¿Continuar?",
  },
  conflict: {
    short: "Conflicto: cambios no guardados",
    detail:
      "Otra pestaña o dispositivo guardó una versión más nueva. Tu borrador sigue a salvo en este navegador.",
    confirm:
      "Hay una versión más nueva en el servidor. Resolvé el conflicto antes de aprobar para evitar sobrescribir cambios de otra sesión.",
  },
};

// Deriva la categoría de copy desde el { reason, status } que retorna
// persistSegmentsToBackend. Un throw del fetch (result undefined) = red.
function _saveErrorCategory(result) {
  if (!result) return "network"; // fetch tiró (catch) → sin red
  const status = result.status;
  const reason = result.reason || "";
  if (reason === "network") return "network";
  if (status === 401 || status === 403) return "session";
  if (reason === "job-gone" || status === 404) return "job-gone";
  if (reason === "stale-revision" || status === 409) return "conflict";
  return "server"; // 400/409/5xx/otros → copy genérico honesto
}

// Font options for the live in-preview switcher. Codes match the render
// pipeline / EditRequestPanel; css families are all loaded in index.html so
// the preview renders the real typeface. "" = Auto (pipeline picks).
const EDITOR_FONTS = [
  { code: "", label: "Auto", css: "'Montserrat', sans-serif" },
  { code: "anton", label: "Anton", css: "'Anton', sans-serif" },
  { code: "bebas-neue", label: "Bebas Neue", css: "'Bebas Neue', sans-serif" },
  { code: "oswald-bold", label: "Oswald", css: "'Oswald', sans-serif" },
  { code: "montserrat-bold", label: "Montserrat", css: "'Montserrat', sans-serif" },
  { code: "poppins-bold", label: "Poppins", css: "'Poppins', sans-serif" },
  { code: "outfit-bold", label: "Outfit", css: "'Outfit', sans-serif" },
  { code: "roboto-bold", label: "Roboto", css: "'Roboto', sans-serif" },
  { code: "jost-bold", label: "Jost", css: "'Jost', sans-serif" },
];
const FONT_CSS_BY_CODE = Object.fromEntries(EDITOR_FONTS.map((f) => [f.code, f.css]));

// Typography options for the live preview controls. Codes match the render
// pipeline (pipeline.py / ass_render / UploadZone).
const TEXT_CASES = [
  { code: "upper", label: "MAY" },
  { code: "title", label: "Aa" },
  { code: "lower", label: "min" },
  { code: "sentence", label: "Abc" },
  { code: "original", label: "ori" },
];
// TRANSITIONS (Cut/Fade fade-time) quedó deprecado 2026-05-23. Las opciones
// ricas viven en LINE_TRANSITIONS (slide/wipe/dissolve_blur) que se aplican
// vía libass (lugar único, sin override silencioso de moviepy).
const CONTRASTS = [
  { code: "subtle", label: "Suave" },
  { code: "medium", label: "Medio" },
  { code: "strong", label: "Fuerte" },
];
// Animación de letra — libass templates (mirror del wizard, ass_render.py).
const LYRICS_ANIMATIONS = [
  { code: "none",        label: "Ninguna" },
  { code: "karaoke",     label: "Karaoke" },
  { code: "word_reveal", label: "Reveal" },
  { code: "pop",         label: "Pop" },
  { code: "glow",        label: "Glow" },
];
// Transición de línea — entrada (y salida en dissolve_blur) por libass.
const LINE_TRANSITIONS = [
  { code: "none",          label: "Ninguna" },
  { code: "slide_up",      label: "Sube" },
  { code: "slide_side",    label: "Lateral" },
  { code: "wipe",          label: "Cortina" },
  { code: "dissolve_blur", label: "Desvanecer" },
];

function applyTextCase(text, code) {
  if (code === "upper") return (text || "").toUpperCase();
  if (code === "lower") return (text || "").toLowerCase();
  if (code === "title") return (text || "").replace(/\b\w/g, (c) => c.toUpperCase());
  if (code === "sentence") {
    return (text || "").toLowerCase().split("\n").map(
      (ln) => ln.replace(/[a-zà-ÿ]/i, (c) => c.toUpperCase())
    ).join("\n");
  }
  return text || "";
}

// SHOW_MOTION_PICKER (legacy text_motion) eliminado 2026-05-23 —
// reemplazado por lyrics_animation + line_transition (libass).

function formatTime(seconds) {
  if (!isFinite(seconds) || seconds < 0) return "0:00";
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

function formatTimestamp(seconds) {
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  const ms = Math.floor((seconds % 1) * 10);
  return `${m}:${s.toString().padStart(2, "0")}.${ms}`;
}

// Parse "M:SS.t", "M:SS", or a raw seconds value into a non-negative
// float. Returns null when the string can't be interpreted, so the
// caller can decide to ignore the edit instead of writing garbage.
function parseTimestamp(str) {
  if (str == null) return null;
  const trimmed = String(str).trim().replace(",", ".");
  if (!trimmed) return null;
  if (trimmed.includes(":")) {
    const parts = trimmed.split(":");
    if (parts.length !== 2) return null;
    if (!/^\d+$/.test(parts[0]) || !/^\d+(?:\.\d+)?$/.test(parts[1])) return null;
    const m = Number(parts[0]);
    const s = Number(parts[1]);
    if (!Number.isFinite(m) || !Number.isFinite(s)) return null;
    if (m < 0 || s < 0 || s >= 60) return null;
    return m * 60 + s;
  }
  if (!/^\d+(?:\.\d+)?$/.test(trimmed)) return null;
  const v = Number(trimmed);
  if (!Number.isFinite(v) || v < 0) return null;
  return v;
}

// Timestamps can come from the API as numbers, numeric strings, null, or
// malformed values. Keep one strict boundary sanitizer for every path that
// reads, sorts, restores, or persists timings. In particular, never use
// parseFloat here: Number("12abc") must be rejected rather than truncated.
function sanitizeSegmentTiming(segment, fallbackStart = 0) {
  const rawStart = segment?.start ?? segment?.startTime ?? segment?.start_time;
  const rawEnd = segment?.end ?? segment?.endTime ?? segment?.end_time;
  const parsedStart = parseTimestamp(rawStart);
  const start = parsedStart == null ? fallbackStart : parsedStart;
  const parsedEnd = parseTimestamp(rawEnd);
  const end = parsedEnd == null ? start + 1 : Math.max(start, parsedEnd);
  return { ...segment, start, end };
}

function sanitizeSegments(segments) {
  if (!Array.isArray(segments)) return [];
  let fallbackStart = 0;
  return segments.map((segment) => {
    const sanitized = sanitizeSegmentTiming(segment, fallbackStart);
    fallbackStart = sanitized.start;
    return sanitized;
  });
}

function sanitizeSegmentsForPersistence(segments) {
  // Editor 2.0 persists the whole segment contract. Backend validation only
  // normalizes start/end/text and deliberately preserves present/future
  // render metadata (_id, words, review, pos, scale, rot, etc.).
  return sanitizeSegments(segments).map((segment) => ({ ...segment }));
}

function findSuggestion(whisperText, refLines, startIdx) {
  if (!refLines.length) return null;
  const wLower = whisperText.toLowerCase().trim();
  let bestScore = 0;
  let bestLine = null;

  const searchStart = Math.max(0, startIdx - 3);
  const searchEnd = Math.min(refLines.length, startIdx + 10);

  for (let i = searchStart; i < searchEnd; i++) {
    const rLower = refLines[i].toLowerCase().trim();
    if (!rLower) continue;
    const wWords = wLower.split(/\s+/);
    const rWords = rLower.split(/\s+/);
    let matches = 0;
    for (const w of wWords) { if (rWords.includes(w)) matches++; }
    const score = matches / Math.max(wWords.length, rWords.length);
    if (score > bestScore) { bestScore = score; bestLine = refLines[i]; }

    if (i < refLines.length - 1) {
      const combined = rLower + " " + refLines[i + 1].toLowerCase().trim();
      const cWords = combined.split(/\s+/);
      let cMatches = 0;
      for (const w of wWords) { if (cWords.includes(w)) cMatches++; }
      const cScore = cMatches / Math.max(wWords.length, cWords.length);
      if (cScore > bestScore) { bestScore = cScore; bestLine = refLines[i] + " " + refLines[i + 1]; }
    }
  }

  if (bestScore > 0.3 && bestLine) {
    const normalize = (s) => s.toLowerCase().replace(/[^a-záéíóúüñ\s]/g, "").replace(/\s+/g, " ").trim();
    if (normalize(bestLine) !== normalize(whisperText)) {
      return bestLine;
    }
  }
  return null;
}

// Find two consecutive lines in `refLines` whose concatenation matches
// `segText`. Used by the auto-split banner: when a Whisper segment
// captures 2 lyric lines mergeadas en uno solo, lrclib plain (passed as
// referenceLyrics) has them como 2 entries. Si el match es lo
// suficientemente fuerte (>0.5), devolvemos el [lineA, lineB] que el
// caller usa para crear 2 segments separados.
//
// Threshold 0.5 — más permisivo que findSuggestion (0.3) porque acá
// estamos comparando contra la CONCATENACIÓN de 2 líneas vs 1 segment,
// el set de words es más grande y el match esperado es más alto.
function findReferenceSplitLines(segText, refLines) {
  if (!refLines || refLines.length < 2) return null;
  const normalize = (s) =>
    s.toLowerCase().replace(/[^a-záéíóúüñ\s]/g, "").replace(/\s+/g, " ").trim();
  const segNorm = normalize(segText);
  if (!segNorm) return null;
  const segWords = segNorm.split(/\s+/);
  if (segWords.length < 4) return null; // demasiado corto para split fiable

  let bestScore = 0;
  let bestPair = null;
  for (let i = 0; i < refLines.length - 1; i++) {
    const a = refLines[i];
    const b = refLines[i + 1];
    if (!a || !b) continue;
    const cNorm = normalize(a + " " + b);
    if (!cNorm) continue;
    const cWords = cNorm.split(/\s+/);
    let matches = 0;
    for (const w of segWords) {
      if (cWords.includes(w)) matches++;
    }
    const score = matches / Math.max(segWords.length, cWords.length);
    if (score > bestScore) {
      bestScore = score;
      bestPair = [a, b];
    }
  }
  if (bestScore > 0.5) return bestPair;
  return null;
}

// ─── Font-code → CSS map (mirrors UploadZone FONTS) ────────────────────────
const FONT_CSS_MAP = {
  "jost-bold":       "'Jost', sans-serif",
  "montserrat-bold": "'Montserrat', sans-serif",
  "poppins-bold":    "'Poppins', sans-serif",
  "outfit-bold":     "'Outfit', sans-serif",
  "roboto-bold":     "'Roboto', sans-serif",
  "bebas-neue":      "'Bebas Neue', sans-serif",
  "oswald-bold":     "'Oswald', sans-serif",
  "anton":           "'Anton', sans-serif",
  "":                "'Montserrat', sans-serif",
};

// Backend tier params come from the shared source of truth (lib/lyricTiers,
// also used by LyricVideoPreview). Adapter keeps this file's sizePx/maxWidthPx
// naming so the existing wrap-estimation code is untouched.
function getTier(text) {
  const tier = tierForLength((text || "").length);
  return { sizePx: tier.fontPx, maxWidthPx: tier.wrapPx };
}

// Simulate moviepy's word-wrap with canvas.measureText.
// Returns the number of visual lines the segment will occupy in the video.
function estimateWrappedLines(text, fontCss, sizePx, maxWidthPx) {
  try {
    const canvas = document.createElement("canvas");
    const ctx = canvas.getContext("2d");
    ctx.font = `bold ${sizePx}px ${fontCss}`;
    const spaceW = ctx.measureText(" ").width;
    const words = text.split(" ");
    let lines = 1;
    let lineW = 0;
    for (const word of words) {
      const ww = ctx.measureText(word).width;
      if (lineW > 0 && lineW + spaceW + ww > maxWidthPx) {
        lines++;
        lineW = ww;
      } else {
        lineW = lineW > 0 ? lineW + spaceW + ww : ww;
      }
    }
    return lines;
  } catch {
    return 1;
  }
}

// Mirror of the backend pipeline._smart_lower(): for the all-lowercase
// aesthetic we lowercase only the FIRST word of the line (sentence-initial
// capital is grammar, not intent) and keep every later word exactly as the
// operator typed it, so proper nouns like "Guinea" survive. Must stay in
// sync with pipeline.py:_smart_lower so the editor preview matches the
// rendered video (otherwise the editor shows "guinea" but the render shows
// "Guinea"). Origin: agus.cafisi / Babasónicos 2026-05-20.
export function smartLower(text) {
  let seenWord = false;
  return (text || "")
    .split(/(\s+)/)
    .map((tok) => {
      if (!tok || /^\s+$/.test(tok)) return tok;
      if (!seenWord) {
        seenWord = true;
        return tok.toLowerCase();
      }
      return tok; // interior word: keep operator's casing as typed
    })
    .join("");
}

// Apply the same case transform as the backend _apply_case().
function applyCase(text, textCase) {
  if (textCase === "upper") return text.toUpperCase();
  if (textCase === "title") return text.replace(/\b\w/g, (c) => c.toUpperCase());
  if (textCase === "lower") return smartLower(text);
  if (textCase === "sentence") {
    // Mirror pipeline._sentence_case(): smartLower (proper nouns survive)
    // then capitalize the first letter of each visual line.
    return smartLower(text).split("\n").map(
      (ln) => ln.replace(/[a-zà-ÿ]/i, (c) => c.toUpperCase())
    ).join("\n");
  }
  return text;
}

// Normalize a lyric line for repeat-detection: trim ends and collapse
// internal whitespace runs. Case- and accent-SENSITIVE on purpose, so we
// only ever group lines the operator typed identically and never touch a
// line they meant to be different.
export function normalizeLineForMatch(text) {
  return (text || "").trim().replace(/\s+/g, " ");
}

export default function LyricsEditor({
  // PR E (2026-07): `segments` es SOLO el seed inicial del store por job
  // (segmentsStore.useJobSegments). Post-mount, este prop ya NO re-seedea: el
  // viejo effect de prop-sync + sus 4 guards de eco fueron eliminados. Para un
  // eventual reemplazo externo del contenido existe segmentsStore.replace(),
  // que preserva la identidad de filas (reseedPreservingIds) — hoy sin caller
  // de producción (reservado / lo ejercitan sólo los tests).
  segments, filename, audioFile, referenceLyrics,
  coverageWarning = false, recoverySource = "",
  onApprove, onBack, isBatch = false, batchProgress = "",
  user = null,
  font = "",
  textCase = "upper",
  fontScale = 1.0,
  textContrast = "medium",
  // 2026-05-23: reemplazan a `lyricTransition` (Corte/Fade) y `textMotion`
  // (Sutil drift) que quedaron deprecados — éstos cubren mejor el mismo
  // espacio y NO apagan silenciosamente al ASS path.
  lyricsAnimation = "none",
  lineTransition = "none",
  transcribeJobId = null,
  segmentsRevision = 0,
  // PR E follow-up (2026-07): key DEL STORE, desacoplada del backend job id.
  // transcribeJobId gobierna el autosave/backend (POST /save-segments) y es el
  // job real (o null). storeKey identifica la review para el segmentsStore y
  // EXISTE incluso cuando transcribeJobId es null (reviews jobId-less: back-nav,
  // resume), así que sus edits sobreviven al unmount y llegan a wizardPersistence.
  // Default a transcribeJobId para que los unit tests que sólo pasan
  // transcribeJobId sigan keyando el store correctamente.
  storeKey = null,
  onPersistSegments = null,
  editorRequest = null,
  saveQueue = null,
  // Descarta caches/draft locales y vuelve a hidratar la versión canónica
  // después de un 409. El padre conoce el ciclo de vida del wizard.
  onReloadServer = null,
  // Versión B, parte 2 (2026-07-15): callback del padre que hace el POST
  // /jobs/{id}/reanchor (re-anclado CTC del timing con el texto corregido).
  // El botón "Re-sincronizar con IA" solo se muestra si el padre lo pasa Y
  // features.anchor_lyrics está activo (flag ANCHOR_LYRICS_ENABLED).
  onReanchor = null,
  // NOTE (PR E): el viejo `onEditedChange` (espejo sincrónico por keystroke
  // hacia App) fue eliminado — era la mitad del loop bidireccional del
  // reseed-storm. Los lectores externos (WizardLivePreview, snapshot de
  // wizardPersistence) leen ahora del segmentsStore vía
  // useJobSegmentsValue(jobId) / segmentsStore.get(jobId); el POST /edit
  // sigue recibiendo lo de pantalla vía onApprove(editedSegments).
  // Post-approval / re-sync mode. The wizard's upload flow never sets
  // these (defaults preserve original behavior); the JobDetail /edit
  // modal mounts this same editor with audioUrl + the disable flags so
  // the operator can fix sync on an already-approved job without
  // pulling in features that no longer apply.
  audioUrl: audioUrlProp = null,
  // El padre (App) trae el audio del /source-audio-url en segundo plano
  // (fire-and-forget con reintentos). Mientras ese fetch está EN VUELO,
  // audioUrl es null pero el audio SÍ existe — mostrar "Audio no disponible"
  // ahí es un falso alarma (reporte 2026-07-25: el banner aparecía ~30s y
  // recién después se podía escuchar). Este flag distingue "cargando" de
  // "no existe / se agotaron los reintentos".
  audioLoading = false,
  disableAutoSplit = false,
  disableBeforeUnload = false,
  disableAutosave = false,
  submitLabel = null,
  // Optional audio peak envelope for the timeline waveform, fetched by the
  // parent (the post-render /edit modal has a job in R2; the wizard doesn't).
  // null → timeline renders without a waveform (graceful).
  waveform = null,
  // Live preview: signed URL of the cached background video (post-render
  // modal). null → preview uses a style-tinted template gradient (wizard).
  previewBgUrl = null,
  // Background style name → template gradient for the wizard preview.
  backgroundStyle = "default",
  // px offset for the sticky header so it clears any sticky app header
  // above it. 0 in the modal (fixed overlay, no app chrome); the wizard
  // passes the app header height so the editor's CTA isn't cut off.
  stickyHeaderTop = 0,
  // Called when the operator picks a font in the live preview switcher.
  // Parent threads it into the render (render_params.font / edit_params).
  onFontChange = null,
  // Same idea for the rest of the typography, set live in the preview.
  onCaseChange = null,
  onContrastChange = null,
  // 2026-05-23: callbacks de los nuevos ejes (libass). Reemplazan al
  // onTransitionChange (que controlaba el legacy lyric_transition).
  onAnimationChange = null,
  onLineTransitionChange = null,
  // UX specialist 2026-05-24: status del pre-gen del fondo (useBackgroundPreview).
  // Valores: "idle" | "queued" | "generating" | "done" | "error" | "disabled".
  // null/undefined → no se renderiza el chip (modo /edit modal post-render).
  bgStatus = null,
  // Phase 2 (2026-05-25): cuando el editor se renderiza dentro del nuevo
  // paso 6 del wizard, los controles tipográficos (font/case/contrast/
  // animation/transition) ya están visibles en el paso 4 ("Animación")
  // del stepper — esconder la columna izquierda y el preview interno
  // para no duplicarlos. El preview central del wizard
  // (WizardLivePreview) sigue mostrando los cambios live. En modo /edit
  // (legacy) o uso standalone, el default es renderizar todo igual que
  // siempre.
  hideTypographyControls = false,
  hideInternalPreview = false,
  // Phase C 2026-05-25: callback que publica el tick de playback hacia
  // el padre (App.jsx). El padre típicamente escribe en un ref para
  // que el WizardLivePreview central pueda leerlo sin pasar por
  // setState (evita re-renders a 60fps del tree de UploadZone).
  // Firma: (activeLineText, activeStart, activeEnd, currentTime).
  // Se llama dentro del rAF loop existente (60fps). El consumer es
  // responsable de throttle si necesario.
  onPlaybackTick = null,
  // 2026-07-16 (idea de Tomi): en el wizard, el reproductor puede vivir
  // ABAJO del video (columna central) en vez de arriba de la lista, para
  // que la columna de la letra quede full y se scrollee menos. Si el padre
  // pasa un elemento DOM acá, el player bar se portalea a ese slot (bajo el
  // video); si es null (ej. /edit modal), se renderiza inline como siempre.
  // El estado del audio (isPlaying/currentTime/etc.) NO se mueve — solo el
  // DOM del control, vía React portal.
  playerSlot = null,
}) {
  const { t } = useI18n();
  // PR E (2026-07): `edited` vive en el segmentsStore (Map por jobId a
  // nivel módulo), NO en un useState local. El store SOBREVIVE al unmount:
  // navegar paso 6 → 4 → 6 en el wizard des-monta y re-monta este editor,
  // y antes eso re-seedeaba desde un prop `segments` stale — así se
  // "borraban los tiempos/locks" (P0 Seba+Gaby). Ahora el remount se
  // engancha a la entrada viva; seedFn corre solo la PRIMERA vez que se ve
  // este jobId. Sin jobId (unit tests / editor standalone) degrada a
  // estado local plano.
  // storeKey desacopla la identidad de store del backend job id: existe
  // incluso para reviews sin job (transcribeJobId null) para que sus edits
  // sobrevivan al unmount. Default a transcribeJobId para los unit tests que
  // sólo pasan transcribeJobId (así siguen keyando el store correctamente).
  const _storeKey = storeKey ?? transcribeJobId ?? null;
  const [edited, setEdited] = useJobSegments(
    _storeKey,
    () => reseedPreservingIds([], sanitizeSegments(segments)),
  );
  const sanitizedEdited = useMemo(() => sanitizeSegments(edited), [edited]);
  const [isDirty, setIsDirty] = useState(false);
  // Two views over the same editor state. The basic review flow is the
  // default; timing tools only appear after the operator explicitly opens
  // the advanced view.
  const editorPreferenceKey = `genly_editor_view:${user?.id || user?.username || "anonymous"}`;
  const [persistedViewMode, setPersistedViewMode] = useLocalStorage(editorPreferenceKey, "basic");
  const [anonymousViewMode, setAnonymousViewMode] = useState("basic");
  const hasEditorPreferenceOwner = Boolean(user?.id || user?.username);
  const viewMode = hasEditorPreferenceOwner ? persistedViewMode : anonymousViewMode;
  const setViewMode = useCallback((next) => {
    if (hasEditorPreferenceOwner) setPersistedViewMode(next);
    else setAnonymousViewMode(next);
  }, [hasEditorPreferenceOwner, setPersistedViewMode]); // "basic" | "advanced"
  const [previewDockOpen, setPreviewDockOpen] = useState(false);
  // 2026-05-25 Studio Console — Modo enfoque. Toggle persistente que
  // agranda max-h de la lista + MAX_VH del timeline. Operador con 30-50
  // segments por video estaba scrolleando constante. localStorage usa
  // string "1"/"0" (el hook es string-only).
  const [focusModeRaw, setFocusModeRaw] = useLocalStorage("genly_editor_focus", "0");
  const focusMode = focusModeRaw === "1";
  const workspaceFocusMode = focusMode || (viewMode === "advanced" && hideTypographyControls);
  const toggleFocusMode = useCallback(
    () => setFocusModeRaw((v) => (v === "1" ? "0" : "1")),
    [setFocusModeRaw],
  );
  // 2026-05-26 — fix #357 follow-up. Cuando el editor se mueve dentro del
  // wizard de 3 columnas (UploadZone:1861), el max-h interno apenas crece
  // ~90px adentro de una columna capada a 460px de ancho — el "enfoque" era
  // imperceptible. Ahora emitimos una clase global en <body> para que
  // UploadZone pueda colapsar el grid a 1-col y esconder stepper + preview
  // central via variantes arbitrarias de Tailwind. Lifting el state hacia
  // UploadZone vía render prop era más invasivo (reviewScreen es una IIFE
  // que recrearía en cada toggle); body class es 1-way data flow simple y
  // se limpia solo en unmount cuando el operador navega a otro paso.
  useEffect(() => {
    if (typeof document === "undefined") return undefined;
    document.body.classList.toggle("editor-focus-mode", workspaceFocusMode);
    return () => document.body.classList.remove("editor-focus-mode");
  }, [workspaceFocusMode]);
  // El auto-fix dejó de ser un card/pill propio (2026-07 rediseño): ahora
  // es el chip ghost "Aplicar corrección · N" con popover en la fila de
  // chips, así que ya no hace falta un estado de expand/collapse propio.
  // Layout edits in the preview apply to ALL lines by default (consistent
  // look across the song); "line" scopes the next edit to the selected line
  // only (for the odd tilted/repositioned line).
  const [layoutScope, setLayoutScope] = useState("all"); // "all" | "line"
  // Live font selection (preview re-renders instantly; emitted to parent
  // for the actual render). Seeded from the job's current font.
  const [selectedFont, setSelectedFont] = useState(font || "");
  const [selectedCase, setSelectedCase] = useState(textCase || "upper");
  const [selectedContrast, setSelectedContrast] = useState(textContrast || "medium");
  // 2026-05-23: nuevos ejes (paridad con el wizard, ver header del archivo).
  const [selectedAnimation, setSelectedAnimation] = useState(lyricsAnimation || "none");
  const [selectedLineTransition, setSelectedLineTransition] = useState(lineTransition || "none");
  // Phase 2 (2026-05-25): sync props → state cuando el wizard controla los
  // typography settings desde el paso 4. Sin esto, el editor montado en paso 6
  // se queda con el seed inicial y no refleja los cambios que el operador
  // hace en el stepper. Solo activo en modo wizard para no romper el flow
  // standalone donde el editor ES la fuente de verdad.
  useEffect(() => {
    if (!hideTypographyControls) return;
    setSelectedFont(font || "");
    setSelectedCase(textCase || "upper");
    setSelectedContrast(textContrast || "medium");
    setSelectedAnimation(lyricsAnimation || "none");
    setSelectedLineTransition(lineTransition || "none");
  }, [hideTypographyControls, font, textCase, textContrast, lyricsAnimation, lineTransition]);
  // Autosave confidence for the timeline view. saveStatus drives the
  // "Guardando…/Guardado ✓" chip; flushCounter triggers an immediate save
  // on a timeline drag (instead of waiting for the 3 s debounce).
  // QA fix 2026-05-28 (audit P0 #74): extendido con "error". Antes el
  // debounced autosave NO actualizaba saveStatus (solo el flush-on-drag
  // lo hacía), y errores de red caían silencioso. Operador veía
  // "Guardado ✓" del último drag aunque el debounced autosave de un
  // text-edit posterior haya fallado → al apretar Aprobar perdía
  // changes. Ahora ambos autosaves actualizan saveStatus, y "error"
  // se muestra como chip rojo con botón Reintentar.
  const [saveStatus, setSaveStatus] = useState("idle"); // idle|local|saving|saved|offline|conflict|error
  // Motivo del último fallo de respaldo, para que el banner + el confirm de
  // "Aprobar" digan la CAUSA REAL en vez de "problema de red" siempre (el
  // copy honesto de PR A quedó hardcodeado a "red"; la causa real puede ser
  // sesión vencida, job expirado, etc. — ver _SAVE_ERROR_COPY). null cuando
  // no hay error. Se deriva de result.reason/status de persistSegments.
  const [saveErrorReason, setSaveErrorReason] = useState(null);
  const [flushCounter, setFlushCounter] = useState(0);
  const [durableHydrated, setDurableHydrated] = useState(false);
  const [conflictDialogOpen, setConflictDialogOpen] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const editedRef = useRef(edited);
  editedRef.current = edited;
  const persistRef = useRef(onPersistSegments);
  persistRef.current = onPersistSegments;
  const saveQueueRef = useRef(null);
  if (!saveQueueRef.current) {
    saveQueueRef.current = saveQueue || createSaveQueue(
      (jobId, value, opts) => persistRef.current?.(jobId, value, opts),
      { categorize: _saveErrorCategory },
    );
  }
  const editorV2Enabled = Boolean(
    user?.features?.editor_v2 && transcribeJobId && editorRequest && !disableAutosave,
  );
  const durableEditor = useEditorDocument({
    jobId: transcribeJobId,
    enabled: editorV2Enabled,
    request: editorRequest,
  });
  const draftOwner = user?.id || user?.username || null;
  const draftTenant = user?.tenant_id || user?.billing_account_id || "workspace";
  const draftKey = transcribeJobId && draftOwner
    ? `${editorV2Enabled ? "genly_editor_draft" : "genly_segments_draft"}:${draftTenant}:${draftOwner}:${transcribeJobId}`
    : null;
  const editorSessionIdRef = useRef(null);
  if (!editorSessionIdRef.current) {
    editorSessionIdRef.current = globalThis.crypto?.randomUUID?.()
      || `editor-${Date.now()}-${Math.random().toString(36).slice(2)}`;
  }
  const trackEditorEvent = useCallback((name, properties = {}) => {
    if (!editorRequest || !transcribeJobId) return;
    editorRequest("/analytics/events", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ events: [{
        name,
        job_id: transcribeJobId,
        properties: { ...properties, session_id: editorSessionIdRef.current },
      }] }),
    }).catch(() => {});
  }, [editorRequest, transcribeJobId]);
  const openedEventJobRef = useRef(null);
  useEffect(() => {
    if (!transcribeJobId || openedEventJobRef.current === transcribeJobId) return;
    if (editorV2Enabled && !durableHydrated) return;
    openedEventJobRef.current = transcribeJobId;
    trackEditorEvent("editor_opened", {
      line_count: editedRef.current.length,
      view: viewMode,
      source: editorV2Enabled ? "editor_v2" : "legacy",
    });
  }, [durableHydrated, editorV2Enabled, trackEditorEvent, transcribeJobId, viewMode]);
  const previousViewRef = useRef(viewMode);
  useEffect(() => {
    if (previousViewRef.current === viewMode) return;
    trackEditorEvent("editor_view_changed", { from: previousViewRef.current, to: viewMode });
    previousViewRef.current = viewMode;
  }, [trackEditorEvent, viewMode]);
  // Snapshot of the timings as first handed to us — the baseline for the
  // timeline's "Resetear timings". PR E + F2 fix: se lee del `original` del
  // store (getOriginal), que es la baseline del PRIMER seed y NUNCA se pisa
  // con edits/replace. En un remount, `edited` ya trae los timings EDITADOS
  // de la entrada viva, así que sembrar el ref con `edited` hacía que Reset
  // restaurara las filas a sí mismas (no-op). getOriginal preserva el
  // original real; fallback a `edited` para el path jobId-less/local (donde
  // el primer mount `original` === `edited` de todos modos).
  const originalSegmentsRef = useRef(segmentsStore.getOriginal(_storeKey) ?? edited);
  // Operator feedback 2026-05-25 (UMG): "Debería hacerlo solo, no
  // preguntarme" — the auto-trim banner ("Recortar N líneas con texto
  // colgado · Aplicar") was friction. Detection is reliable enough to
  // apply silently on initial load. The ref tracks per-segments-prop
  // application so re-seeding a new job re-triggers; routine edits
  // (typing in a line) do NOT, because they don't change the ref.
  const autoTrimAppliedRef = useRef(false);
  // PR E (2026-07): acá vivía el effect de prop-sync/reseed (Bug B7 + los
  // guards de eco #724/live-edit + el detector [reseed-storm]). Se ELIMINÓ
  // entero: el estado vive en segmentsStore (sobrevive unmounts, el prop
  // `segments` es solo seed inicial) y el reemplazo externo post-mount va
  // por segmentsStore.replace(jobId, segs), que preserva _id vía
  // reseedPreservingIds. El canary anti-loop vive ahora en el store
  // (mismo tag "[reseed-storm]" → Sentry vía observability.js).

  // Warn browser on tab-close / external navigation when there are unsaved edits.
  // disableBeforeUnload skips this for the post-approval modal — closing
  // the modal already IS the explicit "discard" gesture, a native confirm
  // on top is noise.
  useEffect(() => {
    if (disableBeforeUnload || !isDirty) return;
    const handler = (e) => { e.preventDefault(); e.returnValue = ""; };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [isDirty, disableBeforeUnload]);

  const handleDurableStatus = useCallback((status, reason, metadata = {}) => {
    setSaveStatus(status);
    setSaveErrorReason(reason);
    if (status === "saved") {
      trackEditorEvent("editor_autosave_success", {
        duration_ms: Math.round(metadata.durationMs || 0),
        checkpoint: metadata.checkpoint || "draft",
      });
    } else if (["offline", "error"].includes(status)) {
      trackEditorEvent("editor_autosave_failed", {
        checkpoint: metadata.checkpoint || "draft",
        reason: reason || status,
      });
    }
    if (status === "saved" && draftKey) {
      try { localStorage.removeItem(draftKey); } catch { /* storage blocked */ }
    }
  }, [draftKey, trackEditorEvent]);

  const durableSegments = useMemo(
    () => sanitizeSegmentsForPersistence(edited),
    [edited],
  );
  const { flush: flushDurableSave } = useEditorAutosave({
    enabled: editorV2Enabled && durableHydrated && !durableEditor.loading,
    segments: durableSegments,
    dirty: isDirty,
    blocked: Boolean(durableEditor.conflict),
    save: durableEditor.save,
    reconcile: durableEditor.reconcile,
    onStatus: handleDurableStatus,
  });

  // Never let an operator type against the legacy seed while the durable
  // revision is still unknown.  Besides preventing a false base-revision=0
  // conflict, this replaces the previously silent disabled approval CTA
  // with an explicit loading/retry state.
  const editorInitializationBlocked = editorV2Enabled && !durableHydrated;

  // Hydrate only after comparing the local draft against the durable server
  // revision. A stale/malformed draft is never silently submitted.
  const durableHydratedJobRef = useRef(null);
  useEffect(() => {
    if (!editorV2Enabled || durableEditor.loading || !durableEditor.document) return;
    if (durableHydratedJobRef.current === transcribeJobId) return;
    durableHydratedJobRef.current = transcribeJobId;
    let cancelled = false;
    const hydrate = async () => {
      const remote = sanitizeSegments(durableEditor.document.segments || []);
      const remoteOriginal = sanitizeSegments(durableEditor.document.original_segments || remote);
      originalSegmentsRef.current = remoteOriginal;
      let next = remote;
      let markDirty = false;
      if (draftKey) {
        try {
          const raw = localStorage.getItem(draftKey);
          if (raw) {
            const draft = JSON.parse(raw);
            if (!Array.isArray(draft?.segments) || !draft.segments.length) {
              setSaveStatus("error");
              setSaveErrorReason("draft-corrupt");
            } else {
              const local = sanitizeSegments(draft.segments);
              // A local draft can outlive a background/typography edit. Those
              // operations may advance the document revision or refresh
              // renderer metadata without changing any operator-owned lyric
              // content. Comparing raw JSON here made that harmless case look
              // like a collaboration conflict as soon as the editor reopened.
              const sameContent = segmentsEquivalent(local, remote);
              if (sameContent) {
                localStorage.removeItem(draftKey);
              } else if (Number.isInteger(draft.base_revision)
                && draft.base_revision === durableEditor.document.revision) {
                next = local;
                markDirty = true;
                setSaveStatus("local");
              } else {
                let baseSegments = Array.isArray(draft.base_segments)
                  ? sanitizeSegments(draft.base_segments)
                  : null;

                // Drafts created before base_segments was introduced still
                // carry base_revision. Recover that immutable base from the
                // durable version history instead of manufacturing an
                // "another tab" conflict for a legacy local draft.
                if (!baseSegments && Number.isInteger(draft.base_revision)) {
                  if (draft.base_revision === 0) {
                    baseSegments = remoteOriginal;
                  } else if (editorRequest) {
                    try {
                      const summariesResponse = await editorRequest(
                        `/editor/${transcribeJobId}/versions?limit=50`,
                      );
                      const summaries = summariesResponse.ok
                        ? (await summariesResponse.clone().json())?.versions || []
                        : [];
                      const baseVersion = summaries.find(
                        (version) => version.revision === draft.base_revision,
                      );
                      if (baseVersion?.id) {
                        const versionResponse = await editorRequest(
                          `/editor/${transcribeJobId}/versions/${encodeURIComponent(baseVersion.id)}`,
                        );
                        if (versionResponse.ok) {
                          const version = await versionResponse.clone().json();
                          if (Array.isArray(version?.segments)) {
                            baseSegments = sanitizeSegments(version.segments);
                          }
                        }
                      }
                    } catch { /* fall back to the explicit safe resolver */ }
                  }
                }
                if (cancelled) return;
                const merged = baseSegments
                  ? mergeThreeWay(baseSegments, local, remote)
                  : { merged: [], conflicts: [{ key: "unknown-base" }] };
                if (merged.conflicts.length === 0) {
                  next = merged.merged;
                  markDirty = !segmentsEquivalent(next, remote);
                  if (markDirty) setSaveStatus("local");
                  else localStorage.removeItem(draftKey);
                } else {
                  next = local;
                  markDirty = true;
                  durableEditor.stageConflict(local, {
                    baseSegments,
                    reason: Number.isInteger(draft.base_revision) ? "stale-draft" : "unversioned-draft",
                  });
                  setSaveStatus("conflict");
                  setSaveErrorReason("conflict");
                  setConflictDialogOpen(true);
                }
              }
            }
          }
        } catch {
          setSaveStatus("error");
          setSaveErrorReason("draft-corrupt");
        }
      }
      if (cancelled) return;
      setEdited(reseedPreservingIds(editedRef.current, next));
      setIsDirty(markDirty);
      setDurableHydrated(true);
    };
    hydrate();
    return () => { cancelled = true; };
  }, [draftKey, durableEditor.document, durableEditor.loading, durableEditor.stageConflict,
    editorRequest, editorV2Enabled, setEdited, transcribeJobId]);

  useEffect(() => {
    if (!durableEditor.conflict) return;
    setSaveStatus("conflict");
    setSaveErrorReason("conflict");
    setConflictDialogOpen(true);
    trackEditorEvent("editor_conflict", {
      server_revision: durableEditor.conflict.serverRevision,
      local_revision: durableEditor.revisionRef.current,
    });
  }, [durableEditor.conflict, durableEditor.revisionRef, trackEditorEvent]);

  // Debounced autosave to backend: every 3s after the last edit, persist
  // the current segments to /jobs/{id}/save-segments. This bumps the
  // reaper's last_user_activity_at anchor so long edit sessions don't get
  // barre at the 30-min TTL (incident 2026-05-14 — Agus batch-edited 5
  // lyrics for 90 min and all 5 jobs got reaped before "Crear videos").
  // No-op when the parent didn't wire the callback (e.g. unit tests).
  //
  // QA fix 2026-05-28 (audit P0 #74): await el resultado de
  // onPersistSegments y actualizá saveStatus. Antes el debounced fire-
  // and-forget tragaba errores. Ahora si la red cae o el backend
  // rechaza, saveStatus pasa a "error" y el chip rojo + bloqueo del
  // botón Aprobar se activan.
  // Auditoría 2026-06-10 ("hay partes que no se graban"): el cleanup del
  // debounce cancelaba el guardado pendiente SIN flush. Salir del step 6
  // del wizard DESMONTA este componente — los últimos <3s de anclas/drags
  // morían con él, y al volver el remount sembraba desde datos viejos.
  // `_pendingFlushRef` guarda el estado más fresco aún-no-persistido; el
  // effect de unmount (deps vacías, abajo) lo dispara fire-and-forget.
  useEffect(() => {
    if (disableAutosave || editorV2Enabled) return undefined;
    if (!onPersistSegments || !transcribeJobId) return undefined;
    if (!Array.isArray(edited) || edited.length === 0) return undefined;
    const queue = saveQueueRef.current;
    queue.prime(transcribeJobId, Number.isInteger(segmentsRevision) ? segmentsRevision : 0);
    queue.schedule(transcribeJobId, () =>
      sanitizeSegmentsForPersistence(editedRef.current));
    return undefined;
  }, [edited, transcribeJobId, onPersistSegments, disableAutosave, segmentsRevision, editorV2Enabled]);

  // The queue is the single status source for debounce, drag, retry and
  // navigation flushes.  A trailing snapshot is coalesced while a request is
  // in flight, and the returned Promise resolves only when the job drains.
  useEffect(() => {
    if (disableAutosave || editorV2Enabled || !onPersistSegments || !transcribeJobId) return undefined;
    const queue = saveQueueRef.current;
    queue.prime(transcribeJobId, Number.isInteger(segmentsRevision) ? segmentsRevision : 0);
    const unsubscribe = queue.subscribe(transcribeJobId, ({ status, reason }) => {
      setSaveStatus(status);
      setSaveErrorReason(reason);
      if (status === "saved" && draftKey) {
        try { localStorage.removeItem(draftKey); } catch { /* storage blocked */ }
      }
    });
    return unsubscribe;
  }, [disableAutosave, editorV2Enabled, onPersistSegments, transcribeJobId, segmentsRevision, draftKey]);

  // Keep a per-user, per-job recoverable local draft.  This is independent
  // from fetch keepalive: browsers cap keepalive request bodies and may kill
  // the page before the network settles.
  const draftRestoredRef = useRef(false);
  const dirtyRef = useRef(isDirty);
  dirtyRef.current = isDirty;
  useEffect(() => {
    if (editorV2Enabled || !draftKey || draftRestoredRef.current) return;
    draftRestoredRef.current = true;
    try {
      const saved = JSON.parse(localStorage.getItem(draftKey) || "null");
      if (Array.isArray(saved?.segments) && saved.segments.length) {
        setEdited(reseedPreservingIds(editedRef.current, sanitizeSegments(saved.segments)));
        setIsDirty(true);
      }
    } catch { /* corrupt or unavailable storage */ }
  }, [draftKey, editorV2Enabled, setEdited]);

  useEffect(() => {
    if (!draftKey || !isDirty) return;
    try {
      const cleaned = sanitizeSegmentsForPersistence(edited);
      localStorage.setItem(draftKey, JSON.stringify({
        segments: cleaned,
        base_segments: editorV2Enabled
          ? sanitizeSegmentsForPersistence(durableEditor.document?.segments || [])
          : undefined,
        base_revision: editorV2Enabled
          ? durableEditor.revisionRef.current
          : (saveQueueRef.current._peek(transcribeJobId)?.revision || 0),
        updated_at: new Date().toISOString(),
      }));
    } catch { /* quota/storage blocked; network autosave still runs */ }
  }, [draftKey, edited, editorV2Enabled, isDirty, transcribeJobId, durableEditor.revisionRef]);

  // SPA navigation unmounts React before pagehide. Flush only when a
  // debounce/trailing snapshot is actually pending; a completed save must not
  // be duplicated merely because isDirty remains true until approval.
  useEffect(() => () => {
    if (disableAutosave || editorV2Enabled || !onPersistSegments || !transcribeJobId) return;
    const queue = saveQueueRef.current;
    const state = queue._peek(transcribeJobId);
    if (!dirtyRef.current || (!state?.debounceTimer && !state?.pending)) return;
    queue.flush(transcribeJobId, {
      provider: () => sanitizeSegmentsForPersistence(editedRef.current),
    });
  }, [disableAutosave, editorV2Enabled, onPersistSegments, transcribeJobId]);

  // Durable flush on page unload (refresh / tab close). The beforeunload
  // handler above only WARNS via a native dialog — it does not persist. And
  // the flush-on-unmount above runs on React unmount (SPA navigation), which
  // a hard refresh (F5) skips: the browser tears down the JS context before
  // the 3s debounce or the unmount cleanup can finish, and an ordinary fetch
  // is canceled mid-flight. So on pagehide/beforeunload we re-fire the pending
  // save with `keepalive: true`, which the browser is required to deliver even
  // as the page goes away. Auth headers ride along (authFetch adds them) —
  // navigator.sendBeacon can't set Authorization, so /save-segments would 401.
  // (Reporte Gaby 2026-06-24: el editor titiló, refrescó para salir y perdió
  // TODO el trabajo no persistido.)
  useEffect(() => {
    if (disableAutosave || editorV2Enabled || !onPersistSegments || !transcribeJobId) return undefined;
    const flushOnUnload = () => {
      try {
        const cleaned = sanitizeSegmentsForPersistence(editedRef.current);
        // useEffect puede no alcanzar a correr entre el último keystroke y
        // pagehide. Persistimos sincrónicamente acá antes de cualquier red;
        // si hay un CAS en vuelo, esta es la recuperación canónica al volver.
        if (draftKey) {
          try {
            localStorage.setItem(draftKey, JSON.stringify({
              segments: cleaned,
              base_segments: editorV2Enabled
                ? sanitizeSegmentsForPersistence(durableEditor.document?.segments || [])
                : undefined,
              base_revision: saveQueueRef.current._peek(transcribeJobId)?.revision || 0,
              updated_at: new Date().toISOString(),
            }));
          } catch { /* storage blocked/quota: keepalive still must run */ }
        }
        const bytes = new Blob([JSON.stringify({ segments: cleaned })]).size;
        // Chromium/WebKit keepalive budget is about 64 KiB. Leave headroom
        // for the OCC field and headers; large drafts remain in localStorage.
        if (bytes <= 60 * 1024) {
          saveQueueRef.current.flush(transcribeJobId, {
            keepalive: true,
            provider: () => cleaned,
          });
        }
      } catch { /* best-effort */ }
    };
    window.addEventListener("pagehide", flushOnUnload);
    window.addEventListener("beforeunload", flushOnUnload);
    return () => {
      window.removeEventListener("pagehide", flushOnUnload);
      window.removeEventListener("beforeunload", flushOnUnload);
    };
  }, [disableAutosave, editorV2Enabled, onPersistSegments, transcribeJobId, draftKey]);

  // Flush-save on a timeline drag (no 3 s wait) + drive the "Guardado ✓"
  // chip. Runs only when flushCounter bumps. By the time this effect fires,
  // setEdited from the same handler has already applied, so `edited` is the
  // post-drag value. Idempotent vs the debounced autosave above.
  useEffect(() => {
    if (flushCounter === 0) return undefined;
    if (editorV2Enabled) {
      flushDurableSave("manual");
      return undefined;
    }
    if (disableAutosave || !onPersistSegments || !transcribeJobId) return undefined;
    saveQueueRef.current.flush(transcribeJobId, {
      provider: () => sanitizeSegmentsForPersistence(editedRef.current),
    });
    return undefined;
    // Only react to the flush trigger — `edited` is intentionally read fresh
    // but NOT a dep (we don't want every keystroke to flush).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [flushCounter, editorV2Enabled, flushDurableSave]);

  const flushPendingSave = useCallback((persistOpts = null, force = false) => {
    if (editorV2Enabled) return flushDurableSave("manual");
    if ((!force && disableAutosave) || !onPersistSegments || !transcribeJobId) {
      return Promise.resolve({ ok: true, skipped: true });
    }
    return saveQueueRef.current.flush(transcribeJobId, {
      provider: () => sanitizeSegmentsForPersistence(editedRef.current),
      persistOpts,
    });
  }, [disableAutosave, editorV2Enabled, flushDurableSave, onPersistSegments, transcribeJobId]);

  // PR E (2026-07): acá vivía el espejo sincrónico por keystroke
  // (onEditedChange → App.setCurrentReview). Eliminado — era la mitad del
  // loop bidireccional del reseed-storm. Los consumidores externos leen
  // ahora directo del segmentsStore (useJobSegmentsValue / get).

  // NOTE: a second debounced-autosave useEffect lived here, copy-pasted
  // identically to the one above (line ~238). Removed 2026-05-18 —
  // the duplicate (a) did not respect `disableAutosave`, and (b) raced
  // its partner on every `edited` change, firing two POSTs in parallel
  // every 3 s. If two edits landed inside the same debounce window the
  // second response could overwrite the first with a stale payload.
  // Agus reported edits not persisting after SPACE anchors; the race
  // was the likely culprit. Keep the single autosave above.

  // ─── Audio sync ─────────────────────────────────────────────────────
  // Blob URL lifecycle must live in useEffect, not useMemo. useMemo is
  // not a lifecycle hook and React 18 StrictMode double-invokes its
  // callback in dev, leaking one URL per mount. More importantly, pairing
  // a useMemo-created URL with a useEffect cleanup keyed on [audioUrl]
  // causes StrictMode's simulated unmount to revoke the URL while the
  // <audio> element in the DOM still references it — playback dies a few
  // seconds in once the initial buffered range is consumed.
  const [blobAudioUrl, setBlobAudioUrl] = useState(null);

  // Blob URL lifecycle — only re-runs when audioFile changes.
  useEffect(() => {
    if (!audioFile) { setBlobAudioUrl(null); return undefined; }
    // HOTFIX 2026-05-29: when a wizard session is resumed from
    // sessionStorage (wizardPersistence), `audioFile` is a STUB object
    // — { name, size, type, lastModified, _restoredStub: true } — not
    // a real Blob/File. URL.createObjectURL on a non-Blob throws
    // "Failed to execute 'createObjectURL' on 'URL': Overload
    // resolution failed". Detect the stub and silently skip; segment
    // editing still works, audio playback is disabled until re-upload.
    const isRealBlob =
      typeof Blob !== "undefined" && audioFile instanceof Blob;
    if (!isRealBlob || audioFile._restoredStub) {
      setBlobAudioUrl(null);
      return undefined;
    }
    const url = URL.createObjectURL(audioFile);
    setBlobAudioUrl(url);
    return () => {
      URL.revokeObjectURL(url);
    };
  }, [audioFile]);

  // The durable R2 URL is authoritative as soon as it reaches this render.
  // Keeping it declarative avoids the prop -> effect -> local-state gap that
  // could leave an edit session stuck in the no-audio state. The blob remains
  // a valid fallback and is revoked only when its own lifecycle ends.
  const audioUrl = audioUrlProp || blobAudioUrl;

  const audioRef = useRef(null);
  const listRef = useRef(null);
  const rowRefs = useRef({});
  const rafRef = useRef(null);
  const playbackTimeRef = useRef(0);
  const lastPublishedTimeRef = useRef(-Infinity);
  const lastPublishedActiveIdRef = useRef(null);

  const [isPlaying, setIsPlaying] = useState(false);
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [audioError, setAudioError] = useState(false);

  useEffect(() => {
    setAudioError(false);
  }, [audioUrl]);

  // INCIDENT (mobile 2026-05-24): the audio element's `onTimeUpdate`
  // event fires every ~250 ms on most browsers (sometimes slower on
  // mobile in background tabs). Lines shorter than that — short
  // interjections ("oh!", "yeah"), rapid-fire hip-hop / reggaetón
  // syllables, or any sub-250ms segment from forced alignment — were
  // SKIPPED in the preview: `currentTime` jumped from before the line's
  // start straight past its end, and `segments.find(s => t >= s.start
  // && t < s.end)` returned null for the whole duration.
  //
  // Keep the audio clock at display cadence without rendering the complete
  // editor at 60 fps. The timeline reads playbackTimeRef directly for its
  // compositor-only playhead; React state is published at 20 fps or whenever
  // the active lyric changes, so short interjections are still not skipped.
  // `onTimeUpdate` is still wired as a fallback for seeks and the
  // initial idle state (before the user hits play).
  useEffect(() => {
    if (!isPlaying) return undefined;
    const tick = () => {
      const a = audioRef.current;
      if (a && !a.paused) {
        const ct = a.currentTime;
        playbackTimeRef.current = ct;
        let active = null;
        for (const s of sanitizedEdited) {
          if (ct >= s.start && ct < s.end) { active = s; break; }
          if (ct >= s.start) active = s;
        }
        const activeKey = active?._id ?? null;
        if (activeKey !== lastPublishedActiveIdRef.current
          || Math.abs(ct - lastPublishedTimeRef.current) >= 0.05) {
          lastPublishedTimeRef.current = ct;
          lastPublishedActiveIdRef.current = activeKey;
          setCurrentTime(ct);
        }
        // Phase C 2026-05-25: publish playback tick al padre. Buscar el
        // segment activo aquí (no usar activeId del scope porque cambia
        // con setState async). Si el padre pasó onPlaybackTick, le
        // notificamos el segmento activo + currentTime para que el
        // WizardLivePreview central pueda hacer word-jump real.
        if (onPlaybackTick) {
          if (active) {
            onPlaybackTick(active.text || "", active.start, active.end, ct, active.words);
          } else {
            onPlaybackTick("", 0, 0, ct);
          }
        }
      }
      rafRef.current = requestAnimationFrame(tick);
    };
    rafRef.current = requestAnimationFrame(tick);
    return () => {
      if (rafRef.current) cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isPlaying, sanitizedEdited, onPlaybackTick]);
  const [wrapWarning, setWrapWarning] = useState(null); // {ids: [...]} for 3+ line segs
  const [focusedSegId, setFocusedSegId] = useState(null); // for preview panel

  // Inline timestamp edit state. Only one row can be in edit mode at a
  // time; clicking a different row's timestamp swaps the active editor.
  // Single-click on a timestamp seeks; double-click switches to edit.
  const [editingId, setEditingId] = useState(null);
  const [editValue, setEditValue] = useState("");
  // Repeat-line propagation. `textEditStart` snapshots {id, text} when the
  // operator focuses a line's text input, so on blur we can compare against
  // the pre-edit text and find other lines that were identical to it.
  // `propagationPrompt` holds {id, newText, matchIds, prevText} while we ask
  // "apply this change to the N other identical lines?".
  const [textEditStart, setTextEditStart] = useState(null);
  const [propagationPrompt, setPropagationPrompt] = useState(null);

  // Tap-to-sync mode — operator hits Space (or button) while audio
  // plays to anchor each line at the current playback time. Solves
  // the generic case where timestamps are stretched, compressed, or
  // offset arbitrarily — listening + tapping is ground truth.
  const [syncMode, setSyncMode] = useState(false);
  const [syncCursor, setSyncCursor] = useState(0);
  // Set of segment _ids that were anchored in the last 10s — used to
  // render a "recently moved" ring + per-row undo button so the operator
  // can see what just moved (the chronological re-sort can be visually
  // confusing, see tapAnchor comments). Each id is auto-removed by a
  // 10s setTimeout scheduled at anchor time.
  const [highlightedIds, setHighlightedIds] = useState(() => new Set());
  // Navegador secuencial de líneas "review" (banner "Revisar →"): cursor
  // sobre el orden actual de review-ids + un flash breve al aterrizar en
  // una fila para que el operador la ubique sin cazar colores.
  const reviewNavIdxRef = useRef(-1);
  const [flashReviewId, setFlashReviewId] = useState(null);
  // Rediseño de controles (2026-07, spec diseño SaaS senior): 6 banners
  // apilados → 2 filas. Estados de los nuevos affordances plegables.
  const [videoSettingsOpen, setVideoSettingsOpen] = useState(false); // disclosure "Ajustes del video"
  const [fixPopoverOpen, setFixPopoverOpen] = useState(false);       // popover del chip "Aplicar corrección"
  const [overflowOpen, setOverflowOpen] = useState(false);           // menú ⋯ del player bar
  // Toast for per-anchor confirmation feedback.
  const { toast } = useToast();
  // Global timing offset panel — UX entry point for "the whole song is
  // shifted by N ms" cases. Different from Sync Mode (which anchors a
  // line + propagates) and the "intro is too long" banner (which only
  // appears when first.start > 3 s). This panel is always available
  // and lets the operator nudge every line by ±1 s with a slider or
  // ±125/250/500 ms presets. Collapsed by default to keep the editor
  // tidy.
  const [shiftPanelOpen, setShiftPanelOpen] = useState(false);
  const [shiftDraftMs, setShiftDraftMs] = useState(0); // -1000..+1000
  // After applying a shift the slider resets to 0 so the next draft
  // starts clean. Without a confirmation chip the operator can't tell
  // whether the click landed — they see the preset highlight clear and
  // assume nothing happened, then re-apply, doubling the shift.
  // appliedShiftMs holds the last applied delta for ~2.5s purely as
  // visual receipt.
  const [appliedShiftMs, setAppliedShiftMs] = useState(null);
  useEffect(() => {
    if (appliedShiftMs == null) return undefined;
    const id = setTimeout(() => setAppliedShiftMs(null), 2500);
    return () => clearTimeout(id);
  }, [appliedShiftMs]);
  // When false (default), each Sync-Mode tap anchors ONLY the current
  // line — leaves every following timestamp alone. When true, the same
  // delta propagates to every line after the cursor (the previous-only
  // behaviour, useful when the whole timeline is uniformly off).
  // Operators reported that the cascading default was destroying their
  // already-correct lines when they only wanted to fix a single anchor;
  // the safer default is single-line.
  const [syncCascade, setSyncCascade] = useState(false);
  // Stack of {id, prevStart, prevEnd} so "Deshacer" can revert the
  // last tap if the operator overshot.
  const [syncHistory, setSyncHistory] = useState([]);

  // Manual-edit history. Each entry is the FULL `edited` snapshot taken
  // BEFORE a mutation lands (single-line timestamp tweak, suggestion
  // application, intro trim, etc.). Capped at 50 entries — that's enough
  // to walk back through a normal review session without bloating React
  // state. Cmd/Ctrl+Z pops one and replays it onto setEdited.
  const [editHistory, setEditHistory] = useState([]);
  const pushEditHistory = useCallback(() => {
    setIsDirty(true);
    setEditHistory((prev) => {
      const next = [...prev, edited];
      return next.length > 50 ? next.slice(next.length - 50) : next;
    });
  }, [edited]);
  const undoEdit = useCallback(() => {
    setEditHistory((prev) => {
      if (!prev.length) return prev;
      const snapshot = prev[prev.length - 1];
      setEdited(snapshot);
      trackEditorEvent("editor_undo", { operation: "edit", count: 1 });
      return prev.slice(0, -1);
    });
  }, [trackEditorEvent]);

  // ─── Visual timeline (Timings view) handlers ────────────────────────
  // A drag commits here. We stamp `locked: true` so the render
  // (pipeline._apply_display_timing) respects this manual end instead of
  // auto-extending it (hold-until-next). The undo snapshot is pushed by the
  // timeline on pointerdown (onDragStart), so this only mutates `edited`.
  const handleTimelineTimingChange = useCallback((id, newStart, newEnd) => {
    setIsDirty(true);
    setEdited((prev) => prev.map((s) =>
      s._id === id ? { ...s, start: newStart, end: newEnd, locked: true } : s
    ));
    setHighlightedIds((prev) => {
      const next = new Set(prev);
      next.add(id);
      return next;
    });
    setTimeout(() => setHighlightedIds((prev) => {
      const next = new Set(prev);
      next.delete(id);
      return next;
    }), 10000);
    // Flush-save immediately (don't wait for the 3 s autosave debounce) so
    // the operator sees "Guardado" right after dropping a block — the
    // flush effect below reads the just-updated `edited` and persists.
    setFlushCounter((c) => c + 1);
    trackEditorEvent("editor_timing_changed", { count: 1, operation: "resize_or_move" });
  }, [trackEditorEvent]);

  const handleTimelineTimingChangeBatch = useCallback((changes, interaction = {}) => {
    if (!changes?.length) return;
    const firstOriginal = editedRef.current.find((segment) => segment._id === changes[0].id);
    const deltaMs = firstOriginal ? Math.round((changes[0].start - firstOriginal.start) * 1000) : 0;
    setIsDirty(true);
    const byId = new Map(changes.map(({ id, start, end }) => [id, { start, end }]));
    setEdited((prev) => prev.map((s) => {
      const change = byId.get(s._id);
      return change
        ? { ...s, start: change.start, end: change.end, locked: true }
        : s;
    }));
    setHighlightedIds((prev) => {
      const next = new Set(prev);
      changes.forEach(({ id }) => next.add(id));
      return next;
    });
    setTimeout(() => setHighlightedIds((prev) => {
      const next = new Set(prev);
      changes.forEach(({ id }) => next.delete(id));
      return next;
    }), 10000);
    setFlushCounter((c) => c + 1);
    trackEditorEvent("editor_group_moved", {
      count: changes.length,
      delta_ms: deltaMs,
      duration_ms: Math.round(interaction.durationMs || 0),
    });
  }, [trackEditorEvent]);

  // Per-line layout (position / size / rotation) committed from the live
  // preview. Same flush-on-commit as the timeline so "Guardado" shows fast.
  const handleLayoutChange = useCallback((id, layout) => {
    setIsDirty(true);
    setEdited((prev) => prev.map((s) =>
      (layoutScope === "all" || s._id === id)
        ? { ...s, pos: layout.pos, scale: layout.scale, rot: layout.rot }
        : s
    ));
    setFlushCounter((c) => c + 1);
  }, [layoutScope]);

  // Restore every line's timing to the original snapshot + drop all `locked`
  // flags, so the render goes back to auto hold-until-next.
  const resetTimings = useCallback(() => {
    pushEditHistory();
    const byId = new Map(sanitizeSegments(originalSegmentsRef.current || []).map((s) => [s._id, s]));
    setEdited((prev) => prev.map((s) => {
      const o = byId.get(s._id);
      if (!o) return s;
      // eslint-disable-next-line no-unused-vars
      const { locked, ...rest } = s;
      return { ...rest, start: o.start, end: o.end };
    }));
    setFlushCounter((c) => c + 1);
    toast({ message: "Timings restaurados al original", tone: "info" });
  }, [pushEditHistory, toast]);

  // Versión B, parte 2 — "Re-sincronizar con IA". Flujo:
  //   1. Flush del estado local a /save-segments (el backend re-ancla lo
  //      que hay en segments_json — sin esto, re-anclaría texto viejo si
  //      el operador tipeó hace <3s y el autosave no corrió).
  //   2. POST /jobs/{id}/reanchor vía el callback del padre.
  //   3. Éxito → reemplazar `edited` con los segments re-anclados (mismo
  //      seed que el mount; las líneas `locked` vuelven intactas del
  //      backend) + toast "N re-sincronizadas, M para revisar".
  //      Decline / error → toast de error, timings quedan como estaban.
  // El snapshot pre-reanchor va al edit history, así Cmd+Z lo revierte.
  const [reanchoring, setReanchoring] = useState(false);
  const canReanchor = !!(onReanchor && transcribeJobId
    && user?.features?.anchor_lyrics === true);
  const handleReanchor = useCallback(async () => {
    if (!onReanchor || !transcribeJobId || reanchoring) return;
    setReanchoring(true);
    try {
      let saved = null;
      if (onPersistSegments) {
        saved = await flushPendingSave(null, true);
        if (saved?.ok === false) throw new Error(saved.reason || "save-failed");
      }
      const baseRevision = Number.isInteger(saved?.revision)
        ? saved.revision
        : (Number.isInteger(segmentsRevision) ? segmentsRevision : 0);
      const res = await Promise.resolve(onReanchor(transcribeJobId, baseRevision));
      if (res && res.ok && Array.isArray(res.segments) && res.segments.length) {
        if (Number.isInteger(res.revision)) {
          saveQueueRef.current.prime(transcribeJobId, res.revision);
        }
        pushEditHistory();
        // PR E: ids frescos vía el mismo helper que el seed inicial —
        // consistencia con la identidad estable del store (PR D).
        setEdited(reseedPreservingIds([], sanitizeSegments(res.segments)));
        toast({
          message: (t("editor.reanchor_done") || "{n} líneas re-sincronizadas, {m} para revisar")
            .replace("{n}", String(res.count ?? res.segments.length))
            .replace("{m}", String(res.review_count ?? 0)),
          tone: "success",
        });
      } else {
        toast({
          message: t("editor.reanchor_failed") || "No se pudo re-sincronizar — el timing quedó como estaba.",
          tone: "error",
        });
      }
    } catch {
      toast({
        message: t("editor.reanchor_failed") || "No se pudo re-sincronizar — el timing quedó como estaba.",
        tone: "error",
      });
    } finally {
      setReanchoring(false);
    }
  }, [onReanchor, onPersistSegments, transcribeJobId, segmentsRevision, reanchoring, edited,
      pushEditHistory, toast, t, flushPendingSave]);

  const focusSegment = useCallback((id) => {
    setFocusedSegId(id);
  }, []);

  const startEditTimestamp = (seg) => {
    setEditingId(seg._id);
    setEditValue(formatTimestamp(seg.start));
  };
  const cancelEditTimestamp = () => {
    setEditingId(null);
    setEditValue("");
  };

  const commitEditTimestamp = (seg) => {
    const parsed = parseTimestamp(editValue);
    if (parsed == null) {
      // Bad input — silently revert.
      cancelEditTimestamp();
      return;
    }

    // Clamp to the window between the previous segment's end and the
    // next segment's start (in the original ordering by _id). Without
    // this, the operator can set a start past the next row's start,
    // producing overlapping segments that the renderer interprets as
    // simultaneous on-screen lines. We use the original index to find
    // neighbors so a previous edit that shifted siblings doesn't cause
    // the wrong rows to be picked up.
    const idx = edited.findIndex((s) => s._id === seg._id);
    const prevSeg = idx > 0 ? edited[idx - 1] : null;
    const nextSeg = idx >= 0 && idx < edited.length - 1 ? edited[idx + 1] : null;
    const minAllowed = prevSeg ? prevSeg.end : 0;
    const maxAllowed = nextSeg ? Math.max(minAllowed, nextSeg.start - 0.1) : (duration || parsed);
    const newStart = Math.max(minAllowed, Math.min(parsed, maxAllowed));

    // No-op edits (clamped value identical to current) shouldn't pollute
    // the undo stack — the user's Ctrl+Z would feel broken otherwise.
    if (Math.abs(newStart - seg.start) >= 1e-3) {
      pushEditHistory();
    }
    setEdited((prev) => prev.map((s) => {
      if (s._id !== seg._id) return s;
      // Preserve segment duration when the operator nudges the start
      // unless that would push end past audio_duration or the next row.
      const segDur = Math.max(0.5, s.end - s.start);
      let newEnd = newStart + segDur;
      const upperBound = nextSeg ? Math.min(nextSeg.start, duration || nextSeg.start) : duration;
      if (upperBound && newEnd > upperBound) newEnd = upperBound;
      return { ...s, start: newStart, end: newEnd };
    }));
    setEditingId(null);
    setEditValue("");
    // Manual edits never propagate to neighbouring lines. The earlier
    // behaviour offered a "shift the rest by the same delta" banner,
    // which the operator could miss (it sat above a long, scrolling
    // list) and accidentally accept — they reported a single-line
    // tweak that silently moved every following timestamp.
    // Use Sync Mode (Space + tap) when you actually want to anchor
    // a line and re-flow the rest.
  };

  // Active segment: the one whose [start, end] contains currentTime, or
  // the latest one whose start <= currentTime if no segment "owns" the
  // moment (e.g. instrumental gap).
  const activeId = useMemo(() => {
    let containing = null;
    let lastStarted = null;
    for (const seg of sanitizedEdited) {
      if (currentTime >= seg.start && currentTime < seg.end) containing = seg;
      if (currentTime >= seg.start) lastStarted = seg;
    }
    return (containing || lastStarted)?._id ?? null;
  }, [sanitizedEdited, currentTime]);

  // UI freeze / render-storm capture (P0 UMG Chile 2026-06-16). Cause-agnostic
  // safety net: if the main thread saturates (the "se queda pegado" + flicker),
  // emit a Sentry-forwarded report with the segment shape + last action so we
  // can finally diagnose the real trigger. getContext is read at report time.
  useUiStormDetector({
    active: true,
    getContext: () => {
      const segs = Array.isArray(edited) ? edited : [];
      const sorted = sanitizeSegments(segs).sort((a, b) => a.start - b.start);
      let overlaps = 0;
      let maxOverlapDepth = 0;
      for (let i = 0; i < sorted.length - 1; i++) {
        if (sorted[i].end > sorted[i + 1].start + 0.001) {
          overlaps += 1;
          let depth = 0;
          for (let j = i + 1; j < sorted.length && sorted[j].start < sorted[i].end; j++) depth += 1;
          if (depth > maxOverlapDepth) maxOverlapDepth = depth;
        }
      }
      return {
        segments: segs.length,
        overlapping_pairs: overlaps,
        max_overlap_depth: maxOverlapDepth,
        is_playing: isPlaying,
        sync_mode: syncMode,
        duration: Math.round((duration || 0) * 10) / 10,
      };
    },
  });

  // Tap handler: anchor the line at syncCursor to currentTime, then
  // propagate the same delta to every line AFTER it (the unanchored
  // ones). If the offset was constant the next line is already roughly
  // right and the operator only needs to confirm. Already-anchored
  // lines (idx < syncCursor) are ground truth and stay put.
  // Empirical compensation for the gap between `audio.currentTime` (the
  // *decoded* position, what the API reports) and what the operator
  // actually hears coming out of the speakers. Browsers buffer ~30-100 ms
  // of decoded audio before it's audible — when the operator hits Space
  // synced with their ear, currentTime has already advanced past the
  // moment they meant to anchor. 80 ms is the empirical mid-point of
  // observed latency on Chrome/Firefox/Safari with non-Bluetooth output.
  // Operators using BT headsets may need more compensation; expose later
  // as a calibration setting if reports persist.
  const AUDIO_LATENCY_COMPENSATION_S = 0.08;
  // Floor between adjacent segments so an anchor can't push a line into
  // (or before) its neighbor. 50 ms is below human flicker perception
  // but enough to keep moviepy's transitions clean.
  const MIN_GAP_S = 0.05;
  // Cascade safety net: if a single anchor would propagate a > 1.5 s
  // shift to every line after it, that's almost certainly a mistap (the
  // operator was paused, scrolled, or got distracted) — confirm before
  // walking the rest of the song into the wrong place.
  const CASCADE_DELTA_CONFIRM_S = 1.5;

  const tapAnchor = useCallback(() => {
    if (!syncMode) return;
    if (syncCursor < 0 || syncCursor >= edited.length) return;
    const target = edited[syncCursor];
    if (!target) return;

    // Compensate audio latency so the anchor matches what the operator
    // *heard* at press time, not what was decoded by then. See the
    // AUDIO_LATENCY_COMPENSATION_S comment above.
    const rawStart = Math.max(0, currentTime - AUDIO_LATENCY_COMPENSATION_S);

    // Honor the operator's intent: anchor at currentTime regardless of
    // where this line currently sits in the array. The previous version
    // clamped to `prevSeg.end + MIN_GAP_S` (where prevSeg was the line
    // at array position syncCursor-1). For the typical "fill in missing
    // chorus repetition" workflow — add line at end of array, then
    // SPACE-anchor it to mid-song — that clamp pinned the new line at
    // the END of the song instead of where the operator wanted it.
    // (Una Vez Más — Viejas Locas, agus.cafisi 2026-05-18, bug B4.)
    //
    // Trade-off: timeline can momentarily be non-monotonic between
    // tapAnchor and the post-mutation sort below. Render iterates
    // `edited` (which gets sorted right after this setEdited), so the
    // operator sees the line move to its new chronological slot.
    // syncCursor advances by _id, not array index, so the next SPACE
    // press lands on the line that was visually next BEFORE the move.
    const upperBound = duration && duration > 0 ? duration : Infinity;
    const newStart = Math.max(0, Math.min(rawStart, upperBound));

    const delta = newStart - target.start;

    // Cascade safety: huge delta = probable mistap. Ask before walking
    // every subsequent line by the same amount. Bail if operator cancels —
    // the current line gets the anchor but the cascade is skipped.
    let applyCascade = syncCascade;
    if (applyCascade && Math.abs(delta) > CASCADE_DELTA_CONFIRM_S) {
      const tail = edited.length - syncCursor - 1;
      const ok = window.confirm(
        `Detectamos un salto de ${delta.toFixed(2)}s en este anchor. ` +
        `¿Aplicar a las ${tail} líneas siguientes? ` +
        `(Cancelar = solo anclar la línea actual.)`
      );
      if (!ok) applyCascade = false;
    }
    const appliedDelta = applyCascade
      ? clampBlockShiftDelta(edited.slice(syncCursor), delta, duration)
      : delta;
    const appliedStart = applyCascade
      ? target.start + appliedDelta
      : newStart;

    // Snapshot the future lines BEFORE mutating so undo can restore
    // every shifted timestamp, not just the anchor's. Skip when we're
    // not going to touch the future — keeps the undo behaviour matching
    // the user's mental model (single line revert).
    const futureSnapshot = applyCascade
      ? edited
          .slice(syncCursor + 1)
          .map((s) => ({ id: s._id, prevStart: s.start, prevEnd: s.end }))
      : [];
    setSyncHistory((prev) => [
      ...prev,
      {
        id: target._id,
        prevStart: target.start,
        prevEnd: target.end,
        cursor: syncCursor,
        future: futureSnapshot,
        delta: appliedDelta,
      },
    ]);
    // Compute next-chronological-line identity BEFORE we mutate, so we
    // can advance syncCursor to the same line the operator was about
    // to anchor next, even if the mutation re-sorts the array. Falls
    // back to "stay on the current line if it ended up last" — sync
    // mode auto-exits at array end.
    const nextLineId = (syncCursor + 1 < edited.length)
      ? edited[syncCursor + 1]._id
      : null;

    setEdited((prev) => {
      const mutated = prev.map((s, i) => {
        if (applyCascade && i >= syncCursor && Math.abs(appliedDelta) >= 0.01) {
          return {
            ...s,
            start: s.start + appliedDelta,
            end: s.end + appliedDelta,
          };
        }
        if (s._id === target._id) {
          const segDur = Math.max(0.5, s.end - s.start);
          let newEnd = appliedStart + segDur;
          if (duration && newEnd > duration) newEnd = duration;
          return { ...s, start: appliedStart, end: newEnd };
        }
        return s;
      });
      // Sort by start so the array — and thus syncCursor's positional
      // index, the render order, and the next neighbour lookup — all
      // stay consistent with the new chronological reality.
      const sorted = mutated.sort((a, b) => a.start - b.start);

      // Diagnostic trace: when the focused row's new chronological
      // position differs from its prior position by more than 2 slots,
      // log a structured event so a curious operator with DevTools open
      // can see what just happened. Helped diagnose the 2026-05-19
      // "lines change places" complaint where the reorder was correct
      // but the visual jump was confusing.
      const prevIdx = prev.findIndex((s) => s._id === target._id);
      const newIdx = sorted.findIndex((s) => s._id === target._id);
      if (Math.abs(newIdx - prevIdx) > 2) {
        // eslint-disable-next-line no-console
        console.info("[sync] Anchor caused multi-position reorder", {
          line_id: target._id,
          prev_start: target.start,
          new_start: appliedStart,
          prev_idx: prevIdx,
          new_idx: newIdx,
          jumps: newIdx - prevIdx,
        });
      }
      return sorted;
    });

    // Per-anchor toast: short visual confirmation of "what I just did",
    // dismissed after 2s. Format mirrors the row timestamp display so
    // the operator can mentally match. The toast lives in
    // ToastProvider — fire-and-forget.
    toast({
      message: `Anclada · ${formatTimestamp(target.start)} → ${formatTimestamp(appliedStart)}`,
      tone: Math.abs(appliedDelta) > 5 ? "warning" : "info",
    });

    // Highlight the row for 10s so the eye finds the moved line + we
    // can render a per-row undo button while the ring is up. Cleanup
    // timer removes the id after 10s — if a new anchor fires on the
    // same id, the timer chain restarts (we don't bother dedupe-ing).
    setHighlightedIds((prev) => {
      const next = new Set(prev);
      next.add(target._id);
      return next;
    });
    setTimeout(() => {
      setHighlightedIds((prev) => {
        if (!prev.has(target._id)) return prev;
        const next = new Set(prev);
        next.delete(target._id);
        return next;
      });
    }, 10000);

    // Advance to the line that was visually next BEFORE the mutation.
    // Located by _id so the sort can't drift us onto the wrong line.
    // If that line no longer exists (shouldn't happen for tapAnchor)
    // or there was no "next", exit sync mode.
    if (nextLineId == null) {
      setSyncMode(false);
    } else {
      // We don't know the post-sort position until the next render, so
      // schedule the cursor move in a microtask after setEdited applies.
      // React batches this with the setEdited update — same render.
      queueMicrotask(() => {
        setEdited((current) => {
          const newPos = current.findIndex((s) => s._id === nextLineId);
          if (newPos >= 0) {
            setSyncCursor(newPos);
          } else {
            setSyncMode(false);
          }
          return current; // no mutation, just reading
        });
      });
    }
  }, [syncMode, syncCursor, edited, currentTime, duration, syncCascade]);

  // Keep syncCursor inside the bounds of `edited` after split/delete
  // operations performed mid-sync. Without this, deleting line 8 while
  // the cursor is on line 5 leaves the UI showing line 5 but the next
  // tapAnchor reads from an array that may have shifted under the hood.
  useEffect(() => {
    if (!syncMode) return;
    if (edited.length === 0) {
      setSyncMode(false);
      return;
    }
    if (syncCursor >= edited.length) {
      setSyncCursor(edited.length - 1);
    }
  }, [edited.length, syncMode, syncCursor]);

  const undoLastAnchor = useCallback(() => {
    setSyncHistory((prev) => {
      if (prev.length === 0) return prev;
      const last = prev[prev.length - 1];
      const futureMap = new Map((last.future || []).map((f) => [f.id, f]));
      setEdited((segs) =>
        segs.map((s) => {
          if (s._id === last.id) return { ...s, start: last.prevStart, end: last.prevEnd };
          const f = futureMap.get(s._id);
          if (f) return { ...s, start: f.prevStart, end: f.prevEnd };
          return s;
        }),
      );
      setSyncCursor(last.cursor);
      // Clear the highlight for the undone row — its anchor was reverted,
      // so the "recently moved" indicator is misleading.
      setHighlightedIds((hl) => {
        if (!hl.has(last.id)) return hl;
        const next = new Set(hl);
        next.delete(last.id);
        return next;
      });
      return prev.slice(0, -1);
    });
  }, []);

  // Per-row undo: revert the MOST RECENT anchor that touched this _id.
  // Different from undoLastAnchor (which is strictly LIFO regardless of
  // which line). Lets the operator click ↻ on row X to undo only that
  // anchor, even if several others happened on different lines after.
  const undoAnchorFor = useCallback((id) => {
    setSyncHistory((prev) => {
      const lastIdx = (() => {
        for (let i = prev.length - 1; i >= 0; i--) {
          if (prev[i].id === id) return i;
        }
        return -1;
      })();
      if (lastIdx < 0) return prev;
      const entry = prev[lastIdx];
      const futureMap = new Map((entry.future || []).map((f) => [f.id, f]));
      setEdited((segs) =>
        segs
          .map((s) => {
            if (s._id === entry.id) return { ...s, start: entry.prevStart, end: entry.prevEnd };
            const f = futureMap.get(s._id);
            if (f) return { ...s, start: f.prevStart, end: f.prevEnd };
            return s;
          })
          .sort((a, b) => a.start - b.start),
      );
      setHighlightedIds((hl) => {
        if (!hl.has(id)) return hl;
        const next = new Set(hl);
        next.delete(id);
        return next;
      });
      // Splice out the reverted entry — preserves any later entries on
      // other lines so undoLastAnchor (Z key) keeps a sensible stack.
      return prev.filter((_, i) => i !== lastIdx);
    });
  }, []);

  const enterSyncModeAt = (idx) => {
    if (edited.length === 0) return;
    const safeIdx = Math.max(0, Math.min(idx, edited.length - 1));
    setSyncCursor(safeIdx);
    setSyncHistory([]);
    setSyncMode(true);
    // Lead-in: scrub to ~1.5s before the chosen line so the operator
    // hears the run-up. Don't autoplay — let them press play when ready.
    const target = edited[safeIdx];
    if (target) seekTo(Math.max(0, target.start - 1.5), false);
  };

  const enterSyncMode = () => enterSyncModeAt(0);

  const exitSyncMode = () => {
    setSyncMode(false);
  };

  // Auto-scroll the active row into view while playing. In sync mode,
  // scroll to the armed row instead so the operator always sees what
  // they're about to anchor.
  const lastScrolledIdRef = useRef(null);
  useEffect(() => {
    if (syncMode) {
      const armed = edited[syncCursor];
      if (!armed) return;
      if (lastScrolledIdRef.current === armed._id) return;
      lastScrolledIdRef.current = armed._id;
      const el = rowRefs.current[armed._id];
      if (el) el.scrollIntoView({ block: "center", behavior: "smooth" });
      return;
    }
    if (!isPlaying || activeId === null) return;
    if (lastScrolledIdRef.current === activeId) return;
    // Auditoría 2026-06-10 ("se mueve y no me deja hacer cambios"): este
    // auto-centrado corre durante playback SIN ninguna supresión — si la
    // operadora está escribiendo o ajustando una línea, el panel entero
    // se le va al centro de la fila activa cada 2-5s. Suprimimos cuando
    // hay un input/textarea con foco (está editando texto) — el timeline
    // ya tiene su propia supresión por interacción (FOLLOW_SUPPRESS_MS).
    const ae = typeof document !== "undefined" ? document.activeElement : null;
    if (ae && (ae.tagName === "INPUT" || ae.tagName === "TEXTAREA")) return;
    lastScrolledIdRef.current = activeId;
    const el = rowRefs.current[activeId];
    if (el) el.scrollIntoView({ block: "center", behavior: "smooth" });
  }, [activeId, isPlaying, syncMode, syncCursor, edited]);

  const togglePlay = useCallback(() => {
    const a = audioRef.current;
    if (!a) return;
    if (a.paused) a.play().catch(() => {});
    else a.pause();
  }, []);

  const seekTo = useCallback((seconds, autoplay = true) => {
    const a = audioRef.current;
    if (!a) return;
    const t = Math.max(0, seconds);
    a.currentTime = t;
    playbackTimeRef.current = t;
    lastPublishedTimeRef.current = t;
    // Refleja en el mismo frame del click. Sin esto hay que esperar al
    // próximo rAF tick (~16ms) y el playhead "se desliza" en vez de saltar.
    setCurrentTime(t);
    if (autoplay && a.paused) a.play().catch(() => {});
    trackEditorEvent("editor_seek", { position_ms: Math.round(t * 1000), source: "editor" });
  }, [trackEditorEvent]);

  // "Revisar →": salta a la SIGUIENTE línea marcada review, en orden.
  // Cicla. Hace scroll a la fila, foco al input de texto, seek del audio a
  // su inicio (para escucharla) y un flash breve. Reemplaza el "cazá las
  // filas pintadas" por un recorrido guiado.
  const jumpToNextReview = useCallback(() => {
    const reviewIds = edited.filter((s) => s.review).map((s) => s._id);
    if (!reviewIds.length) return;
    const next = (reviewNavIdxRef.current + 1) % reviewIds.length;
    reviewNavIdxRef.current = next;
    const id = reviewIds[next];
    const seg = edited.find((s) => s._id === id);
    const el = rowRefs.current[id];
    if (el) {
      el.scrollIntoView({ behavior: "smooth", block: "center" });
      const input = el.querySelector('input[type="text"]');
      if (input) input.focus();
    }
    if (seg) {
      setFocusedSegId(id);
      seekTo(Math.max(0, seg.start), false);
    }
    setFlashReviewId(id);
    setTimeout(() => setFlashReviewId((cur) => (cur === id ? null : cur)), 1200);
  }, [edited, seekTo]);

  // Spacebar: in sync mode, anchors the current line; otherwise toggles
  // play/pause. Cmd/Ctrl+Z (or just Z) reverts the last anchor while
  // in sync mode so the operator can recover from a mistap.
  useEffect(() => {
    const onKey = (e) => {
      const tag = (document.activeElement?.tagName || "").toUpperCase();
      const editing = tag === "INPUT" || tag === "TEXTAREA" || document.activeElement?.isContentEditable;
      if (editing) return;
      if (e.code === "Space") {
        // Ignore keyboard autorepeat / sustained press so the operator
        // doesn't anchor 3 lines from one apparent tap. Native autorepeat
        // fires keydown ~20 times per second on most OSes — one anchor
        // is the operator's intent, the rest are noise.
        if (e.repeat) { e.preventDefault(); return; }
        e.preventDefault();
        if (syncMode) tapAnchor();
        else togglePlay();
      } else if ((e.metaKey || e.ctrlKey) && (e.key === "z" || e.key === "Z")) {
        // Cmd/Ctrl+Z: undo. Sync Mode rolls back the last anchor (with
        // its propagated future); outside Sync Mode it pops the manual
        // edit history (single-line edits, suggestions, intro trim).
        e.preventDefault();
        if (syncMode) undoLastAnchor();
        else undoEdit();
      } else if (syncMode && (e.key === "z" || e.key === "Z")) {
        e.preventDefault();
        undoLastAnchor();
      } else if (syncMode && e.key === "Escape") {
        e.preventDefault();
        exitSyncMode();
      } else if ((e.metaKey || e.ctrlKey) && (e.key === "k" || e.key === "K")) {
        // Cmd/Ctrl+K — toggle Sync mode (refactor 2026-05-23: el botón visual
        // se compactó a ícono discreto, este shortcut es la forma rápida
        // desde teclado). Funciona en ambos sentidos: si ya está activo, sale.
        if (!audioUrl) return;  // no sync sin audio
        e.preventDefault();
        if (syncMode) exitSyncMode();
        else enterSyncMode();
      } else if (!e.metaKey && !e.ctrlKey && !e.altKey && (e.key === "f" || e.key === "F")) {
        // 2026-05-25 — F toggle "Modo Enfoque". Guard `editing` arriba
        // ya nos protege de capturarlo cuando el operador está tipeando
        // en un input/textarea (la mayoría de las veces). Sin modifier
        // keys para que no choque con Cmd+F (buscar nativo del browser).
        e.preventDefault();
        toggleFocusMode();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [togglePlay, syncMode, tapAnchor, undoLastAnchor, undoEdit, audioUrl, enterSyncMode, exitSyncMode, toggleFocusMode]);

  // ─── Reference lyrics suggestions (unchanged) ───────────────────────
  const refLines = useMemo(() => {
    if (!referenceLyrics) return [];
    return referenceLyrics.split("\n").filter((l) => l.trim());
  }, [referenceLyrics]);

  const suggestionsById = useMemo(() => {
    const map = {};
    let refIdx = 0;
    segments.forEach((seg, i) => {
      const suggestion = findSuggestion(seg.text, refLines, refIdx);
      map[i] = suggestion;
      if (suggestion) {
        const idx = refLines.findIndex(
          (l, j) => j >= refIdx && l.toLowerCase().includes(seg.text.toLowerCase().split(" ")[0]?.toLowerCase())
        );
        if (idx >= 0) refIdx = idx + 1;
      }
    });
    return map;
  }, [segments, refLines]);

  // Detección de segments mergeados (2 lyric lines en 1 segment) usando
  // lrclib plain como oracle. Caso real motivador: Whisper agrupa
  // 2 versos consecutivos en un solo segment ("Siento el calor de toda
  // tu piel en mi cuerpo otra vez") cuando lrclib los tiene como
  // entries separadas. El banner banner-prominent al tope del editor
  // ofrece auto-dividir TODO el lote con 1 click.
  const mergeableSegments = useMemo(() => {
    if (refLines.length < 2) return [];
    const out = [];
    edited.forEach((seg) => {
      if (!seg.text || !seg.text.trim()) return;
      const pair = findReferenceSplitLines(seg.text, refLines);
      if (pair) out.push({ _id: seg._id, splitLines: pair });
    });
    return out;
  }, [edited, refLines]);

  // Auto-dividir TODOS los segments mergeados usando reference como
  // oracle. Para cada uno: timestamp split proporcional al char-count
  // de cada línea (lineA más larga = más tiempo). Reverse-order para
  // no romper índices durante la mutación.
  const autoSplitAllFromReference = () => {
    if (mergeableSegments.length === 0) return;
    pushEditHistory();
    setEdited((prev) => {
      // Map id → splitLines para lookup rápido
      const byId = new Map(
        mergeableSegments.map((m) => [m._id, m.splitLines]),
      );
      const result = [];
      let nextId = prev.reduce((m, s) => Math.max(m, s._id), -1) + 1;
      for (const seg of prev) {
        const splitLines = byId.get(seg._id);
        if (!splitLines) {
          result.push(seg);
          continue;
        }
        const [lineA, lineB] = splitLines;
        const totalChars = lineA.length + lineB.length;
        if (totalChars === 0) {
          result.push(seg);
          continue;
        }
        const ratio = lineA.length / totalChars;
        const dur = Math.max(0.6, seg.end - seg.start);
        const midTime = seg.start + dur * ratio;
        const gap = 0.05;
        result.push({
          ...seg,
          _id: nextId++,
          text: lineA,
          end: Math.max(seg.start + 0.3, midTime - gap),
        });
        result.push({
          ...seg,
          _id: nextId++,
          text: lineB,
          start: Math.min(seg.end - 0.3, midTime),
          end: seg.end,
        });
      }
      return result;
    });
  };

  const updateText = (id, text) => {
    pushEditHistory();
    setEdited((prev) => prev.map((seg) => (seg._id === id ? { ...seg, text } : seg)));
  };

  // Called on blur of a line's text input. If the operator changed a line
  // that was identical to other lines (a repeated chorus), offer to apply
  // the same new text to those other occurrences. Match is against the
  // PRE-edit text (textEditStart), so lines the operator already diverged by
  // hand never match and are never touched. Newly-typed text is compared
  // exact (trim + collapsed whitespace), case/accent sensitive.
  const handleTextBlur = (id, newText) => {
    const start = textEditStart;
    setTextEditStart(null);
    if (!start || start.id !== id) return;
    const prevNorm = normalizeLineForMatch(start.text);
    const newNorm = normalizeLineForMatch(newText);
    // Only prompt when the text actually changed and the pre-edit line had
    // real content (don't propagate blanks).
    if (!prevNorm || prevNorm === newNorm) return;
    const matchIds = edited
      .filter((s) => s._id !== id && normalizeLineForMatch(s.text) === prevNorm)
      .map((s) => s._id);
    if (matchIds.length > 0) {
      setPropagationPrompt({ id, newText, matchIds, prevText: start.text });
    }
  };

  const applyPropagation = () => {
    if (!propagationPrompt) return;
    const { newText, matchIds } = propagationPrompt;
    pushEditHistory();
    const idset = new Set(matchIds);
    setEdited((prev) => prev.map((s) => (idset.has(s._id) ? { ...s, text: newText } : s)));
    setPropagationPrompt(null);
    // Flush-save immediately so the propagated lines persist without waiting
    // for the 3 s autosave debounce.
    setFlushCounter((c) => c + 1);
    toast({
      message: (t("editor.repeat_applied") || "Cambio aplicado a {n} líneas repetidas")
        .replace("{n}", matchIds.length),
      tone: "info",
    });
  };

  const dismissPropagation = () => setPropagationPrompt(null);

  const applySuggestion = (id) => {
    const suggestion = suggestionsById[id];
    if (suggestion) updateText(id, suggestion);
  };

  const applyAllSuggestions = () => {
    pushEditHistory();
    setEdited((prev) =>
      prev.map((seg) => {
        const suggestion = suggestionsById[seg._id];
        return suggestion ? { ...seg, text: suggestion } : seg;
      })
    );
  };

  // Shift the entire timeline by `delta` seconds, clamping start/end to
  // [0, duration]. Used by the "Recortar intro" banner so the operator
  // can collapse a long instrumental intro down to a configurable
  // pre-roll without manually nudging every line.
  const shiftAllSegments = useCallback((delta) => {
    if (Math.abs(delta) < 0.05) return;
    pushEditHistory();
    setEdited((prev) => shiftBlockWithinDuration(prev, delta, duration));
  }, [pushEditHistory, duration]);

  const deleteSegments = useCallback((ids) => {
    const requested = new Set(Array.isArray(ids) ? ids : [ids]);
    const existingIds = edited.filter((seg) => requested.has(seg._id)).map((seg) => seg._id);
    if (!existingIds.length) return false;
    if (existingIds.length > 1 && !window.confirm(
      `¿Eliminar ${existingIds.length} líneas? Podés deshacerlo con Cmd/Ctrl+Z.`,
    )) return false;
    recordEditorAction("delete", { ids: existingIds, count: existingIds.length });
    pushEditHistory();
    const idSet = new Set(existingIds);
    setEdited((prev) => prev.filter((seg) => !idSet.has(seg._id)));
    setFlushCounter((c) => c + 1);
    trackEditorEvent("editor_timing_changed", {
      count: existingIds.length,
      operation: "delete",
      duration_ms: 0,
    });
    return true;
  }, [edited, pushEditHistory, trackEditorEvent]);

  const deleteSeg = (id) => deleteSegments([id]);

  // Text-length-based cap. Used by the per-row ✂ button + bulk-trim
  // action in this editor. There is NO automatic application — the
  // operator chooses when (and per-segment whether) to apply.
  const TRIM_FLOOR_S = 3.5;
  const TRIM_PER_CHAR_S = 0.10;
  const TRIM_MARGIN_S = 1.0;
  const estimateVoiceEndDuration = (text) =>
    Math.max(TRIM_FLOOR_S, (text || "").length * TRIM_PER_CHAR_S + TRIM_MARGIN_S);

  /** Bulk: trim every segment whose duration exceeds the cap. Each
   * segment is trimmed independently — only its own `end` is modified
   * based on its own text length and start. No cross-segment effect. */
  const trimAllLongSegs = () => {
    pushEditHistory();
    setEdited((prev) =>
      prev.map((seg) => {
        const dur = seg.end - seg.start;
        const cap = estimateVoiceEndDuration(seg.text);
        if (dur <= cap) return seg;
        return { ...seg, end: seg.start + cap };
      }),
    );
  };

  const longSegCount = edited.filter((seg) => {
    const dur = seg.end - seg.start;
    return dur > estimateVoiceEndDuration(seg.text);
  }).length;

  // Auto-trim on initial load: if the just-loaded segments have hanging
  // text (lrclib/genius lines that ran into instrumental outros, or
  // duplicated chorus blocks at the end), apply the same fix the
  // operator would have applied manually via the autofix banner. The
  // `autoTrimAppliedRef` (declared up by the segments re-seed effect)
  // guards against re-running on every text-edit keystroke. Cmd-Z still
  // works because trimAllLongSegs calls pushEditHistory.
  useEffect(() => {
    if (autoTrimAppliedRef.current) return;
    if (!edited || edited.length === 0) return;
    if (longSegCount > 0) {
      trimAllLongSegs();
    }
    autoTrimAppliedRef.current = true;
  }, [edited, longSegCount]);

  // Compute how many visual lines a segment will occupy in the video.
  const linesForSeg = useCallback((text) => {
    const displayText = applyCase(text || "", textCase);
    const tier = getTier(displayText);
    const fontCss = FONT_CSS_MAP[font] || FONT_CSS_MAP[""];
    const sizePx = Math.round(tier.sizePx * Math.max(0.6, Math.min(1.5, fontScale)));
    return estimateWrappedLines(displayText, fontCss, sizePx, tier.maxWidthPx);
  }, [font, textCase, fontScale]);

  // Split a segment into two. When `charOffset` is given (operator pressed Enter
  // at the cursor) AND the segment carries per-word timing, the split is
  // WORD-AWARE: each half inherits the REAL start/end of its words — no re-sync.
  // Otherwise (the "✂ Dividir" button, which has no cursor, or a segment with no
  // word timing) we fall back to the canvas wrap-boundary + char-ratio timing
  // and DROP the now-meaningless `words` array from both halves (keeping the
  // parent's full `words` on each child was the old bug → wrong per-word timing).
  const splitSegAt = (id, charOffset) => {
    pushEditHistory();
    setEdited((prev) => {
      const idx = prev.findIndex((s) => s._id === id);
      if (idx === -1) return prev;
      const seg = prev[idx];
      const nextId1 = prev.reduce((m, s) => Math.max(m, s._id), -1) + 1;
      const nextId2 = nextId1 + 1;

      // ── WORD-ACCURATE PATH (Enter at cursor, segment has word timing) ──
      if (charOffset != null && Array.isArray(seg.words) && seg.words.length > 1) {
        const r = splitWordsAtCharOffset(seg.text, seg.words, charOffset);
        if (r) {
          const aStart = firstWordStart(r.wordsA);
          const aEnd = lastWordEnd(r.wordsA);
          const bStart = firstWordStart(r.wordsB);
          const bEnd = lastWordEnd(r.wordsB);
          const s1 = {
            ...seg, _id: nextId1, text: r.textA, words: r.wordsA,
            start: aStart != null ? aStart : seg.start,
            end: aEnd != null ? aEnd : seg.end,
          };
          const s2 = {
            ...seg, _id: nextId2, text: r.textB, words: r.wordsB,
            start: bStart != null ? bStart : s1.end + 0.05,
            end: bEnd != null ? bEnd : seg.end,
          };
          if (!(s2.start > s1.end)) s2.start = s1.end + 0.02; // monotonic safety
          return [...prev.slice(0, idx), s1, s2, ...prev.slice(idx + 1)];
        }
        // r === null (degenerate / text edited so tokens≠words) → char-ratio below
      }

      // ── FALLBACK: char-ratio (no word timing, or unaligned text) ──
      const fullText = seg.text || "";
      let cut = charOffset; // cursor split point when present
      if (cut == null) {
        // Button path: keep the old behaviour — split at the canvas wrap boundary.
        const displayText = applyCase(fullText, textCase);
        const tier = getTier(displayText);
        const fontCss = FONT_CSS_MAP[font] || FONT_CSS_MAP[""];
        const sizePx = Math.round(tier.sizePx * Math.max(0.6, Math.min(1.5, fontScale)));
        const wlist = fullText.split(" ");
        const canvas = document.createElement("canvas");
        const ctx = canvas.getContext("2d");
        ctx.font = `bold ${sizePx}px ${fontCss}`;
        const spaceW = ctx.measureText(" ").width;
        let lineW = 0;
        let splitIdx = Math.floor(wlist.length / 2);
        for (let wi = 0; wi < wlist.length - 1; wi++) {
          const ww = ctx.measureText(applyCase(wlist[wi], textCase)).width;
          lineW = lineW > 0 ? lineW + spaceW + ww : ww;
          if (lineW > tier.maxWidthPx) { splitIdx = wi > 0 ? wi : 1; break; }
          splitIdx = wi + 1;
        }
        cut = wlist.slice(0, splitIdx).join(" ").length + 1; // +1 for the space
      }
      const part1 = fullText.slice(0, cut).trim();
      const part2 = fullText.slice(cut).trim();
      if (!part1 || !part2) return prev; // never create an empty line
      // Char ratio (long words take longer to sing → matches the vocal pause
      // better than word-count). Drop the stale `words` array from both halves.
      const ratio = part1.length / Math.max(1, part1.length + part2.length);
      const midTime = seg.start + (seg.end - seg.start) * ratio;
      const gap = 0.05;
      const { words: _dropWords, ...segNoWords } = seg;
      const s1 = { ...segNoWords, _id: nextId1, text: part1, end: Math.max(seg.start + 0.3, midTime - gap) };
      const s2 = { ...segNoWords, _id: nextId2, text: part2, start: Math.min(seg.end - 0.3, midTime), end: seg.end };
      return [...prev.slice(0, idx), s1, s2, ...prev.slice(idx + 1)];
    });
  };
  // Back-compat: the "✂ Dividir" button + bulk callers split with no cursor.
  const splitSeg = (id) => splitSegAt(id, null);

  // Merge a line with the NEXT line: concatenate text + per-word timing,
  // start = first.start, end = second.end. If only one side has `words` we
  // can't fabricate timing for the gap → drop words (karaoke falls back to
  // uniform distribution, consistent with the split fallback).
  const mergeSeg = (id) => {
    recordEditorAction("merge", { id });
    pushEditHistory();
    setEdited((prev) => {
      const idx = prev.findIndex((s) => s._id === id);
      if (idx === -1 || idx >= prev.length - 1) return prev;
      const a = prev[idx];
      const b = prev[idx + 1];
      const text = `${(a.text || "").trim()} ${(b.text || "").trim()}`.trim();
      const aw = Array.isArray(a.words) ? a.words : null;
      const bw = Array.isArray(b.words) ? b.words : null;
      const mergedWords = aw && bw ? [...aw, ...bw] : null;
      const { words: _awDrop, ...aNoWords } = a;
      const merged = {
        ...aNoWords, text, start: a.start, end: b.end,
        ...(mergedWords ? { words: mergedWords } : {}),
      };
      return [...prev.slice(0, idx), merged, ...prev.slice(idx + 2)];
    });
  };

  // Insert a duplicate of `seg` immediately after it. Same text, same
  // duration, start placed right after the original ends so the new
  // row visibly differs in time. Operator typically re-syncs it via
  // Sync mode tap or manual edit. Useful when Whisper missed a chorus
  // repeat — duplicate the chorus block, then tap-sync the copies.
  const duplicateSeg = (id) => {
    recordEditorAction("duplicate", { id, segments: Array.isArray(edited) ? edited.length : null });
    if (!edited.some((seg) => seg._id === id)) return;
    pushEditHistory();
    setEdited((prev) => {
      const idx = prev.findIndex((s) => s._id === id);
      if (idx === -1) return prev;
      const orig = prev[idx];
      const segDur = Math.max(0.5, orig.end - orig.start);
      const newStart = Math.min(duration || orig.end + segDur, orig.end);
      const newEnd = Math.min(duration || newStart + segDur, newStart + segDur);
      const nextId = prev.reduce((m, s) => Math.max(m, s._id), -1) + 1;
      const dup = { ...orig, _id: nextId, start: newStart, end: newEnd };
      return [...prev.slice(0, idx + 1), dup, ...prev.slice(idx + 1)];
    });
    setFlushCounter((c) => c + 1);
  };

  // Append a blank line at the end of the list. Operator types the
  // missing lyrics into the text input, then tap-syncs it.
  const addBlankLine = () => {
    recordEditorAction("addLine", { segments: Array.isArray(edited) ? edited.length : null });
    pushEditHistory();
    setEdited((prev) => {
      // Insert the new line at the audio playhead — that's where the
      // operator is listening when they realise something's missing
      // (typical case: a chorus repetition the pipeline collapsed,
      // or a verse Whisper skipped). The previous behaviour pinned
      // every new line to `last.end + 0.5`, so click "Agregar línea"
      // at 1:23 of a song and the row appeared at the END of the
      // editor with the wrong timestamp. SPACE then clamped it to
      // an already-wrong neighbour bound.
      //
      // Fallback when currentTime is 0 (audio not playing yet) or out
      // of the song's range: drop the new line after the last existing
      // one, same as before. That way the wizard's first "add line"
      // on a fresh job (before pressing play) doesn't land at 0:00
      // pegado al primer segment.
      // Note: we do NOT subtract AUDIO_LATENCY_COMPENSATION_S here.
      // tapAnchor compensates because the operator is reacting to
      // *heard* audio while the playhead has decoded ~80 ms ahead. But
      // "Add line at playhead" is an explicit click — they want the
      // segment to start where the cursor is, not 80 ms before.
      const playhead = currentTime > 0 ? Math.max(0, currentTime) : 0;
      const last = prev[prev.length - 1];
      const lastEnd = last ? last.end : 0;
      const baseStart = playhead > 0
        ? Math.min(playhead, duration ? duration - 0.5 : playhead)
        : Math.min(duration || lastEnd + 2, lastEnd + 0.5);
      const segDur = 3;
      const baseEnd = Math.min(
        duration || baseStart + segDur,
        baseStart + segDur,
      );
      const nextId = prev.reduce((m, s) => Math.max(m, s._id), -1) + 1;
      const inserted = { _id: nextId, start: baseStart, end: baseEnd, text: "" };
      // Keep `edited` sorted by start so syncCursor / neighbour clamp /
      // /save-segments autosave all see a monotonic timeline. The
      // backend also sorts (#184) but doing it here keeps the UI's
      // immediate render consistent without waiting for a round-trip.
      return [...prev, inserted].sort((a, b) => a.start - b.start);
    });
    setFlushCounter((c) => c + 1);
  };

  // Insert a blank line right AFTER the row at display index `idx`, timing
  // interpolated into the gap to the next line. This is the "add a line in
  // the MIDDLE of the song" affordance — the bottom "Agregar línea" button
  // forced the operator to scroll away from where they were working.
  const insertLineAfter = (idx) => {
    recordEditorAction("insertLine", { idx });
    if (idx < 0 || idx >= edited.length) return;
    pushEditHistory();
    setEdited((prev) => {
      const cur = prev[idx];
      const nxt = prev[idx + 1];
      const gapStart = cur ? cur.end : (prev[0] ? prev[0].start : 0);
      const gapEnd = nxt ? nxt.start : (duration || gapStart + 3);
      const gap = Math.max(0, gapEnd - gapStart);
      let s = gapStart + (gap > 0.6 ? gap / 3 : 0.1);
      let e = s + (gap > 0.6 ? gap / 3 : 1.0);
      if (!(e > s) || e > gapEnd) {
        s = gapStart + 0.1;
        e = Math.min(gapEnd > s ? gapEnd - 0.05 : s + 1.0, s + 1.0);
        if (e <= s) e = s + 0.5;
      }
      const nextId = prev.reduce((m, x) => Math.max(m, x._id), -1) + 1;
      const inserted = { _id: nextId, start: s, end: e, text: "" };
      return [...prev, inserted].sort((a, b) => a.start - b.start);
    });
    setFlushCounter((c) => c + 1);
  };

  // Smart "Agregar línea": when the operator has a row selected/focused,
  // insert the new line RIGHT BELOW it (gap-interpolated, sync-preserving via
  // insertLineAfter) — that's where they expect it to land. Only fall back to
  // the playhead/end behaviour (addBlankLine) when nothing is focused yet
  // (fresh job, before touching any row). focusedSegId persists as the
  // last-focused row, so clicking the bottom button doesn't lose the target.
  const addLineSmart = () => {
    if (focusedSegId != null) {
      const selIdx = edited.findIndex((s) => s._id === focusedSegId);
      if (selIdx !== -1) {
        insertLineAfter(selIdx);
        return;
      }
    }
    addBlankLine();
  };

  // Operator-friendly title: strip extension + collapse underscores/dashes
  // to em-dashes + title-case respecting Spanish/PT lowercase stop-words.
  // Pre-fix the header showed the raw filename ("El Arbol De La Vida _ Voy
  // A Dejarte - Viejas Locas"); now it reads "El Arbol de la Vida — Voy a
  // Dejarte — Viejas Locas". See lib/prettifySongTitle.js. (UI F7.)
  const name = prettifySongTitle(filename);
  const pendingSuggestions = edited.filter((seg) => {
    const s = suggestionsById[seg._id];
    return s && s !== seg.text;
  }).length;
  const hasSuggestions = pendingSuggestions > 0;
  const blankCount = edited.filter((seg) => !(seg.text || "").trim()).length;

  const _buildCleanedSegments = () => {
    const sorted = sanitizeSegments(edited)
      .filter((seg) => (seg.text || "").trim())
      .sort((a, b) => a.start - b.start);
    return sorted.map((seg, i) => {
      let end = seg.end;
      if (i + 1 < sorted.length) {
        const nextStart = sorted[i + 1].start;
        if (end > nextStart - 0.05) {
          end = Math.max(seg.start + 0.3, nextStart - 0.05);
        }
      }
      return { ...seg, end };
    });
  };

  // Single-flight del CTA completo, incluido el flush de autosave que ocurre
  // ANTES de onApprove. El lock de App sólo cubre el POST /edit; no alcanzaba
  // para los clicks que quedaban esperando el mismo flush y luego despertaban
  // después de que el primer POST ya había navegado al progreso. Esos callbacks
  // tardíos enviaban el edit de nuevo y mostraban "ya se está re-renderizando"
  // encima de un edit que en realidad sí había arrancado.
  const approveInFlightRef = useRef(false);
  const [isApproving, setIsApproving] = useState(false);

  const runApprove = async ({ skipWrapWarning = false } = {}) => {
    if (editorV2Enabled && (!durableHydrated || durableEditor.loading)) {
      toast({ message: "Estamos cargando la última versión. Esperá un instante para aprobar.", tone: "info" });
      return;
    }
    if (saveStatus === "conflict" || durableEditor.conflict) {
      setConflictDialogOpen(true);
      toast({ message: "Resolvé el conflicto antes de aprobar.", tone: "error" });
      return;
    }
    if (saveErrorReason === "draft-corrupt") {
      toast({ message: "Descartá o recuperá manualmente el borrador local antes de aprobar.", tone: "error" });
      return;
    }
    // Aviso (no bloqueo) si el último autosave falló. IMPORTANTE — contrato
    // real verificado (incidente UMG 21-jul-2026): aprobar manda los
    // segments EN PANTALLA en el cuerpo del POST (onApprove(cleaned) acá;
    // App los envía en segments_json / edit body y el backend pisa
    // segments_json antes de renderizar). El autosave fallido solo afecta
    // el RESPALDO del servidor (reanudar tras refresh / reaper), no el
    // render. El copy anterior decía lo contrario y llevó a operadores a
    // no aprobar trabajo que sí estaba a salvo.
    if (saveStatus === "error") {
      const _copy = _SAVE_ERROR_COPY[saveErrorReason] || _SAVE_ERROR_COPY.server;
      const proceed = window.confirm(_copy.confirm);
      if (!proceed) return;
    }
    // Check for 3+ line segments before submitting — show a warning banner
    // so the operator can auto-split them rather than discover the issue
    // after waiting for the full video render.
    const problematic = edited.filter(
      (seg) => (seg.text || "").trim() && linesForSeg(seg.text) >= 3
    );
    if (problematic.length > 0 && !skipWrapWarning) {
      setWrapWarning({ ids: problematic.map((s) => s._id) });
      return;
    }
    setWrapWarning(null);
    const cleaned = _buildCleanedSegments();
    if (editorV2Enabled) {
      // `_buildCleanedSegments` may tighten an overlap by 50 ms. Persist
      // that exact final snapshot before sending its revision/version id;
      // Editor 2.0 intentionally ignores browser JSON during approval.
      const cleanedForPersistence = sanitizeSegmentsForPersistence(cleaned);
      const saveResult = await flushDurableSave("manual", cleanedForPersistence);
      if (saveResult?.ok === false) {
        if (saveResult.reason === "conflict") setConflictDialogOpen(true);
        else toast({ message: "No pudimos confirmar esta versión. Reintentá sin cerrar el editor.", tone: "error" });
        return;
      }
      const approvalResult = await Promise.resolve(onApprove(cleaned.map(({ _id, ...rest }) => rest), {
        baseRevision: saveResult.revision,
        editorRevision: saveResult.revision,
        editorVersionId: saveResult.versionId,
      }));
      if (approvalResult?.ok === false) {
        setIsDirty(true);
        if (approvalResult.reason === "conflict") {
          const remote = approvalResult.conflict || {};
          durableEditor.stageConflict(cleanedForPersistence, {
            serverRevision: Number.isInteger(remote.server_revision)
              ? remote.server_revision : durableEditor.revisionRef.current,
            serverSegments: Array.isArray(remote.server_segments)
              ? remote.server_segments : durableEditor.document?.segments || [],
            updatedBy: remote.updated_by || null,
            updatedAt: remote.updated_at || null,
            reason: "approval-conflict",
          });
          setSaveStatus("conflict");
          setSaveErrorReason("conflict");
          setConflictDialogOpen(true);
        } else {
          toast({ message: "No pudimos aprobar esta versión. Tus cambios siguen guardados para reintentar.", tone: "error" });
        }
        return;
      }
      setIsDirty(false);
      trackEditorEvent("editor_approved", { revision: saveResult.revision });
      return;
    }
    if (disableAutosave || !onPersistSegments || !transcribeJobId) {
      setIsDirty(false);
      await Promise.resolve(onApprove(cleaned.map(({ _id, ...rest }) => rest), {
        baseRevision: Number.isInteger(segmentsRevision) ? segmentsRevision : 0,
      }));
      return;
    }
    let saveResult = await flushPendingSave();
    if (saveResult?.ok === false && saveResult.reason === "stale-revision") {
      setSaveStatus("conflict");
      setSaveErrorReason("conflict");
      toast({ message: t("editor.approve_conflict") || "Hay una versión más nueva. Recargá para compararla antes de aprobar.", tone: "error" });
      return;
    }
    setIsDirty(false);
    await Promise.resolve(onApprove(cleaned.map(({ _id, ...rest }) => rest), {
      baseRevision: Number.isInteger(saveResult?.revision)
        ? saveResult.revision
        : (Number.isInteger(segmentsRevision) ? segmentsRevision : 0),
    }));
  };

  const handleApprove = async (options = {}) => {
    if (approveInFlightRef.current) return;
    approveInFlightRef.current = true;
    setIsApproving(true);
    try {
      await runApprove({ skipWrapWarning: options?.skipWrapWarning === true });
    } finally {
      approveInFlightRef.current = false;
      setIsApproving(false);
    }
  };

  const handleBackSafely = useCallback(async () => {
    const result = await flushPendingSave();
    if (result?.ok === false && result.reason === "stale-revision") return;
    onBack?.();
  }, [flushPendingSave, onBack]);

  const progressPct = duration > 0 ? (currentTime / duration) * 100 : 0;

  // UX 2026-05-26: cuando synced-direct fallback (PR #365) dispara, TODAS
  // las líneas vienen con `review: true`. Marcar cada una individualmente
  // con badge "⚠ revisar tiempo" satura visualmente (28 badges apilados).
  // Heurística: si ≥3 líneas son review, mostramos un BANNER único arriba
  // y suprimimos los badges per-línea (el banner ya transmite la info).
  // Si <3 son review, el badge per-línea queda — es info útil sin saturar.
  const reviewSegCount = edited.reduce((n, s) => n + (s.review ? 1 : 0), 0);
  // Banner calmo con navegador secuencial: se muestra con ≥1 línea review.
  // Antes era ≥3 (con badges per-línea abajo); ahora el banner + la barra
  // de acento sutil cubren cualquier cantidad sin ruido.
  const showReviewBanner = reviewSegCount >= 1;

  // UX 2026-05-26 (cont.): mismo problema con la warning "● ⚠ 2 líneas" + botón
  // "Dividir" que aparece cuando el render del video va a wrappar el texto a
  // 2 renglones. Con líneas tipo "Será por eso que hoy estamos aquí" o
  // "No hay nadie más que vos y yo" en upper-case + bold (~28 chars), el
  // wrap se dispara y 28 lineas seguidas con "2 líneas + Dividir" son ruido.
  // Mismo enfoque: si ≥3 segments hit, banner único arriba + bulk action.
  const wrap2SegIds = edited
    .filter((s) => (s.text || "").trim() && linesForSeg(s.text) === 2)
    .map((s) => s._id);
  const wrap2Count = wrap2SegIds.length;
  const showWrap2Banner = wrap2Count >= 3;

  // ─── Rediseño de controles (2026-07) — valores derivados de la nueva
  // barra de 2 filas: un chip primario "Revisar", un chip ghost "Aplicar
  // corrección", y un disclosure "Ajustes del video".
  const cap99 = (n) => (n > 99 ? "99+" : String(n));
  // Auto-fix (correcciones automáticas del sistema) — antes un banner
  // verde; ahora un chip ghost con popover de detalle + Deshacer.
  const splitAvailable = !disableAutoSplit && mergeableSegments.length > 0;
  const trimAvailable = longSegCount > 0;
  const hasAutoFix = splitAvailable || hasSuggestions || trimAvailable;
  const fixCount = (splitAvailable ? 1 : 0) + (hasSuggestions ? 1 : 0) + (trimAvailable ? 1 : 0);
  const hasUndo = editHistory.length > 0;
  const applyAllFixes = () => {
    // Orden: split (cambia el nº de segmentos), luego suggestions (texto
    // por segmento), luego trim (end por segmento). Cada handler llama a
    // pushEditHistory así Cmd-Z los deshace paso a paso.
    if (splitAvailable) autoSplitAllFromReference();
    if (hasSuggestions) applyAllSuggestions();
    if (trimAvailable) trimAllLongSegs();
    setFixPopoverOpen(false);
  };
  // "Ajustes del video" (disclosure) — concerns informativos del render:
  // wrap a 2 renglones + intro instrumental larga. Neutral, sin ámbar.
  const first = edited[0];
  const introLong = !!(first && first.start > 3);
  const videoSettingsCount = (showWrap2Banner ? 1 : 0) + (introLong ? 1 : 0);
  // Línea de confianza (muted, sin caja): funde "Sincronizado con tu
  // letra" (si hay líneas review del anclado) + estado del fondo. Si no
  // hay nada que avisar → "Todo listo".
  const confidenceParts = [];
  if (reviewSegCount > 0) confidenceParts.push(t("editor.confidence_synced") || "Sincronizado con tu letra");
  if (bgStatus === "done") confidenceParts.push(t("editor.confidence_bg_done") || "Fondo listo");
  else if (bgStatus === "queued" || bgStatus === "generating") confidenceParts.push(t("editor.confidence_bg_generating") || "Generando fondo…");
  else if (bgStatus === "error") confidenceParts.push(t("editor.confidence_bg_error") || "El fondo se genera al aprobar");
  const confidenceText = confidenceParts.length
    ? confidenceParts.join(" · ")
    : (t("editor.confidence_all_ready") || "Todo listo");
  const saveStatusLabel = {
    idle: isDirty ? "Cambios locales" : "Guardado",
    local: "Cambios locales",
    saving: "Guardando…",
    saved: "Guardado",
    offline: "Sin conexión",
    conflict: "Conflicto detectado",
    error: "No se pudo guardar",
  }[saveStatus] || "Cambios locales";
  const collaboratingUser = editorV2Enabled
    && durableEditor.lock?.active
    && durableEditor.lock?.user
    && String(durableEditor.lock.user.id) !== String(user?.id)
    ? durableEditor.lock.user
    : null;

  const handleScrub = (e) => {
    if (!duration) return;
    const rect = e.currentTarget.getBoundingClientRect();
    const pct = (e.clientX - rect.left) / rect.width;
    seekTo(pct * duration, false);
  };

  // INCIDENT 2026-05-24: the list view used `max-w-3xl` (~768 px). With
  // the 2-col grid (preview / lines) that left the preview ~360 px wide —
  // "TENDRÉ QUE DEJARTE..." felt cramped — and the line inputs only had
  // ~280 px so anything longer than 30 chars visually cut off ("Nuestra
  // relación no es pa…"). Bumped to a generous 1400 px so both columns
  // breathe: preview ~680 px wide (≈ 2× before), lines fit ≈ 60 chars per
  // row before scrolling. Timeline view stays at max-w-6xl (already wide
  // enough).
  return (
    // UI F10 (2026-05-26): pb-28 (7 rem ≈ 112 px) garantiza safe-area
    // bajo el botón flotante "Aprobar y generar" (h-12 = 48 px + bottom-6
    // = 24 px + sombra). Sin esto la última card del timeline o de la
    // lista quedaba tapada cuando el operador scrolleaba hasta el final.
    <div data-testid="lyrics-editor" aria-busy={editorInitializationBlocked} className={`w-full mx-auto pb-28 ${viewMode === "advanced" ? "max-w-[1800px] px-2 sm:px-4" : "max-w-[1400px]"}`}>
      {editorInitializationBlocked && createPortal(
        <div
          className="fixed inset-0 z-[80] flex items-center justify-center bg-surface-0/70 px-5 backdrop-blur-sm"
          role={durableEditor.error ? "alertdialog" : "status"}
          aria-modal={durableEditor.error ? "true" : undefined}
          aria-label={durableEditor.error ? "No pudimos abrir la versión editable" : "Preparando editor"}
        >
          <div className="w-full max-w-sm rounded-3xl border border-white/[0.10] bg-surface-1 p-6 text-center shadow-2xl shadow-black/40">
            {durableEditor.error ? (
              <>
                <div className="mx-auto flex h-11 w-11 items-center justify-center rounded-2xl bg-red-500/10 text-red-300 ring-1 ring-red-400/20" aria-hidden="true">
                  <svg className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                    <path d="M12 9v4m0 4h.01M10.3 3.6 2.5 17.1A2 2 0 0 0 4.2 20h15.6a2 2 0 0 0 1.7-2.9L13.7 3.6a2 2 0 0 0-3.4 0Z" strokeLinecap="round" strokeLinejoin="round" />
                  </svg>
                </div>
                <h3 className="mt-4 text-base font-semibold text-white">No pudimos abrir la versión editable</h3>
                <p className="mt-2 text-sm leading-6 text-ink-secondary">
                  Tus líneas siguen a salvo. Reconectá el editor antes de modificar o aprobar.
                </p>
                <button
                  type="button"
                  onClick={() => durableEditor.load()}
                  className="mt-5 inline-flex h-11 items-center justify-center rounded-xl bg-white px-5 text-sm font-semibold text-surface-0 transition-colors hover:bg-gray-100"
                >
                  Reintentar
                </button>
              </>
            ) : (
              <>
                <span className="mx-auto block h-8 w-8 animate-spin rounded-full border-2 border-brand/30 border-t-brand-light" aria-hidden="true" />
                <h3 className="mt-4 text-base font-semibold text-white">Preparando el editor…</h3>
                <p className="mt-2 text-sm text-ink-secondary">Cargando la última versión guardada.</p>
              </>
            )}
          </div>
        </div>,
        document.body,
      )}
      {/* Hidden audio element drives playback. */}
      {audioUrl && (
        <audio
          ref={audioRef}
          src={audioUrl}
          onTimeUpdate={(e) => {
            const time = e.currentTarget.currentTime;
            playbackTimeRef.current = time;
            if (e.currentTarget.paused) {
              lastPublishedTimeRef.current = time;
              setCurrentTime(time);
            }
          }}
          onLoadedMetadata={(e) => setDuration(e.currentTarget.duration || 0)}
          onPlay={() => setIsPlaying(true)}
          onPause={(e) => {
            const time = e.currentTarget.currentTime;
            playbackTimeRef.current = time;
            lastPublishedTimeRef.current = time;
            setCurrentTime(time);
            setIsPlaying(false);
          }}
          onEnded={(e) => {
            const time = e.currentTarget.currentTime;
            playbackTimeRef.current = time;
            lastPublishedTimeRef.current = time;
            setCurrentTime(time);
            setIsPlaying(false);
          }}
          onError={() => {
            setAudioError(true);
            setIsPlaying(false);
          }}
        />
      )}

      {/* Header: back + title (non-sticky). The primary CTA is a FIXED
          floating button (below) so it can never be hidden behind the
          app's own sticky top bar — the recurring "botón cortado". */}
      <div className="py-3 mb-4 flex items-center gap-3">
        <button onClick={handleBackSafely}
          aria-label="Volver"
          className="w-9 h-9 rounded-xl bg-surface-2/40 ring-1 ring-white/[0.04] hover:ring-white/[0.08] hover:text-white flex items-center justify-center text-gray-400 transition-colors shrink-0">
          <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
            <path d="M19 12H5M12 19l-7-7 7-7" />
          </svg>
        </button>
        <div className="min-w-0">
          <h2 className="text-lg font-bold tracking-tight">{t("editor.title")}</h2>
          <p className="text-sm text-gray-200 truncate">
            {name}
            {batchProgress && <span className="ml-2 text-brand-light text-xs">({batchProgress})</span>}
          </p>
        </div>
        {collaboratingUser && (
          <div className="ml-auto hidden max-w-xs items-center gap-2 rounded-xl bg-cyan-400/[0.08] px-3 py-2 text-cyan-100 ring-1 ring-cyan-300/20 sm:flex" aria-live="polite">
            <span className="relative flex h-2 w-2 shrink-0" aria-hidden="true">
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-cyan-300 opacity-50" />
              <span className="relative inline-flex h-2 w-2 rounded-full bg-cyan-300" />
            </span>
            <span className="truncate text-[11px]">
              <strong className="font-semibold">{collaboratingUser.username || "Alguien del equipo"}</strong> está editando
            </span>
          </div>
        )}
      </div>

      {/* El status del pre-gen del fondo ("Fondo listo" / "Generando…") ya
          NO es un pill propio (2026-07 rediseño): se funde en la línea de
          confianza muted debajo del player bar (ver confidenceText). */}

      {/* Docked primary CTA — a fixed bottom action BAR, not a bare floating
          pill. The full-width gradient scrim means lyric lines scroll BEHIND
          a solid edge instead of under a translucent button (kills the
          overlap the pill had over the last rows — UX review 2026-07-16),
          and the button still can never be cut by the app's sticky top bar
          (the recurring "botón cortado" this fixed-position was chosen to
          avoid). `pointer-events-none` on the scrim lets clicks reach the
          list in the transparent upper region; the button re-enables them.
          The container's pb-28 still reserves space so the last row clears
          the bar when scrolled to the end. */}
      <div className="fixed bottom-0 left-0 right-0 z-50 border-t border-white/[0.08] bg-surface-1/95 px-4 py-3 shadow-[0_-16px_50px_rgba(0,0,0,.28)] backdrop-blur-xl">
        <div className="mx-auto flex w-full max-w-[1800px] items-center justify-between gap-4">
          <div className="hidden min-w-0 sm:block">
            <p className={`text-[11px] font-medium ${saveStatus === "conflict" ? "text-amber-300" : saveStatus === "error" || saveStatus === "offline" ? "text-red-300" : "text-white"}`}>{saveStatusLabel}</p>
            <p className="mt-0.5 truncate text-[10px] text-ink-tertiary">{edited.length} líneas · {viewMode === "advanced" ? "timings revisados" : "texto revisado"}</p>
          </div>
          <button
            onClick={handleApprove}
            disabled={isApproving || (editorV2Enabled && (!durableHydrated || durableEditor.loading)) || saveStatus === "conflict" || saveErrorReason === "draft-corrupt"}
            aria-busy={isApproving}
            aria-label={isApproving
              ? (t("editor.applying_changes") || "Aplicando cambios…")
              : (submitLabel || (isBatch
                ? (t("editor.approve_next") || "Aprobar y continuar")
                : (t("editor.approve_generate") || "Aprobar y generar")))}
            data-tour="editor-approve-floating"
            className="editor-primary-cta ml-auto inline-flex h-11 items-center gap-2 rounded-xl bg-gradient-to-r from-brand to-brand-light px-5 text-sm font-semibold text-white shadow-xl shadow-brand/25 transition-transform hover:-translate-y-0.5 disabled:cursor-not-allowed disabled:opacity-60"
          >
            {isApproving
              ? (t("editor.applying_changes") || "Aplicando cambios…")
              : (submitLabel || (isBatch ? t("editor.approve_next") : t("editor.approve_generate")))}
            <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
              <path d="M5 12h14M12 5l7 7-7 7" />
            </svg>
          </button>
        </div>
      </div>

      {coverageWarning && (
        <div className="mb-4 rounded-2xl ring-1 ring-accent/25 bg-accent/[0.06] px-4 py-3 flex items-start gap-3">
          <svg className="w-5 h-5 text-accent flex-shrink-0 mt-0.5" fill="none" stroke="currentColor" strokeWidth="1.8" viewBox="0 0 24 24">
            <circle cx="12" cy="12" r="10" />
            <path d="M12 8v4M12 16h.01" strokeLinecap="round" />
          </svg>
          <p className="text-xs text-ink-secondary leading-relaxed">
            {t("editor.coverage_warning")}
          </p>
        </div>
      )}

      {/* El panel de auto-fix (correcciones automáticas) ya NO es un banner
          verde propio (2026-07 rediseño): se volvió el chip ghost "Aplicar
          corrección · N" de la fila de chips, con su detalle + Deshacer en
          un popover. Ver la fila de chips más abajo. */}

      {/* QA fix 2026-05-28 (audit P0 #74): banner persistente del estado
          autosave. En LIST view no había feedback visible cuando una
          edición de texto fallaba — operador veía "Guardado" del último
          drag del timeline y asumía que todo estaba ok, pero el último
          keystroke de texto había fallado silente. Banner rojo encima
          del audio bar lo hace imposible de perder.
          (Timeline view ya tiene el chip embedded en su header,
          LyricsTimeline.jsx:354+) */}
      {["error", "offline", "conflict"].includes(saveStatus) && (
        <div className={`mb-3 rounded-card px-4 py-3 flex items-center gap-3 animate-fade-in ring-1 ${saveStatus === "conflict" ? "bg-amber-500/10 ring-amber-500/30" : "bg-red-500/10 ring-red-500/30"}`}>
          <svg className={`w-4 h-4 shrink-0 ${saveStatus === "conflict" ? "text-amber-300" : "text-red-400"}`} fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
            <path d="M12 9v3.75m9-.75a9 9 0 11-18 0 9 9 0 0118 0zM12 15.75h.01" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          <div className="flex-1 min-w-0">
            <p className="text-[12px] text-red-300 font-medium">
              {(_SAVE_ERROR_COPY[saveErrorReason] || _SAVE_ERROR_COPY.server).short}
            </p>
            <p className="text-[10px] text-red-300/70 mt-0.5">
              {(_SAVE_ERROR_COPY[saveErrorReason] || _SAVE_ERROR_COPY.server).detail}
            </p>
          </div>
          <div className="flex shrink-0 gap-2">
            {saveErrorReason === "conflict" ? (
              <button
                type="button"
                onClick={() => {
                  if (editorV2Enabled) setConflictDialogOpen(true);
                  else if (onReloadServer) onReloadServer({ draftKey, storeKey: _storeKey });
                  else window.location.reload();
                }}
                className="text-[11px] text-white bg-brand hover:bg-brand-light rounded-lg px-3 py-1.5 transition-colors"
              >
                {editorV2Enabled ? "Resolver conflicto" : "Cargar versión del servidor"}
              </button>
            ) : saveErrorReason === "draft-corrupt" ? (
              <button
                type="button"
                onClick={() => {
                  try { if (draftKey) localStorage.removeItem(draftKey); } catch { /* best effort */ }
                  setSaveStatus("saved");
                  setSaveErrorReason(null);
                }}
                className="text-[11px] text-red-200 hover:text-white bg-red-500/20 hover:bg-red-500/30 rounded-lg px-3 py-1.5 transition-colors"
              >
                Descartar borrador local
              </button>
            ) : (
              <button
                type="button"
                onClick={() => flushPendingSave()}
                className="text-[11px] text-red-200 hover:text-white bg-red-500/20 hover:bg-red-500/30 rounded-lg px-3 py-1.5 transition-colors"
                title="Forzar reintento de guardado"
              >
                Reintentar
              </button>
            )}
          </div>
        </div>
      )}

      {/* ─── Audio control bar — sticky-ish above the lyrics list ───
          The "Activar modo Sync" entry used to live as its own banner
          below this player (2026-05-16 removed). Sync is a tool for
          adjusting timing, not an alert — putting it inline with the
          play controls groups it with what it modifies (the timeline)
          AND frees the primary purple CTA so "Aprobar y generar" in
          the parent header has no visual competitor. */}
      {/* Audio bar SIEMPRE visible — incluso si audioUrl no cargó. La parte
          de reproductor (play + scrub + timer) se condiciona internamente;
          el resto (toggle Lista/Timeline, Modo Enfoque, Modo Sync, HelpTip)
          tiene que estar visible siempre porque permite EDITAR TEXTO sin
          necesidad de audio. Hotfix 2026-05-30 — antes envolver todo en
          {audioUrl && ...} hacía que jobs con input_r2_key=null (migrados a
          mano, GC de R2 después de 30 d, etc.) perdieran acceso al toggle
          de vista y al editor de texto, sólo viendo una lista plana sin
          forma de cambiar la vista. */}
      {(() => { const _playerBar = (
        /* Phase B 2026-05-25: sticky para que el play/pause + scrub
           siempre estén accesibles mientras el operador scrollea la
           lista de líneas. top usa stickyHeaderTop (passed by parent)
           para clear el header superior si lo hay. backdrop-blur +
           bg semi-transparente para que el contenido scrolleado abajo
           se vea sutil debajo. z-20 sobre el contenido normal del editor.
           Cuando se portalea bajo el video (playerSlot), NO va sticky ni
           lleva el offset del header — vive estático debajo del preview. */
        <div
          className={`${playerSlot ? "relative z-30" : "mb-3 sticky z-20"} min-w-0 max-w-full backdrop-blur-md bg-surface-1/95 flex items-center gap-3 p-3 rounded-xl ring-1 ring-white/[0.04]`}
          style={playerSlot ? undefined : { top: stickyHeaderTop || 0 }}
          data-tour="editor-playbar"
        >
          {/* Reproductor + scrub bar: solo si hay audio. Sin audio mostramos
              un mensaje compacto avisando que el play/scrub no están y
              dejando los controles de vista intactos a la derecha. */}
          {audioUrl ? (<>
          <button
            onClick={togglePlay}
            className="w-10 h-10 rounded-full bg-brand hover:bg-brand-light text-white flex items-center justify-center transition-colors shrink-0"
            aria-label={isPlaying ? "Pausar" : "Reproducir"}
          >
            {isPlaying ? (
              <svg className="w-4 h-4" fill="currentColor" viewBox="0 0 24 24">
                <rect x="6" y="5" width="4" height="14" rx="1"/>
                <rect x="14" y="5" width="4" height="14" rx="1"/>
              </svg>
            ) : (
              <svg className="w-4 h-4 ml-0.5" fill="currentColor" viewBox="0 0 24 24">
                <path d="M8 5v14l11-7z"/>
              </svg>
            )}
          </button>
          <span className="text-xs text-ink-secondary tabular-nums shrink-0 w-10 text-right">
            {formatTime(currentTime)}
          </span>
          <button
            type="button"
            onClick={handleScrub}
            className="flex-1 h-1.5 bg-surface-3/60 rounded-full overflow-hidden cursor-pointer relative"
            aria-label="Buscar"
          >
            {/* `pointer-events-none` para que clicks siempre atraviesen al
                botón parent (sin esto, el div fill podía absorberlos durante
                el frame de transición). `transform: scaleX()` en vez de
                width animado: composited en GPU, no dispara reflow ni pelea
                contra el rAF loop que actualiza `currentTime` cada ~16 ms.
                Mismo pattern que el playhead fix de PR #348. */}
            <div
              className="h-full bg-gradient-to-r from-brand to-brand-light pointer-events-none origin-left"
              style={{ transform: `scaleX(${Math.min(1, Math.max(0, progressPct / 100))})` , width: "100%" }}
            />
          </button>
          <span className="text-xs text-gray-500 tabular-nums shrink-0 w-10">
            {formatTime(duration)}
          </span>
          </>) : audioLoading ? (
            /* Cargando: el padre todavía está trayendo la URL del audio.
               NO mostrar "no disponible" acá — es un falso alarma mientras
               el fetch (con reintentos) está en vuelo. Mismo ancho que el
               reproductor para no colapsar la fila. */
            <div className="min-w-0 flex-1 flex items-center gap-2 text-[11px] text-ink-secondary">
              <svg className="w-3.5 h-3.5 flex-shrink-0 animate-spin" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                <path d="M21 12a9 9 0 1 1-6.219-8.56" strokeLinecap="round" />
              </svg>
              <span className="truncate">
                {t("editor.audio_loading") || "Cargando audio…"}
              </span>
            </div>
          ) : (
            /* Sin audio (reintentos agotados / job sin input): ocupar el
               mismo espacio horizontal que el reproductor para que la fila
               no colapse y los controles de vista (a la derecha) queden en
               la misma posición que cuando hay audio. Esto preserva la
               memoria muscular del operador. */
            <div className="min-w-0 flex-1 flex items-center gap-2 text-[11px] text-amber-300/90">
              <svg className="w-3.5 h-3.5 flex-shrink-0" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                <path d="M12 9v4M12 17h.01" />
                <path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z" />
              </svg>
              <span className="truncate">
                {t("editor.audio_unavailable") ||
                  "Audio no disponible para reproducir — podés editar el texto igual."}
              </span>
            </div>
          )}
        </div>
      ); return playerSlot ? createPortal(_playerBar, playerSlot) : _playerBar; })()}

      {/* ─── Zona de controles — rediseño 2026-07 (spec de diseño) ───────
          Antes: 6 banners full-width apilados (bg pill, auto-fix verde,
          review verde, wrap2 ámbar, intro) = ruido, parecía todo roto con
          un sync excelente (0,13s mediana). Ahora, debajo del player bar:
          (1) línea de confianza muted sin caja, (2) fila de MÁX 2 chips
          [Aplicar corrección · N] + [Revisar · •N →], (3) disclosure
          "Ajustes del video (N) ▾" plegado. Ningún elemento usa ámbar
          salvo el punto del contador Revisar (match con las barritas de la
          lista). Contadores capean 99+. */}

      {/* (1) Línea de confianza — muted, text-xs, check teal, sin caja.
          Funde estado de sync + fondo. Trunca con ellipsis en narrow.
          pl-3 alinea su check verde con el del chip "Aplicar corrección"
          (que lo indenta su propio padding de contenedor) — sin esto los
          dos tics verdes quedaban desalineados (reporte Tomi 2026-07-16). */}
      {/* Se portalea al slot bajo el video (es estado del VIDEO, no de la
          letra) cuando el wizard pasa playerSlot; así la columna de la
          letra sube. En modal/inline queda acá. mt-2 separa del player bar
          cuando va portaleado. */}
      {(() => { const _conf = (
        <div className={`${playerSlot ? "mt-2" : ""} mb-1 pl-3 flex items-center gap-1.5 text-xs text-ink-secondary min-w-0`} data-testid="editor-confidence">
          <svg className="w-3.5 h-3.5 text-emerald-400 shrink-0" fill="none" stroke="currentColor" strokeWidth="2.4" viewBox="0 0 24 24">
            <polyline points="20 6 9 17 4 12" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          <span className="truncate">{confidenceText}</span>
        </div>
      ); return playerSlot ? createPortal(_conf, playerSlot) : _conf; })()}

      {/* (2) Fila de chips (máx 2). Cada chip = icono + verbo + número (no
          oraciones). Wrap en narrow. */}
      {(fixCount > 0 || showReviewBanner) && (
        <div className="mb-3 flex flex-wrap items-center gap-2" data-testid="editor-chip-row">
          {/* Chip ghost "Aplicar corrección · N": aplica inline; el caret ▾
              abre popover con el detalle (ver-diff) + Deshacer. N=0 → no
              renderiza. */}
          {fixCount > 0 && (
            <div className="relative inline-flex">
              <button
                type="button"
                onClick={applyAllFixes}
                data-testid="apply-fix-chip"
                title={t("editor.autofix_apply_all_short") || "Aplicar todo"}
                className="inline-flex items-center gap-1.5 h-8 pl-3 pr-2.5 rounded-l-lg text-[12px] font-medium
                  bg-white/[0.04] ring-1 ring-white/[0.08] text-gray-200 hover:bg-white/[0.08] hover:text-white transition-colors"
              >
                <svg className="w-3.5 h-3.5 text-emerald-400 shrink-0" fill="none" stroke="currentColor" strokeWidth="2.4" viewBox="0 0 24 24">
                  <polyline points="20 6 9 17 4 12" strokeLinecap="round" strokeLinejoin="round" />
                </svg>
                <span>{t("editor.chip_apply_fix") || "Aplicar corrección"}</span>
                <span className="text-gray-500">·</span>
                <span className="tabular-nums">{cap99(fixCount)}</span>
              </button>
              <button
                type="button"
                onClick={() => setFixPopoverOpen((v) => !v)}
                data-testid="apply-fix-caret"
                aria-haspopup="menu"
                aria-expanded={fixPopoverOpen}
                title="Ver cambios y deshacer"
                aria-label="Ver cambios y deshacer"
                className="inline-flex items-center h-8 px-1.5 -ml-px rounded-r-lg
                  bg-white/[0.04] ring-1 ring-white/[0.08] text-gray-400 hover:bg-white/[0.08] hover:text-white transition-colors"
              >
                <svg className={`w-3 h-3 transition-transform ${fixPopoverOpen ? "rotate-180" : ""}`} fill="none" stroke="currentColor" strokeWidth="2.4" viewBox="0 0 24 24">
                  <polyline points="6 9 12 15 18 9" strokeLinecap="round" strokeLinejoin="round" />
                </svg>
              </button>
              {fixPopoverOpen && (
                <>
                  <button
                    type="button"
                    aria-hidden="true"
                    tabIndex={-1}
                    onClick={() => setFixPopoverOpen(false)}
                    className="fixed inset-0 z-20 cursor-default"
                  />
                  <div
                    role="menu"
                    data-testid="apply-fix-popover"
                    className="absolute left-0 top-full mt-1.5 z-30 w-64 p-2 rounded-xl bg-surface-1 ring-1 ring-white/[0.08] shadow-2xl shadow-black/40 animate-fade-in"
                  >
                    <ul className="space-y-1 px-1 py-0.5">
                      {splitAvailable && (
                        <li className="text-[11px] text-ink-secondary flex items-center gap-2">
                          <span className="text-gray-600 font-mono text-[10px]">└</span>
                          {(t("editor.autofix_split") || "Auto-dividir {n} líneas mergeadas").replace("{n}", mergeableSegments.length)}
                        </li>
                      )}
                      {hasSuggestions && (
                        <li className="text-[11px] text-ink-secondary flex items-center gap-2">
                          <span className="text-gray-600 font-mono text-[10px]">└</span>
                          {(t("editor.autofix_suggestions") || "Aplicar {n} sugerencias ortográficas").replace("{n}", pendingSuggestions)}
                        </li>
                      )}
                      {trimAvailable && (
                        <li className="text-[11px] text-ink-secondary flex items-center gap-2">
                          <span className="text-gray-600 font-mono text-[10px]">└</span>
                          {(t("editor.autofix_trim") || "Recortar {n} líneas con texto colgado").replace("{n}", longSegCount)}
                        </li>
                      )}
                    </ul>
                    {hasUndo && (
                      <div className="mt-1.5 pt-1.5 border-t border-white/[0.06]">
                        <button
                          type="button"
                          role="menuitem"
                          onClick={() => { undoEdit(); setFixPopoverOpen(false); }}
                          title={t("editor.undo_hint") || "Cmd/Ctrl+Z"}
                          className="w-full flex items-center gap-2 px-2 py-1.5 rounded-md text-[11px] text-gray-300 hover:text-white hover:bg-white/[0.05] transition-colors"
                        >
                          <svg className="w-3 h-3 shrink-0" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                            <path d="M3 7v6h6M3 13a9 9 0 109-9" strokeLinecap="round" strokeLinejoin="round" />
                          </svg>
                          {t("editor.undo") || "Deshacer"}
                        </button>
                      </div>
                    )}
                  </div>
                </>
              )}
            </div>
          )}
          {/* Chip PRIMARIO "Revisar · •N →": navegador secuencial. Punto
              ámbar 6px en el número (único ámbar del header, match con las
              barritas de la lista). Al llegar a 0 no renderiza (auto-oculta). */}
          {showReviewBanner && (
            <button
              type="button"
              onClick={jumpToNextReview}
              data-testid="review-next-btn"
              title={t("editor.review_next_hint") || "Saltar a la siguiente línea para revisar"}
              className="inline-flex items-center gap-1.5 h-8 pl-3 pr-2.5 rounded-lg text-[12px] font-semibold
                bg-brand/15 ring-1 ring-brand/40 text-brand-light hover:bg-brand/25 transition-colors animate-fade-in"
            >
              <span>{t("editor.review_next") || "Revisar"}</span>
              <span className="inline-flex items-center gap-1">
                <span className="text-brand-light/40">·</span>
                <span className="w-1.5 h-1.5 rounded-full bg-amber-400 shrink-0" aria-hidden="true" />
                <span className="tabular-nums">{cap99(reviewSegCount)}</span>
              </span>
              <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2.2" viewBox="0 0 24 24">
                <path d="M5 12h14M12 5l7 7-7 7" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </button>
          )}
        </div>
      )}

      {/* (3) Disclosure "Ajustes del video (N) ▾" — plegado. Concerns
          informativos del render (wrap a 2 renglones, intro instrumental).
          Neutral, SIN ámbar. Filas wrap a 2 líneas en narrow. */}
      {videoSettingsCount > 0 && (() => { const _vset = (
        <div className="mb-5 pl-3" data-testid="video-settings-disclosure">
          <button
            type="button"
            onClick={() => setVideoSettingsOpen((v) => !v)}
            aria-expanded={videoSettingsOpen}
            className="inline-flex items-center gap-1.5 text-xs text-ink-secondary hover:text-white transition-colors"
          >
            <span>{t("editor.video_settings") || "Ajustes del video"}</span>
            <span className="tabular-nums text-gray-500">({cap99(videoSettingsCount)})</span>
            <svg className={`w-3 h-3 transition-transform ${videoSettingsOpen ? "rotate-180" : ""}`} fill="none" stroke="currentColor" strokeWidth="2.4" viewBox="0 0 24 24">
              <polyline points="6 9 12 15 18 9" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </button>
          {videoSettingsOpen && (
            <div className="mt-2 space-y-2 animate-fade-in">
              {showWrap2Banner && (
                <div className="flex flex-wrap items-center justify-between gap-2 px-3 py-2 rounded-lg bg-white/[0.03] ring-1 ring-white/[0.06]">
                  <p className="text-xs text-ink-secondary flex-1 min-w-0">
                    {(t("editor.video_wrap2") || "2 renglones en el video · {n} líneas — se ven OK").replace("{n}", wrap2Count)}
                  </p>
                  <button
                    type="button"
                    onClick={() => { pushEditHistory(); wrap2SegIds.forEach((id) => splitSeg(id)); }}
                    className="shrink-0 text-[11px] font-medium px-2.5 py-1 rounded-md bg-white/[0.06] ring-1 ring-white/[0.08] text-gray-200 hover:bg-white/[0.1] hover:text-white transition-colors"
                  >
                    {t("editor.wrap2_banner_split_all") || "Dividir todas"}
                  </button>
                </div>
              )}
              {introLong && (
                <div className="flex flex-wrap items-center justify-between gap-2 px-3 py-2 rounded-lg bg-white/[0.03] ring-1 ring-white/[0.06]">
                  <p className="text-xs text-ink-secondary flex-1 min-w-0">
                    {(t("editor.video_intro") || "Intro instrumental · {s}s (arranca {t})")
                      .replace("{s}", Math.round(first.start))
                      .replace("{t}", formatTimestamp(first.start))}
                  </p>
                  <div className="flex items-center gap-2 shrink-0">
                    <button
                      type="button"
                      onClick={() => shiftAllSegments(-(first.start - 2))}
                      title={`Mover todas las líneas hacia atrás ${(first.start - 2).toFixed(1)}s — el primer lyric arrancará a los 2 s.`}
                      className="text-[11px] font-medium px-2.5 py-1 rounded-md bg-white/[0.06] ring-1 ring-white/[0.08] text-gray-200 hover:bg-white/[0.1] hover:text-white transition-colors"
                    >
                      {t("editor.intro_trim_to_2") || "Recortar a 2s"}
                    </button>
                    <button
                      type="button"
                      onClick={() => shiftAllSegments(-first.start)}
                      title={`Mover todas las líneas hacia atrás ${first.start.toFixed(1)}s — el primer lyric arrancará al segundo 0.`}
                      className="text-[11px] font-medium px-2.5 py-1 rounded-md text-gray-400 hover:text-white hover:bg-white/[0.05] transition-colors"
                    >
                      {t("editor.intro_trim_to_0") || "Empezar en 0s"}
                    </button>
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      ); return playerSlot ? createPortal(_vset, playerSlot) : _vset; })()}

      {audioUrl && syncMode && (
        <div className="mb-3 px-3 py-2 rounded-card bg-brand/[0.08] ring-1 ring-brand/40 animate-fade-in">
          {/* Top row: status + counter + exit. Compact, single line. */}
          <div className="flex items-center justify-between mb-1.5 gap-2">
            <div className="flex items-center gap-1.5 min-w-0">
              <span className="inline-block w-1.5 h-1.5 rounded-full bg-brand animate-pulse shrink-0" />
              <span className="text-[10px] font-semibold text-brand-light uppercase tracking-wider shrink-0">
                {t("editor.sync_mode_on") || "Sync"}
              </span>
              <span className="text-[10px] text-gray-500 tabular-nums shrink-0">
                {syncCursor + 1}/{edited.length}
              </span>
              <span className="hidden sm:inline text-[10px] text-gray-600 ml-2 truncate">
                <kbd className="px-1 py-0.5 rounded bg-surface-3/60 ring-1 ring-white/[0.05] font-mono text-[9px]">space</kbd>
                {" anclar · "}
                <kbd className="px-1 py-0.5 rounded bg-surface-3/60 ring-1 ring-white/[0.05] font-mono text-[9px]">Z</kbd>
                {" deshace"}
              </span>
            </div>
            <div className="flex items-center gap-2 shrink-0">
              <label className="flex items-center gap-1.5 text-[10px] text-gray-400 cursor-pointer select-none"
                title={t("editor.sync_cascade_hint") || "Cuando está activo, el delta de cada tap se aplica también a las líneas siguientes"}>
                <input
                  type="checkbox"
                  checked={syncCascade}
                  onChange={(e) => setSyncCascade(e.target.checked)}
                  className="w-3 h-3 accent-brand"
                />
                {t("editor.sync_cascade_label") || "Arrastrar siguientes"}
              </label>
              <button
                onClick={exitSyncMode}
                className="text-[10px] text-gray-400 hover:text-white px-1.5 py-0.5 transition-colors"
              >
                {t("editor.sync_exit") || "Salir"}
              </button>
            </div>
          </div>
          {/* Action row: line text on left (visual hero), compact button on right. */}
          <div className="flex items-center gap-2">
            <p className="flex-1 text-sm text-white font-medium leading-snug line-clamp-1 min-w-0">
              {edited[syncCursor]?.text || <span className="text-gray-500 italic">(sin texto)</span>}
            </p>
            <span className="text-[10px] font-mono text-brand-light tabular-nums shrink-0">
              {formatTime(currentTime)}
            </span>
            <button
              onClick={tapAnchor}
              className="shrink-0 h-8 px-3 rounded-lg bg-brand hover:bg-brand-light text-white text-caption
                font-semibold transition-colors flex items-center gap-1.5"
            >
              <svg className="w-3 h-3" fill="none" stroke="currentColor" strokeWidth="3" viewBox="0 0 24 24">
                <polyline points="20 6 9 17 4 12" />
              </svg>
              {t("editor.sync_tap") || "Anclar"}
            </button>
            <button
              onClick={undoLastAnchor}
              disabled={syncHistory.length === 0}
              title={t("editor.sync_undo_btn") || "Deshacer"}
              className="shrink-0 w-8 h-8 rounded-lg bg-surface-2/60 ring-1 ring-white/[0.05]
                text-gray-300 hover:text-white hover:bg-surface-2 disabled:opacity-30
                disabled:cursor-not-allowed transition-colors flex items-center justify-center"
            >
              <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                <path d="M3 7v6h6M3 13a9 9 0 109-9" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </button>
          </div>
        </div>
      )}


      {/* ─── Misaligned-first-line banner ───────────────────────────────
          Two signals merged into one banner:

          (a) Real instrumental intro: first lyric > 3 s into the audio.
              Offer to collapse it to 2 s or 0 s of pre-roll.

          (b) LRC author put line 1 at 0:00 even though there's a long
              instrumental intro before vocals start. Detected by an
              anomalously large gap between line 1 and line 2 — a chorus
              line typically follows ~8 s after the first verse line, so
              a 15+ s gap with line 1 at ~0:00 is a strong signal the
              author marked line 1 to "show through the intro" and the
              real vocal entry is roughly where line 2 starts. We offer
              to nudge line 1 only — leaves the rest of the timeline
              (which is correct relative to line 2) untouched. */}
      {(() => {
        if (syncMode || edited.length === 0) return null;
        const first = edited[0];
        const second = edited[1];

        // El caso "intro instrumental larga" (first.start > 3) YA no es un
        // banner propio (2026-07 rediseño): es una fila del disclosure
        // "Ajustes del video" (ver arriba). Acá queda sólo el patrón de
        // lrclib "línea 1 en 0:00" que es una sugerencia distinta.

        // Detect lrclib's "first line at 0:00" pattern: line 1 is near
        // t=0 but line 2 is suspiciously far away — usually an LRC
        // authoring quirk where the first line is anchored to song
        // start instead of the first vocal entry.
        if (first.start <= 1.0 && second && edited.length >= 4) {
          // Compute typical gap from lines 2..min(6) so a single odd
          // value doesn't skew the threshold.
          const gaps = [];
          for (let i = 1; i < Math.min(edited.length - 1, 6); i++) {
            gaps.push(edited[i + 1].start - edited[i].start);
          }
          const median = gaps.sort((a, b) => a - b)[Math.floor(gaps.length / 2)] || 0;
          const firstGap = second.start - first.start;
          // Trigger when line 1 → line 2 is meaningfully longer than
          // typical gap and the absolute gap is non-trivial. Threshold
          // is conservative so a normal song-with-no-intro doesn't
          // false-positive.
          if (median > 0 && firstGap > median * 2 && firstGap > 8) {
            const suggested = Math.max(0, second.start - median);
            const fixFirstOnly = () => {
              pushEditHistory();
              setEdited((prev) =>
                prev.map((s, i) => {
                  if (i !== 0) return s;
                  const segDur = Math.max(0.5, s.end - s.start);
                  let newEnd = suggested + segDur;
                  if (duration && newEnd > duration) newEnd = duration;
                  return { ...s, start: suggested, end: newEnd };
                }),
              );
            };
            // Stripe variant (2026-05-16): demoted from full amber fill
            // to a left-border accent + small text. The cause requires
            // operator decision but isn't a critical alert — it's one
            // suggestion among others. Reducing the visual weight stops
            // it from competing with the consolidated auto-fix panel
            // above.
            return (
              <div className="mb-2 pl-3 pr-3 py-1.5 border-l-2 border-amber-500/60 flex items-center gap-2 animate-fade-in">
                <p className="text-[11px] text-ink-secondary flex-1 leading-relaxed">
                  {t("editor.first_line_misaligned") ||
                    "La primera línea parece estar en 0:00 pero la canción arranca más tarde."}{" "}
                  <span className="text-gray-500">
                    {t("editor.first_line_misaligned_hint") || "¿Moverla a"}{" "}
                    <span className="font-mono text-amber-300/90">{formatTimestamp(suggested)}</span>?
                  </span>
                </p>
                <button
                  onClick={fixFirstOnly}
                  className="shrink-0 text-[10px] font-medium px-2 py-0.5 rounded text-amber-300 hover:text-amber-200
                    hover:bg-amber-500/10 transition-colors"
                >
                  {t("editor.first_line_fix") || "Mover sólo línea 1"}
                </button>
              </div>
            );
          }
        }

        return null;
      })()}

      {/* ─── Global timing offset ───────────────────────────────────
          Always-available panel for the common "the whole song is ±N ms
          off" case. Whisper's per-segment timestamps can drift by 200-
          800 ms (codec lag, intro silence, etc.); rather than nudging
          every line manually, the operator shifts the entire timeline.
          Collapsed by default — opens when user clicks the toggle.

          The bulk-trim button that used to live in this wrapper was
          moved (2026-05-16) into the consolidated auto-fix panel near
          the top of the editor so the operator sees ONE "system can
          fix N things" action instead of a standalone amber alert. */}
      {/* "Ajustes avanzados de timing" (global shift) ocultado: el ajuste
          fino por línea se resuelve en la timeline; el shift global casi no
          se usa y ensuciaba el flujo. */}
      <div className="hidden">
        <button
          onClick={() => setShiftPanelOpen((v) => !v)}
          className="w-full flex items-center justify-between px-3 py-2 rounded-card bg-surface-2/40 ring-1 ring-white/[0.04] hover:ring-white/[0.08] text-xs text-gray-300 hover:text-white transition-colors"
        >
          <span className="flex items-center gap-2">
            <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
              <path d="M8 7h12M8 12h12M8 17h12M4 7h.01M4 12h.01M4 17h.01" />
            </svg>
            {t("editor.shift_panel_title") || "Ajustes avanzados de timing"}
          </span>
          <svg
            className={`w-3.5 h-3.5 transition-transform ${shiftPanelOpen ? "rotate-180" : ""}`}
            fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24"
          ><path d="M19 9l-7 7-7-7" /></svg>
        </button>

        {shiftPanelOpen && (
          <div className="mt-2 px-3 py-3 rounded-card bg-surface-1/40 ring-1 ring-white/[0.04] space-y-3 animate-fade-in">
            <p className="text-[11px] text-gray-500 leading-relaxed">
              {t("editor.shift_panel_hint") ||
                "Aplica un offset uniforme a todas las líneas. Si la letra aparece tarde, usá valores negativos (anticipar). Si aparece antes de tiempo, positivos (atrasar). Drift típico de lyrics curadas: 100-200ms."}
            </p>

            {/* Slider continuo */}
            <div className="flex items-center gap-3">
              <span className="text-[10px] font-mono text-gray-500 w-12 text-right">-1000ms</span>
              <input
                type="range"
                min={-1000}
                max={1000}
                step={10}
                value={shiftDraftMs}
                onChange={(e) => setShiftDraftMs(parseInt(e.target.value, 10))}
                className="flex-1 accent-brand"
              />
              <span className="text-[10px] font-mono text-gray-500 w-12">+1000ms</span>
            </div>

            {/* Presets + valor actual + input custom. Granularidad fina
                para drift típico de lrclib synced (100-200ms) + presets
                más gruesos para mismatches mayores. */}
            <div className="flex flex-wrap items-center gap-2">
              {[-250, -150, -100, -50, 0, 50, 100, 150, 250].map((preset) => (
                <button
                  key={preset}
                  onClick={() => setShiftDraftMs(preset)}
                  className={`text-[11px] font-mono px-2.5 py-1 rounded ring-1 transition-colors ${
                    shiftDraftMs === preset
                      ? "bg-brand/20 ring-brand/40 text-brand-light"
                      : "bg-surface-2/40 ring-white/[0.05] text-gray-300 hover:text-white"
                  }`}
                >
                  {preset > 0 ? "+" : ""}{preset}ms
                </button>
              ))}
              <span className="text-[10px] text-gray-500">{t("editor.shift_or_custom") || "o"}</span>
              <input
                type="number"
                step={10}
                value={shiftDraftMs}
                onChange={(e) => {
                  const v = parseInt(e.target.value || "0", 10);
                  if (!Number.isNaN(v)) {
                    // clamp to slider range; users can still apply by
                    // calling repeatedly if they need bigger shifts.
                    setShiftDraftMs(Math.max(-1000, Math.min(1000, v)));
                  }
                }}
                className="w-20 text-[11px] font-mono px-2 py-1 rounded bg-surface-2/40 ring-1 ring-white/[0.05] text-white"
              />
              <span className="text-[10px] text-gray-500">ms</span>
              <button
                onClick={() => {
                  if (shiftDraftMs === 0) return;
                  const applied = shiftDraftMs;
                  shiftAllSegments(applied / 1000);  // ms → seconds
                  setAppliedShiftMs(applied);
                  setShiftDraftMs(0);
                }}
                disabled={shiftDraftMs === 0}
                className="ml-auto text-[11px] font-semibold px-3 py-1.5 rounded-lg bg-brand/20 ring-1 ring-brand/40 text-brand-light hover:bg-brand/30 transition-colors disabled:opacity-30 disabled:cursor-not-allowed"
              >
                {t("editor.shift_apply") || "Aplicar"}
              </button>
            </div>

            {/* Inline confirmation chip — clears after 2.5s. Without it
                the operator can't distinguish "applied" from "didn't
                register" because the slider returns to 0 on success. */}
            {appliedShiftMs != null && (
              <div className="flex items-center gap-2 text-[11px] text-emerald-300 animate-fade-in">
                <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2.5" viewBox="0 0 24 24">
                  <polyline points="20 6 9 17 4 12" strokeLinecap="round" strokeLinejoin="round" />
                </svg>
                <span className="font-mono">
                  {(t("editor.shift_applied") || "Aplicado: {n}ms")
                    .replace("{n}", appliedShiftMs > 0 ? `+${appliedShiftMs}` : appliedShiftMs)}
                </span>
                <span className="text-gray-500">·</span>
                <span className="text-gray-400">{t("editor.shift_applied_undo") || "Cmd/Ctrl+Z para revertir"}</span>
              </div>
            )}

            <p className="text-[10px] text-gray-600 leading-relaxed">
              {t("editor.shift_undo_hint") || "Deshacer con Cmd/Ctrl+Z o el botón de deshacer."}
            </p>
          </div>
        )}
      </div>

      {/* ─── Workspace UNIFICADO 2-col (2026-05-23 refactor world-class) ──
             Antes había dos workspaces enteros que se renderizaban según
             viewMode (timeline → grid; list → full-width). Ahora SIEMPRE
             es grid: izq sticky con controles+preview, der con lista o
             timeline según viewMode. Preview siempre visible, controles
             siempre en el mismo lugar.

             Phase 2 (2026-05-25): cuando el editor se monta dentro del
             paso 6 del wizard (hideTypographyControls=true), la columna
             izquierda no renderiza — los controles ya están en el paso
             4 del stepper y el preview central del wizard refleja los
             cambios. El grid colapsa a 1 columna full-width. */}
      <div className="relative mb-4 flex items-center gap-3 rounded-2xl bg-gradient-to-r from-surface-2/80 via-surface-2/45 to-brand/[0.055] p-2 ring-1 ring-white/[0.08] shadow-xl shadow-black/10" data-testid="editor-mode-explainer">
        <div
          className="grid min-w-0 flex-1 grid-cols-2 gap-1 rounded-xl bg-black/20 p-1"
          role="tablist"
          aria-label={t("editor.mode_label") || "Modo de edición"}
          onKeyDown={(event) => {
            if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
            event.preventDefault();
            const next = event.key === "ArrowLeft" || event.key === "Home" ? "basic" : "advanced";
            setViewMode(next);
            if (next === "basic") setSyncMode(false);
            event.currentTarget.querySelector(`[data-editor-view="${next}"]`)?.focus();
          }}
        >
          <button
            type="button"
            role="tab"
            aria-selected={viewMode === "basic"}
            tabIndex={viewMode === "basic" ? 0 : -1}
            data-editor-view="basic"
            onClick={() => { setViewMode("basic"); setSyncMode(false); setOverflowOpen(false); }}
            aria-label={t("editor.basic_view") || "Revisar letra"}
            className={`group flex min-w-0 items-center gap-2.5 rounded-lg px-3 py-2.5 text-left transition-all ${viewMode === "basic" ? "bg-white/[0.09] text-white ring-1 ring-white/[0.11] shadow-lg" : "text-ink-secondary hover:bg-white/[0.04] hover:text-white"}`}
          >
            <span className={`grid h-8 w-8 shrink-0 place-items-center rounded-lg ${viewMode === "basic" ? "bg-emerald-400/15 text-emerald-300" : "bg-white/[0.04] text-ink-tertiary"}`}>
              <svg className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.8" viewBox="0 0 24 24"><path d="M6 6h12M6 12h12M6 18h8" strokeLinecap="round" /></svg>
            </span>
            <span className="min-w-0">
              <span className="block truncate text-[12px] font-semibold">{t("editor.basic_view") || "Revisar letra"}</span>
              <span className="hidden truncate text-[10px] text-ink-tertiary sm:block">Corregir texto y aprobar</span>
            </span>
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={viewMode === "advanced"}
            tabIndex={viewMode === "advanced" ? 0 : -1}
            data-editor-view="advanced"
            onClick={() => { setViewMode("advanced"); setOverflowOpen(false); }}
            aria-label={t("editor.advanced_view") || "Ajustar tiempos"}
            className={`group flex min-w-0 items-center gap-2.5 rounded-lg px-3 py-2.5 text-left transition-all ${viewMode === "advanced" ? "bg-brand/20 text-white ring-1 ring-brand/35 shadow-lg shadow-brand/10" : "text-ink-secondary hover:bg-white/[0.04] hover:text-white"}`}
          >
            <span className={`grid h-8 w-8 shrink-0 place-items-center rounded-lg ${viewMode === "advanced" ? "bg-brand text-white shadow-lg shadow-brand/25" : "bg-white/[0.04] text-ink-tertiary"}`}>
              <svg className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.8" viewBox="0 0 24 24"><path d="M4 7h7v4H4zM13 13h7v4h-7z" /><path d="M4 3v18" strokeLinecap="round" /></svg>
            </span>
            <span className="min-w-0">
              <span className="block truncate text-[12px] font-semibold">{t("editor.advanced_view") || "Ajustar tiempos"}</span>
              <span className="hidden truncate text-[10px] text-ink-tertiary sm:block">Timeline y edición en grupo</span>
            </span>
          </button>
        </div>
        {viewMode === "advanced" && (
          <div className="relative shrink-0">
            <button
              type="button"
              data-testid="editor-overflow-btn"
              onClick={() => setOverflowOpen((value) => !value)}
              aria-haspopup="menu"
              aria-expanded={overflowOpen}
              className="inline-flex h-10 items-center gap-2 rounded-xl px-3 text-[11px] font-medium text-ink-secondary ring-1 ring-white/[0.09] transition-colors hover:bg-white/[0.06] hover:text-white"
            >
              <svg className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.8" viewBox="0 0 24 24"><path d="M4 7h16M7 12h10M10 17h4" strokeLinecap="round" /></svg>
              <span className="hidden md:inline">Herramientas</span>
            </button>
            {overflowOpen && (
              <>
                <button type="button" aria-hidden="true" tabIndex={-1} onClick={() => setOverflowOpen(false)} className="fixed inset-0 z-40 cursor-default" />
                <div role="menu" className="absolute right-0 top-full z-50 mt-2 w-64 overflow-hidden rounded-2xl bg-surface-1 p-1.5 ring-1 ring-white/[0.1] shadow-2xl shadow-black/50">
                  {!hideTypographyControls && (
                    <button type="button" role="menuitem" onClick={() => { setPreviewDockOpen((value) => !value); setOverflowOpen(false); }} className="w-full rounded-xl px-3 py-2.5 text-left text-[11px] text-ink-secondary hover:bg-white/[0.05] hover:text-white">
                      <span className="block font-medium">{previewDockOpen ? "Ocultar preview" : "Mostrar preview"}</span><span className="mt-0.5 block text-[10px] text-ink-tertiary">Abre una referencia visual lateral</span>
                    </button>
                  )}
                  <button type="button" role="menuitem" onClick={() => { toggleFocusMode(); setOverflowOpen(false); }} className="w-full rounded-xl px-3 py-2.5 text-left text-[11px] text-ink-secondary hover:bg-white/[0.05] hover:text-white">
                    <span className="block font-medium">{focusMode ? (t("editor.focus_exit") || "Salir de modo enfoque") : (t("editor.focus_enter") || "Trabajar a pantalla completa")}</span><span className="mt-0.5 block text-[10px] text-ink-tertiary">Maximiza el espacio de edición</span>
                  </button>
                  {canReanchor && !syncMode && (
                    <button type="button" role="menuitem" data-testid="reanchor-btn" disabled={reanchoring} onClick={() => { handleReanchor(); setOverflowOpen(false); }} className="w-full rounded-xl px-3 py-2.5 text-left text-[11px] text-ink-secondary hover:bg-white/[0.05] hover:text-white disabled:opacity-50">
                      <span className="block font-medium">{reanchoring ? (t("editor.reanchor_running") || "Re-sincronizando…") : (t("editor.reanchor") || "Re-sincronizar con IA")}</span><span className="mt-0.5 block text-[10px] text-ink-tertiary">Conserva los ajustes manuales</span>
                    </button>
                  )}
                  {!syncMode && (
                    <button type="button" role="menuitem" data-tour="editor-sync-entry" onClick={() => { enterSyncMode(); setOverflowOpen(false); }} className="w-full rounded-xl px-3 py-2.5 text-left text-[11px] text-ink-secondary hover:bg-white/[0.05] hover:text-white">
                      <span className="block font-medium">{t("editor.sync_enter_full") || "Re-anclar por tap (Modo Sync)"}</span><span className="mt-0.5 block text-[10px] text-ink-tertiary">Marcá entradas mientras escuchás</span>
                    </button>
                  )}
                  {editorV2Enabled && (
                    <button type="button" role="menuitem" onClick={() => { setHistoryOpen(true); setOverflowOpen(false); }} className="w-full rounded-xl px-3 py-2.5 text-left text-[11px] text-ink-secondary hover:bg-white/[0.05] hover:text-white">
                      <span className="block font-medium">Historial de versiones</span><span className="mt-0.5 block text-[10px] text-ink-tertiary">Restaurá un checkpoint sin perder el actual</span>
                    </button>
                  )}
                </div>
              </>
            )}
          </div>
        )}
      </div>
      <div className={`grid gap-4 mb-4 items-start ${viewMode === "advanced" ? (previewDockOpen && !hideTypographyControls && !hideInternalPreview ? "grid-cols-1 xl:grid-cols-[minmax(0,1fr)_320px]" : "grid-cols-1") : (hideTypographyControls || hideInternalPreview ? "grid-cols-1" : "grid-cols-1 lg:grid-cols-2")}`}>
          {/* COLUMNA IZQUIERDA — sticky en desktop. Controles tipográficos
              + LyricVideoPreview (editable) + scope toggle.
              Phase 2: oculta si hideTypographyControls=true (modo wizard). */}
          {!hideTypographyControls && !hideInternalPreview && (viewMode !== "advanced" || previewDockOpen) && (
          <div className={`space-y-2 lg:sticky lg:top-2 lg:self-start ${viewMode === "advanced" ? "xl:order-2" : ""}`}>
            {/* Live font switcher — preview re-renders in the chosen
                typeface instantly; applied to the render on re-render. */}
            <div className="flex items-center gap-2 px-1">
              <span className="text-[11px] text-ink-tertiary shrink-0">Tipografía</span>
              <select
                value={selectedFont}
                onChange={(e) => { setSelectedFont(e.target.value); onFontChange?.(e.target.value); }}
                className="flex-1 bg-surface-2 ring-1 ring-white/[0.08] rounded-md px-2 py-1.5 text-xs text-white focus:ring-brand outline-none cursor-pointer"
                style={{ fontFamily: FONT_CSS_BY_CODE[selectedFont] }}
                title="Probar otra tipografía — se ve en el preview al instante"
              >
                {EDITOR_FONTS.map((f) => (
                  <option key={f.code} value={f.code} style={{ fontFamily: f.css }}>{f.label}</option>
                ))}
              </select>
            </div>
            {/* Live text style: case + contrast + transition. Preview reflects
                case/contrast instantly; all three apply on re-render. */}
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 px-1 text-[11px]">
              <div className="flex items-center gap-1.5">
                <span className="text-ink-tertiary">Estilo</span>
                <div className="inline-flex rounded-md ring-1 ring-white/[0.08] overflow-hidden font-semibold">
                  {TEXT_CASES.map((o) => (
                    <button key={o.code} type="button"
                      onClick={() => { setSelectedCase(o.code); onCaseChange?.(o.code); }}
                      className={`px-2 py-1 transition-colors ${selectedCase === o.code ? "bg-brand text-white" : "text-ink-secondary hover:text-white"}`}>{o.label}</button>
                  ))}
                </div>
              </div>
              <div className="flex items-center gap-1.5">
                <span className="text-ink-tertiary">Contraste</span>
                <div className="inline-flex rounded-md ring-1 ring-white/[0.08] overflow-hidden font-semibold">
                  {CONTRASTS.map((o) => (
                    <button key={o.code} type="button"
                      onClick={() => { setSelectedContrast(o.code); onContrastChange?.(o.code); }}
                      className={`px-2 py-1 transition-colors ${selectedContrast === o.code ? "bg-brand text-white" : "text-ink-secondary hover:text-white"}`}>{o.label}</button>
                  ))}
                </div>
              </div>
              {/* Animación de letra (lyrics_animation) — libass templates. */}
              <div className="flex items-center gap-1.5">
                <span className="text-ink-tertiary">Animación</span>
                <select value={selectedAnimation}
                  onChange={(e) => { setSelectedAnimation(e.target.value); onAnimationChange?.(e.target.value); }}
                  className="bg-surface-2 ring-1 ring-white/[0.08] rounded-md px-1.5 py-1 text-white focus:ring-brand outline-none cursor-pointer">
                  {LYRICS_ANIMATIONS.map((o) => (<option key={o.code} value={o.code}>{o.label}</option>))}
                </select>
              </div>
              {/* Transición de línea (line_transition) — entrada slide/wipe/blur. */}
              <div className="flex items-center gap-1.5">
                <span className="text-ink-tertiary">Transición</span>
                <select value={selectedLineTransition}
                  onChange={(e) => { setSelectedLineTransition(e.target.value); onLineTransitionChange?.(e.target.value); }}
                  className="bg-surface-2 ring-1 ring-white/[0.08] rounded-md px-1.5 py-1 text-white focus:ring-brand outline-none cursor-pointer">
                  {LINE_TRANSITIONS.map((o) => (<option key={o.code} value={o.code}>{o.label}</option>))}
                </select>
              </div>
            </div>
            <div className="flex items-center justify-between px-1 gap-2 flex-wrap">
              <span className="text-[11px] text-ink-tertiary">Mover · escalar · rotar aplica a</span>
              <div className="inline-flex rounded-md ring-1 ring-white/[0.08] overflow-hidden text-[11px] font-semibold">
                <button type="button" onClick={() => setLayoutScope("all")}
                  className={`px-2.5 py-1 transition-colors ${layoutScope === "all" ? "bg-brand text-white" : "text-ink-secondary hover:text-white"}`}>
                  Todas las líneas
                </button>
                <button type="button" onClick={() => setLayoutScope("line")}
                  className={`px-2.5 py-1 transition-colors ${layoutScope === "line" ? "bg-brand text-white" : "text-ink-secondary hover:text-white"}`}>
                  Solo esta
                </button>
              </div>
            </div>
            <LyricVideoPreview
              t={t}
              segments={edited}
              currentTime={currentTime}
              isPlaying={isPlaying}
              backgroundUrl={previewBgUrl || null}
              backgroundStyle={backgroundStyle || "default"}
              font={FONT_CSS_BY_CODE[selectedFont] || undefined}
              textCase={selectedCase}
              textContrast={selectedContrast}
              // 2026-05-23: la prop `transition` (Corte/Fade) salió con el
              // deprecation de lyric_transition. Cuando el preview soporte
              // las animaciones libass nuevas se pasa por acá:
              //   lyricsAnimation={selectedAnimation}
              //   lineTransition={selectedLineTransition}
              fontScale={fontScale}
              onSelect={(id) => {
                focusSegment(id);
                const seg = edited.find((s) => s._id === id);
                if (seg) seekTo(Math.max(0, seg.start), false);
              }}
              onLayoutChange={handleLayoutChange}
              onDragStart={pushEditHistory}
            />
          </div>
          )}
          {/* COLUMNA DERECHA — scrollea independiente. Lista o timeline
              según viewMode. min-w-0 evita que rows muy largas rompan el grid.
              Phase E 2026-05-25: relative + el mini-map vertical se posiciona
              absolute a la derecha cuando hay >20 segments. */}
          <div className={`min-w-0 space-y-2 relative ${viewMode === "advanced" ? "xl:order-1" : ""}`}>
            {viewMode === "advanced" ? (
              <div
                data-testid="advanced-workspace-shell"
                className="min-h-[260px] rounded-2xl ring-1 ring-white/[0.08] bg-surface-2/20"
              >
                {audioLoading ? (
                  <div data-testid="advanced-audio-loading" className="flex min-h-[260px] flex-col items-center justify-center gap-3 px-6 text-center">
                    <svg className="h-6 w-6 animate-spin text-brand-light" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24" aria-hidden="true">
                      <path d="M21 12a9 9 0 1 1-6.219-8.56" strokeLinecap="round" />
                    </svg>
                    <div>
                      <p className="text-sm font-medium text-white">Cargando audio…</p>
                      <p className="mt-1 text-xs text-ink-tertiary">Ajustar tiempos aparecerá cuando el audio esté listo.</p>
                    </div>
                    <button
                      type="button"
                      onClick={() => { setViewMode("basic"); setSyncMode(false); }}
                      className="text-xs text-brand-light hover:text-white transition-colors"
                    >
                      Volver a Revisar letra
                    </button>
                  </div>
                ) : !audioUrl || audioError ? (
                  <div data-testid="advanced-audio-unavailable" className="flex min-h-[260px] flex-col items-center justify-center gap-3 px-6 text-center">
                    <svg className="h-7 w-7 text-amber-300" fill="none" stroke="currentColor" strokeWidth="1.7" viewBox="0 0 24 24" aria-hidden="true">
                      <path d="M12 9v4M12 17h.01" />
                      <path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z" />
                    </svg>
                    <div>
                      <p className="text-sm font-medium text-white">No se puede ajustar tiempos sin audio</p>
                      <p className="mt-1 max-w-md text-xs text-ink-tertiary">
                        Podés corregir el texto en Revisar letra. Cuando el audio esté disponible, volvé a Ajustar tiempos.
                      </p>
                    </div>
                    <button
                      type="button"
                      onClick={() => { setViewMode("basic"); setSyncMode(false); }}
                      className="rounded-lg bg-brand/15 px-3 py-2 text-xs font-medium text-brand-light ring-1 ring-brand/30 hover:bg-brand/25 hover:text-white transition-colors"
                    >
                      Volver a Revisar letra
                    </button>
                  </div>
                ) : (
                  <LyricsTimeline
                    segments={edited}
                    duration={duration}
                    currentTime={currentTime}
                    playbackTimeRef={playbackTimeRef}
                    isPlaying={isPlaying}
                    saveStatus={saveStatus}
                    activeId={activeId}
                    focusedSegId={focusedSegId}
                    highlightedIds={highlightedIds}
                    waveform={waveform}
                    gapS={MIN_GAP_S}
                    focusMode={workspaceFocusMode}
                    onSeek={(s) => seekTo(s, false)}
                    onDragStart={pushEditHistory}
                    onTimingChange={handleTimelineTimingChange}
                    onTimingChangeBatch={handleTimelineTimingChangeBatch}
                    onTextChange={updateText}
                    onDeleteSelection={deleteSegments}
                    onFocus={focusSegment}
                    onReset={resetTimings}
                    onSelectionCreated={({ count, method, durationMs }) => trackEditorEvent("editor_selection_created", {
                      count,
                      method,
                      duration_ms: Math.round(durationMs || 0),
                    })}
                  />
                )}
              </div>
            ) : (
              <>
                <p className="text-[11px] text-gray-200 mb-2 px-1">
                  {t("editor.list_hint") || "Click en un tiempo para reproducir desde ahí · doble click para editarlo"}
                </p>
                <div className="relative">
                  <div className="absolute bottom-0 left-0 right-0 h-12 bg-gradient-to-t from-surface to-transparent pointer-events-none z-10 rounded-b-2xl" />
                  {/* Phase E 2026-05-25: mini-map vertical en el borde derecho.
                      Cada segment se renderiza como un dot proporcional a su
                      duración. El activo brilla. Playhead horizontal según
                      currentTime. Click → seek a ese punto. Solo se renderiza
                      cuando hay >20 segments (canciones cortas no lo necesitan).
                      Sin esto, una canción de 80 líneas requiere scroll bruto
                      para localizar dónde está el operador. */}
                  {duration > 0 && edited.length > 20 && (
                    <button
                      type="button"
                      onClick={(e) => {
                        const rect = e.currentTarget.getBoundingClientRect();
                        const pct = (e.clientY - rect.top) / rect.height;
                        const seekT = Math.max(0, Math.min(duration, pct * duration));
                        seekTo(seekT, false);
                      }}
                      title={t("editor.minimap_hint") || "Mini-mapa: click para saltar al tiempo"}
                      aria-label={t("editor.minimap_hint") || "Mini-mapa"}
                      className="absolute right-0 top-0 bottom-0 w-2 z-20 group/mini cursor-pointer"
                      style={{ touchAction: "none" }}
                    >
                      {edited.map((seg) => {
                        const top = (seg.start / duration) * 100;
                        const height = Math.max(0.4, ((seg.end - seg.start) / duration) * 100);
                        const isActive = seg._id === activeId;
                        return (
                          <span
                            key={seg._id}
                            className={`absolute left-0 right-0 rounded-sm pointer-events-none transition-colors ${
                              isActive
                                ? "bg-brand shadow-[0_0_6px_rgba(109,74,255,0.7)]"
                                : "bg-white/10 group-hover/mini:bg-white/25"
                            }`}
                            style={{ top: `${top}%`, height: `${height}%` }}
                          />
                        );
                      })}
                      {duration > 0 && (
                        <span
                          className="absolute left-[-3px] right-[-3px] h-0.5 bg-brand-light pointer-events-none transition-[top] duration-150 ease-linear shadow-[0_0_8px_rgba(179,157,255,0.8)]"
                          style={{ top: `${Math.min(100, Math.max(0, (currentTime / duration) * 100))}%` }}
                        />
                      )}
                    </button>
                  )}
                  {/* Phase D 2026-05-25: gap entre rows reducido de 4px (space-y-1)
                      a 2px (space-y-0.5). En canciones largas de 60+ líneas
                      esto ahorra ~120px de scroll total. Y como el Phase B
                      compactó el header (auto-fix pill 32px), max-h ahora
                      puede crecer (100vh-200 vs 100vh-280 antes). */}
                  {/* QA fix 2026-05-28: el max-h vh-based servía cuando el
                      editor scrolleaba ADENTRO de su panel y el page-scroll
                      cubría todo. Con el nuevo wizard layout (PR
                      fix/wizard-scroll-viewport), el scroll context del
                      panel derecho vive en UploadZone (~línea 2147,
                      lg:overflow-y-auto h-full). Si dejamos el max-h acá
                      sobre lg, el operador ve DOBLE scroll: el inner cap
                      (acá) PLUS el outer (UploadZone). Resultado: scrollear
                      al final del inner deja contenido del outer abajo,
                      pero el mouse-wheel no transfiere → scroll trapped.
                      Mobile mantiene el cap original (no hay outer
                      overflow ahí, el page scroll cubre todo). */}
                  <div ref={listRef} className={`space-y-0.5 overflow-y-auto pr-1 pb-8 ${workspaceFocusMode ? "max-h-[calc(100vh-110px)]" : "max-h-[calc(100vh-200px)]"} lg:max-h-none lg:overflow-visible`}>
          {edited.map((seg, idx) => {
            const suggestion = suggestionsById[seg._id];
            const isApplied = suggestion && seg.text === suggestion;
            const isActive = seg._id === activeId;
            const isArmed = syncMode && idx === syncCursor;
            const isAnchored = syncMode && idx < syncCursor;
            // Recently anchored: ring highlights the row + per-row undo
            // button appears next to the timestamp. Auto-clears 10s after
            // the anchor (timer in tapAnchor's setHighlightedIds).
            const wasRecentlyAnchored = highlightedIds.has(seg._id);
            // Line the aligner inserted (Whisper missed it): timing is
            // interpolated, so flag it amber for the operator to verify.
            const isReview = !!seg.review;

            return (
              <div
                key={seg._id}
                ref={(el) => { rowRefs.current[seg._id] = el; }}
                {...(idx === 0 ? { "data-tour": "editor-list-row" } : {})}
                /* Phase A 2026-05-25: highlight prominente cuando es activo.
                   Antes: bg-brand/[0.07] ring-1 ring-brand/25 (invisible al
                   operador, ~7% opacity). Ahora: bg-brand/15 + left-bar
                   border-l-4 brand + glow shadow + key con activeId
                   dispara el pulse de la animación wlp-row-pulse al
                   transicionar a este row. */
                data-active={isActive && !isArmed ? "true" : "false"}
                className={`group rounded-xl transition-all
                  ${isArmed ? "bg-brand/[0.18] ring-2 ring-brand shadow-glow scale-[1.01]" : ""}
                  ${!isArmed && isActive ? "wlp-active-row bg-brand/15 border-l-4 border-brand pl-1 shadow-[0_0_24px_-8px_rgba(109,74,255,0.45)]"
                    /* Señal "review" CALMA (2026-07): antes un ring ámbar
                       completo alrededor de la tarjeta leía como "roto" con
                       11/26 líneas. Ahora sólo una barra de acento fina en el
                       borde izquierdo (mismo grosor que la fila activa para
                       no romper la grilla), en ámbar tenue. Sin fondo ni ring. */
                    : `border-l-4 ${!isArmed && !isActive && !wasRecentlyAnchored && isReview ? "border-amber-400/50" : "border-transparent"}`}
                  ${!isArmed && !isActive && wasRecentlyAnchored ? "bg-brand/[0.05] ring-1 ring-brand/40" : ""}
                  ${flashReviewId === seg._id ? "ring-1 ring-amber-400/50" : ""}
                  ${isAnchored ? "opacity-60" : ""}`}
              >
                <div className="flex items-start gap-2 p-1">
                  {editingId === seg._id ? (
                    <input
                      type="text"
                      aria-label={`Tiempo de inicio de la línea ${idx + 1}`}
                      autoFocus
                      value={editValue}
                      onChange={(e) => setEditValue(e.target.value)}
                      onBlur={() => commitEditTimestamp(seg)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") { e.preventDefault(); commitEditTimestamp(seg); }
                        else if (e.key === "Escape") { e.preventDefault(); cancelEditTimestamp(); }
                      }}
                      className="text-[11px] font-mono pt-2 w-14 shrink-0 text-right bg-surface-1
                        border border-brand/40 focus:border-brand outline-none rounded-md px-1
                        text-brand-light"
                    />
                  ) : (
                    <div className="flex items-center gap-1 shrink-0">
                      {/* sr-only hook so tests can enter sync mode at a specific row
                          without requiring hover state (jsdom has no hover). */}
                      <button
                        type="button"
                        data-testid={`sync-dot-${idx}`}
                        title="Activar Sync desde esta línea"
                        onClick={() => enterSyncModeAt(idx)}
                        className="sr-only"
                        aria-label="Activar Sync desde esta línea"
                      />
                      <button
                        onClick={() => seekTo(Math.max(0, seg.start), true)}
                        onDoubleClick={() => startEditTimestamp(seg)}
                        aria-label={`Reproducir desde ${formatTimestamp(seg.start)}. Doble click para editar el tiempo de la línea ${idx + 1}`}
                        title={t("editor.timestamp_hint") || "Click: ir al tiempo · Doble click: editar"}
                        className={`text-[11px] font-mono pt-2.5 w-14 text-right transition-colors
                          ${isActive ? "text-brand-light font-semibold"
                            : wasRecentlyAnchored ? "text-brand-light"
                            : isReview ? "text-amber-400/80 hover:text-amber-300"
                            : "text-gray-200 hover:text-brand-light"}`}
                      >
                        {/* Phase A 2026-05-25: indicador ▶ visible solo en
                            la fila activa para reforzar "esta es la que está
                            sonando ahora". El símbolo es half-width para no
                            empujar el timestamp ni romper la grilla. */}
                        {isActive && <span className="text-brand-light mr-0.5" aria-hidden="true">▶</span>}
                        {formatTimestamp(seg.start)}
                      </button>
                      {wasRecentlyAnchored && (
                        <button
                          type="button"
                          onClick={() => undoAnchorFor(seg._id)}
                          title={t("editor.undo_anchor_hint") || "Deshacer este anchor"}
                          className="mt-2 w-5 h-5 rounded-md text-[10px] text-ink-tertiary
                            hover:text-brand-light hover:bg-brand/10 transition-colors
                            flex items-center justify-center"
                          aria-label="Deshacer anchor"
                        >
                          {/* Counter-clockwise undo arrow */}
                          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
                               strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"
                               className="w-3 h-3">
                            <path d="M3 7v6h6" />
                            <path d="M3 13a9 9 0 1 0 3-7" />
                          </svg>
                        </button>
                      )}
                    </div>
                  )}
                  <div className="flex-1 min-w-0 relative">
                    <input
                      type="text"
                      aria-label={`Letra de la línea ${idx + 1}`}
                      value={seg.text}
                      onChange={(e) => updateText(seg._id, e.target.value)}
                      onKeyDown={(e) => {
                        const el = e.currentTarget;
                        if (e.key === "Enter") {
                          // Split THIS line at the cursor, word-aware (keeps timing).
                          e.preventDefault();
                          const caret = el.selectionStart ?? el.value.length;
                          if (!el.value.slice(0, caret).trim() || !el.value.slice(caret).trim()) {
                            el.blur();
                            return;
                          }
                          splitSegAt(seg._id, caret);
                          el.blur();
                        } else if (
                          e.key === "Backspace" &&
                          el.selectionStart === 0 &&
                          el.selectionEnd === 0 &&
                          el.value === ""
                        ) {
                          // Backspace en una línea VACÍA → la une con la anterior
                          // (= elimina la línea vacía). Antes fusionaba con
                          // CUALQUIER Backspace en pos 0, así que al borrar la
                          // primera palabra la línea "desaparecía" sola (confuso,
                          // reporte 2026-07-01). Ahora borrar texto NUNCA fusiona;
                          // sólo una línea ya vacía lo hace. Merge explícito: botón.
                          const i = edited.findIndex((s) => s._id === seg._id);
                          if (i > 0) {
                            e.preventDefault();
                            mergeSeg(edited[i - 1]._id);
                          }
                        }
                      }}
                      onFocus={() => {
                        seekTo(seg.start, false);
                        setFocusedSegId(seg._id);
                        setTextEditStart({ id: seg._id, text: seg.text });
                      }}
                      onBlur={(e) => handleTextBlur(seg._id, e.target.value)}
                      /* Phase A 2026-05-25: cuando es active y no focused,
                         escondemos el texto del input (text-transparent +
                         caret-transparent) para que el overlay de karaoke
                         word-jump abajo sea el único texto visible. Al
                         clickear el input para editar, focusedSegId cambia
                         y el texto vuelve. */
                      className={`w-full px-3 py-2 rounded-xl bg-surface-1 border text-sm
                        focus:border-brand/40 focus:outline-none hover:border-white/[0.08] transition-all
                        text-white
                        ${suggestion && !isApplied ? "border-amber-500/20" : "border-white/[0.04]"}`}
                    />
                    {/* Phase A 2026-05-25: overlay karaoke word-jump (Apple
                        Music style). Solo visible cuando este segment es el
                        activo Y el operador no está editando. Las palabras
                        se renderizan como spans con scale + glow en la
                        palabra activa, dim en las futuras, neutral en las
                        ya pasadas. El avance usa los word-stamps REALES
                        (activeWordIndex) cuando existen — con el lead-in
                        (#801) la línea aparece 0.4s antes del canto y el
                        viejo reparto uniforme corría adelantado. */}
                    {isActive && focusedSegId !== seg._id && seg.text && (() => {
                      const words = seg.text.split(/(\s+)/);
                      const activeWordIdx = activeWordIndex(
                        seg.text, seg.words, seg.start, seg.end, currentTime);
                      let nonSpaceIdx = -1;
                      return (
                        <div
                          className="absolute inset-0 rounded-xl bg-surface-1 px-3 py-2 text-sm pointer-events-none whitespace-pre-wrap leading-[1.4]"
                          aria-hidden="true"
                          style={{ fontFeatureSettings: "normal" }}
                        >
                          {words.map((tok, i) => {
                            if (!/\S/.test(tok)) return <span key={i}>{tok}</span>;
                            nonSpaceIdx += 1;
                            const wActive = nonSpaceIdx === activeWordIdx;
                            const wPast = nonSpaceIdx < activeWordIdx;
                            return (
                              <span
                                key={i}
                                style={{
                                  display: "inline-block",
                                  transform: wActive ? "scale(1.08)" : "scale(1)",
                                  transformOrigin: "center bottom",
                                  color: wActive ? "#b39dff" : wPast ? "rgba(255,255,255,0.92)" : "rgba(255,255,255,0.55)",
                                  textShadow: wActive ? "0 0 14px rgba(109,74,255,0.65)" : "none",
                                  transition: "transform 140ms cubic-bezier(.2,1.4,.35,1), color 200ms ease, text-shadow 200ms ease",
                                }}
                              >
                                {tok}
                              </span>
                            );
                          })}
                        </div>
                      );
                    })()}
                    {/* Pill "revisar tiempo" per-línea ELIMINADO (2026-07):
                        la barra de acento izquierda + el timestamp en ámbar
                        ya señalan la línea sin saturar; el navegador
                        secuencial del banner ("Revisar →") lleva a cada una. */}
                    {propagationPrompt && propagationPrompt.id === seg._id && (
                      <div className="flex items-center gap-2 mt-1.5 px-3 py-2 rounded-xl
                        bg-brand/10 ring-1 ring-brand/30 text-xs text-white">
                        <span className="flex-1">
                          {(t("editor.repeat_prompt") || "Esta línea se repite en otras {n}. ¿Aplicar el cambio a todas?")
                            .replace("{n}", propagationPrompt.matchIds.length)}
                        </span>
                        <button
                          type="button"
                          onMouseDown={(e) => e.preventDefault()}
                          onClick={applyPropagation}
                          className="px-2.5 py-1 rounded-lg bg-brand text-white font-medium hover:bg-brand/80 transition-colors whitespace-nowrap"
                        >
                          {(t("editor.repeat_apply_all") || "Aplicar a todas ({n})")
                            .replace("{n}", propagationPrompt.matchIds.length)}
                        </button>
                        <button
                          type="button"
                          data-testid="repeat-only-this-btn"
                          onMouseDown={(e) => e.preventDefault()}
                          onClick={dismissPropagation}
                          className="px-2.5 py-1 rounded-lg bg-surface-2 text-white/70 hover:text-white transition-colors whitespace-nowrap"
                        >
                          {t("editor.repeat_only_this") || "Solo esta"}
                        </button>
                      </div>
                    )}
                    {/* Wrap indicator + split action. Suprimido per-line
                        cuando ≥3 segments tienen wrap a 2 líneas — en ese
                        caso un banner único arriba transmite la info y
                        ofrece bulk action. Los casos 3+ líneas (más
                        urgentes) siempre se muestran inline. */}
                    {(() => {
                      if (!(seg.text || "").trim()) return null;
                      const lines = linesForSeg(seg.text);
                      // Surface the split affordance on long SINGLE lines too: a
                      // 1-visual-line seg can still be too long to read/karaoke
                      // comfortably (operator-reported). ~34 chars ≈ where a
                      // lyric line gets unwieldy.
                      const longSingle = lines <= 1 && (seg.text || "").trim().length > 34;
                      if (lines <= 1 && !longSingle) return null;
                      if (lines === 2 && showWrap2Banner) return null;
                      return (
                        <div className="flex items-center gap-2 mt-1 ml-1">
                          {longSingle ? (
                            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full
                              bg-amber-500/10 text-amber-300 ring-1 ring-amber-500/25 text-[10px] font-medium">
                              ↔ línea larga
                            </span>
                          ) : lines === 2 ? (
                            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full
                              bg-amber-500/10 text-amber-300 ring-1 ring-amber-500/25 text-[10px] font-medium">
                              <span className="relative flex h-1.5 w-1.5">
                                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-amber-400 opacity-60"/>
                                <span className="relative inline-flex rounded-full h-1.5 w-1.5 bg-amber-400"/>
                              </span>
                              ⚠ 2 líneas
                            </span>
                          ) : (
                            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full
                              bg-red-500/10 text-red-300 ring-1 ring-red-500/25 text-[10px] font-medium">
                              ✗ {lines} líneas
                            </span>
                          )}
                          <button
                            onClick={() => splitSeg(seg._id)}
                            title="Divide en dos en el wrap (reparte el tiempo proporcionalmente). Tip: apretá Enter en el cursor para partir exactamente ahí conservando el timing por palabra."
                            className="text-[10px] text-brand hover:text-brand-light transition-colors
                              flex items-center gap-0.5 px-2 py-0.5 rounded-lg
                              bg-brand/5 hover:bg-brand/15 ring-1 ring-brand/20"
                          >
                            ✂ Dividir
                          </button>
                        </div>
                      );
                    })()}
                    {suggestion && !isApplied && (
                      <button onClick={() => applySuggestion(seg._id)}
                        className="flex items-center gap-1.5 mt-1 ml-1 px-2 py-1 rounded-lg
                          bg-accent/5 hover:bg-accent/15 text-accent/70 hover:text-accent
                          text-[11px] transition-all group/btn">
                        <svg className="w-3 h-3" fill="none" stroke="currentColor" strokeWidth="2.5" viewBox="0 0 24 24">
                          <polyline points="20 6 9 17 4 12"/>
                        </svg>
                        <span className="text-gray-500 group-hover/btn:text-accent transition-colors">
                          {suggestion}
                        </span>
                      </button>
                    )}
                  </div>
                  <div className="shrink-0 flex items-center gap-0.5 mt-0.5">
                    {/* Sync-entry per row ELIMINADO 2026-05-23 — único entry
                        point pasa a ser el botón global del playbar + Cmd+K. */}
                    <button onClick={() => duplicateSeg(seg._id)}
                      className="w-8 h-8 rounded-lg opacity-0 group-hover:opacity-100
                        hover:bg-brand/10 flex items-center justify-center text-gray-600
                        hover:text-brand-light transition-all"
                      title={t("editor.duplicate_line") || "Duplicar línea (útil para estribillos repetidos)"}>
                      <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                        <rect x="9" y="9" width="11" height="11" rx="1.5" />
                        <path d="M5 15V5a1 1 0 011-1h10" />
                      </svg>
                    </button>
                    <button onClick={() => insertLineAfter(idx)}
                      className="w-8 h-8 rounded-lg opacity-0 group-hover:opacity-100
                        hover:bg-brand/10 flex items-center justify-center text-gray-600
                        hover:text-brand-light transition-all"
                      title={t("editor.insert_line_below") || "Insertar línea acá (en el medio de la canción)"}>
                      <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                        <path d="M12 5v14M5 12h14" />
                      </svg>
                    </button>
                    {idx < edited.length - 1 && (
                      <button onClick={() => mergeSeg(seg._id)}
                        className="w-8 h-8 rounded-lg opacity-0 group-hover:opacity-100
                          hover:bg-brand/10 flex items-center justify-center text-gray-600
                          hover:text-brand-light transition-all"
                        title="Unir con la línea siguiente — conserva el sync (combina los tiempos por palabra). Atajo: Backspace en una línea vacía la une con la anterior.">
                        <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                          <path d="M7 8l5 5 5-5M7 16l5-5 5 5" />
                        </svg>
                      </button>
                    )}
                    {/* Per-row ✂ trim removed: redundant with the bulk
                        "Recortar N líneas con texto colgado · Aplicar" auto-fix
                        at the top, and timing is now handled in the timeline. */}
                    <button onClick={() => deleteSeg(seg._id)}
                      className="w-8 h-8 rounded-lg opacity-0 group-hover:opacity-100
                        hover:bg-red-500/10 flex items-center justify-center text-gray-600
                        hover:text-red-400 transition-all"
                      title="Eliminar línea">
                      <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                        <path d="M18 6L6 18M6 6l12 12" />
                      </svg>
                    </button>
                  </div>
                </div>
              </div>
            );
          })}
          <button
            data-tour="editor-add-line"
            onClick={addLineSmart}
            className="w-full mt-2 py-2.5 rounded-xl border border-dashed border-white/[0.08]
              hover:border-brand/40 hover:bg-brand/[0.04] text-white hover:text-brand-light
              text-caption transition-all flex items-center justify-center gap-1.5"
          >
            <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
              <path d="M12 5v14M5 12h14" />
            </svg>
            {t("editor.add_line") || "Agregar línea"}
          </button>
                  </div>
                </div>
              </>
            )}
          </div>
      </div>

      {/* Line-count + blank-line note. The primary CTA lives in the sticky
          header now (always reachable) — no duplicate button here. */}
      <div className="mt-4 flex items-center gap-2 min-w-0" data-tour="editor-approve">
        <span className="text-xs text-gray-200 shrink-0">
          {edited.length} {t("editor.lines")}
        </span>
        {blankCount > 0 && (
          <span className="text-[11px] text-amber-400 truncate">
            · {blankCount} {blankCount === 1 ? t("editor.blank_singular") || "línea en blanco" : t("editor.blank_plural") || "líneas en blanco"} —{" "}
            {t("editor.blanks_dropped") || "se omitirán"}
          </span>
        )}
      </div>

      {/* Inline preview list-only ELIMINADO 2026-05-23 — el refactor world-class
          deja el LyricVideoPreview siempre visible en la columna izquierda,
          en ambas vistas. Este bloque (que sólo aparecía en list mode cuando
          una fila tenía foco) era redundante y a veces divergía visualmente. */}

      <ConflictDialog
        conflict={conflictDialogOpen ? durableEditor.conflict : null}
        currentUserId={user?.id}
        onCancel={() => setConflictDialogOpen(false)}
        onUseServer={async () => {
          const result = await durableEditor.resolve("use_server");
          if (!result?.ok) return;
          const next = sanitizeSegments(result.document.segments || []);
          setEdited(reseedPreservingIds(editedRef.current, next));
          setIsDirty(false);
          setSaveStatus("saved");
          setSaveErrorReason(null);
          setConflictDialogOpen(false);
          trackEditorEvent("editor_conflict", { server_revision: result.document.revision, resolution: "use_server" });
          try { if (draftKey) localStorage.removeItem(draftKey); } catch { /* best effort */ }
        }}
        onSaveLocal={async () => {
          const result = await durableEditor.resolve("save_local_as_new");
          if (!result?.ok) return;
          const next = sanitizeSegments(result.document.segments || editedRef.current);
          setEdited(reseedPreservingIds(editedRef.current, next));
          setIsDirty(false);
          setSaveStatus("saved");
          setSaveErrorReason(null);
          setConflictDialogOpen(false);
          trackEditorEvent("editor_conflict", { server_revision: result.document.revision, resolution: "save_local_as_new" });
          try { if (draftKey) localStorage.removeItem(draftKey); } catch { /* best effort */ }
        }}
      />
      <WrapWarningDialog
        warning={wrapWarning}
        onReview={() => {
          const firstId = wrapWarning?.ids?.[0];
          setWrapWarning(null);
          if (firstId == null) return;
          setFocusedSegId(firstId);
          window.setTimeout(() => rowRefs.current[firstId]?.scrollIntoView?.({ block: "center", behavior: "smooth" }), 0);
        }}
        onAutoSplit={() => {
          wrapWarning?.ids?.forEach((id) => splitSeg(id));
          setWrapWarning(null);
        }}
        onApproveAnyway={() => {
          setWrapWarning(null);
          handleApprove({ skipWrapWarning: true });
        }}
      />
      <VersionHistory
        open={historyOpen}
        loadVersions={durableEditor.listVersions}
        onClose={() => setHistoryOpen(false)}
        onRestore={async (versionId) => {
          const result = await durableEditor.restoreVersion(versionId);
          if (!result?.ok) return;
          const next = sanitizeSegments(result.document.segments || []);
          setEdited(reseedPreservingIds(editedRef.current, next));
          setIsDirty(false);
          setSaveStatus("saved");
          setHistoryOpen(false);
          trackEditorEvent("editor_version_restored", { to_revision: result.document.revision });
        }}
      />
      <EditorTour user={user} viewMode={viewMode} />
    </div>
  );
}
