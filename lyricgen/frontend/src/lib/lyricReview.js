// Revisión rápida de la letra (backend: lyric_review.py).
//
// El servidor devuelve cada punto con su arreglo; acá sólo se aplica sobre la
// letra que está en pantalla. Los arreglos se ubican por identidad de línea y
// por palabras (no por posición de caracteres), así siguen funcionando si el
// revisor tocó la línea después del último guardado.
import { findAlertLineIndex, lineWithAlertInserted, normToken } from "./heardWords";

const nfc = (value) => String(value || "").normalize("NFC");

function splitPunct(raw) {
  const match = /^([^\p{L}\p{N}]*)(.*?)([^\p{L}\p{N}']*)$/u.exec(raw);
  return match ? [match[1], match[2], match[3]] : ["", raw, ""];
}

// Reemplaza TODAS las apariciones de las palabras `find` en la línea,
// conservando los signos de afuera de la pantalla ("¿Cuando vuelva" →
// "¿Cuando vuelvas"). Devuelve null si ya no están.
export function replaceWords(text, find, replacement) {
  const tokens = nfc(text).split(/\s+/).filter(Boolean);
  const target = nfc(find).split(/\s+/).map(normToken).filter(Boolean);
  if (!target.length) return null;
  // Los signos de afuera son los de la pantalla actual (el revisor puede
  // haberlos cambiado después del guardado); la corrección sólo trae palabras.
  const core = splitPunct(nfc(replacement).trim())[1];
  const out = [];
  let changed = false;
  for (let i = 0; i < tokens.length;) {
    const window = tokens.slice(i, i + target.length);
    if (window.length === target.length && window.every((tok, k) => normToken(tok) === target[k])) {
      const lead = splitPunct(window[0])[0];
      const trail = splitPunct(window[window.length - 1])[2];
      out.push(`${lead}${core}${trail}`);
      i += target.length;
      changed = true;
    } else {
      out.push(tokens[i]);
      i += 1;
    }
  }
  if (!changed) {
    const raw = nfc(text);
    return raw.includes(nfc(find)) ? raw.split(nfc(find)).join(nfc(replacement)) : null;
  }
  return out.join(" ");
}

export function applyPunctuation(text, mode) {
  const body = nfc(text).replace(/[¿?]/g, "").replace(/\s{2,}/g, " ").trim();
  if (mode === "exclaim") return `¡${body.replace(/^¡/, "").replace(/!$/, "")}!`;
  return body;
}

function lineIndexFor(segments, occurrence) {
  return findAlertLineIndex(segments, {
    line_segment_id: occurrence.line_segment_id,
    start: occurrence.start,
    end: occurrence.end,
  });
}

function mergeLines(segments, index, direction) {
  const other = direction === "previous" ? index - 1 : index + 1;
  if (index < 0 || other < 0 || other >= segments.length) return segments;
  const first = Math.min(index, other);
  const a = segments[first];
  const b = segments[first + 1];
  const merged = {
    ...a,
    text: `${String(a.text || "").trim()} ${String(b.text || "").trim()}`.trim(),
    start: Math.min(Number(a.start), Number(b.start)),
    end: Math.max(Number(a.end), Number(b.end)),
    qa_dismissed: [...new Set([...(a.qa_dismissed || []), ...(b.qa_dismissed || [])])],
  };
  if (!merged.qa_dismissed.length) delete merged.qa_dismissed;
  return [...segments.slice(0, first), merged, ...segments.slice(first + 2)];
}

function applyOccurrence(segments, occurrence, fix, mint) {
  if (fix.type === "new_line") {
    return [...segments, {
      ...mint(),
      start: Number(fix.start),
      end: Number(fix.end),
      text: (fix.text || "").charAt(0).toUpperCase() + (fix.text || "").slice(1),
    }].sort((a, b) => a.start - b.start);
  }
  const index = lineIndexFor(segments, occurrence);
  if (index === -1) return null;
  if (fix.type === "merge") return mergeLines(segments, index, fix.direction);
  const line = segments[index];
  let text = null;
  if (fix.type === "replace") text = replaceWords(line.text, fix.find, fix.replace);
  else if (fix.type === "punctuation") text = applyPunctuation(line.text, fix.mode);
  else if (fix.type === "insert") text = lineWithAlertInserted(segments, index, fix);
  if (text == null || text === line.text) return text == null ? null : segments;
  return segments.map((segment, i) => (i === index ? { ...segment, text } : segment));
}

// Aplica el arreglo en todas las apariciones. `alternative` es la opción
// elegida cuando los oídos no coinciden. Devuelve { segments, applied }.
export function applyReviewItem(segments, item, { mint, alternative = null } = {}) {
  let next = segments;
  let applied = 0;
  let nextId = segments.reduce((max, segment) => Math.max(max, Number(segment._id) || 0), -1) + 1;
  const minter = mint || (() => ({ _id: nextId++ }));
  for (const occurrence of item.occurrences || []) {
    const fix = alternative ? { ...occurrence.fix, replace: alternative.replace } : occurrence.fix;
    const result = applyOccurrence(next, occurrence, fix, minter);
    if (result) {
      applied += result === next ? 0 : 1;
      next = result;
    }
  }
  return { segments: next, applied };
}

// "Está bien así": las claves quedan en la línea y viajan con la versión.
export function dismissReviewItem(segments, item) {
  const occurrence = (item.occurrences || [])[0];
  if (!occurrence) return segments;
  const index = lineIndexFor(segments, occurrence);
  if (index === -1) return segments;
  return segments.map((segment, i) => {
    if (i !== index) return segment;
    const keys = new Set(segment.qa_dismissed || []);
    for (const key of item.keys || []) keys.add(key);
    return { ...segment, qa_dismissed: [...keys] };
  });
}

// Qué cambia en la línea, palabra por palabra, para resaltarlo.
export function changedWords(before, after) {
  const was = new Map();
  for (const tok of nfc(before).split(/\s+/).map(normToken)) was.set(tok, (was.get(tok) || 0) + 1);
  return nfc(after).split(/\s+/).filter(Boolean).map((word) => {
    const tok = normToken(word);
    const count = was.get(tok) || 0;
    if (count > 0) {
      was.set(tok, count - 1);
      return { word, changed: false };
    }
    return { word, changed: Boolean(tok) };
  });
}
