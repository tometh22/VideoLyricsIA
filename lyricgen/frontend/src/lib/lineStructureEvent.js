// Medición de "partir" y "unir" en el editor (evento
// `editor_line_structure_changed`). Sólo cuenta: no cambia cómo se parte ni
// cómo se une. La pregunta que responde es cuántas veces el corte o la unión
// pudo usar el timing real por palabra y cuántas cayó al reparto por
// caracteres, separado en "la línea no tenía palabras" y "tenía palabras pero
// ya no coinciden con el texto" (texto corregido después del alineado).

import { lineWords } from "./splitWords";

const hasWords = (seg) => Array.isArray(seg?.words) && seg.words.length > 0;

// `plan` es el resultado de planSegmentSplit (null = no se partió).
export function splitTimingMode(seg, plan) {
  if (!plan) return null;
  if (plan.timing === "words") return "words";
  return hasWords(seg) ? "stale_words" : "no_words";
}

export function mergeTimingMode(a, b) {
  // A side that lost `words` but whose clocks are recovered still merges with words.
  const aw = hasWords(a) || lineWords(a) != null;
  const bw = hasWords(b) || lineWords(b) != null;
  if (aw && bw) return "words";
  return aw || bw ? "one_side" : "no_words";
}

const durationMs = (start, end) => {
  const value = Math.round((Number(end) - Number(start)) * 1000);
  return Number.isFinite(value) && value > 0 ? Math.min(value, 86_400_000) : 0;
};

// Propiedades de un evento por acción del operador. `items` es una lista de
// `{ mode, start, end }` (una por línea afectada; las acciones masivas mandan
// varias en un solo evento). Devuelve null si nada cambió.
export function lineStructureProperties(structureOp, trigger, items) {
  const done = (items || []).filter((item) => item && item.mode);
  if (!done.length) return null;
  const countMode = (mode) => done.filter((item) => item.mode === mode).length;
  return {
    structure_op: structureOp,
    trigger,
    count: done.length,
    words_count: countMode("words"),
    stale_words_count: countMode("stale_words"),
    one_side_count: countMode("one_side"),
    no_words_count: countMode("no_words"),
    duration_ms: done.reduce((sum, item) => sum + durationMs(item.start, item.end), 0),
  };
}
