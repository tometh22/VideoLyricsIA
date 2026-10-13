// Revisión rápida de la letra (backend: lyric_review.py).
//
// El servidor devuelve cada punto con su arreglo; acá sólo se aplica sobre la
// letra que está en pantalla. Los arreglos se ubican por identidad de línea y
// por palabras (no por posición de caracteres), así siguen funcionando si el
// revisor tocó la línea después del último guardado. Si la línea ya no es la
// que vio el servidor, el arreglo NO se aplica (devuelve 0 aplicados) y el
// editor avisa: nunca se corrige a ciegas otra línea.
import { findAlertLineIndex, lineWithAlertInserted, normToken } from "./heardWords";

const nfc = (value) => String(value || "").normalize("NFC");
const keyText = (value) => nfc(value).split(/\s+/).map(normToken).filter(Boolean).join(" ");

function splitPunct(raw) {
  const match = /^([^\p{L}\p{N}]*)(.*?)([^\p{L}\p{N}']*)$/u.exec(raw);
  return match ? [match[1], match[2], match[3]] : ["", raw, ""];
}

// Mismo algoritmo que lyric_review.replace_words: por palabras, con los signos
// de la pantalla. scope "one" cambia sólo la aparición en `atWord` (o la
// primera): en "Te quiero, te quiero" corregir una no toca la otra.
export function replaceWords(text, find, replacement, { atWord = null, scope = "all" } = {}) {
  const tokens = nfc(text).split(/\s+/).filter(Boolean);
  const target = nfc(find).split(/\s+/).map(normToken).filter(Boolean);
  if (!target.length) return null;
  const core = splitPunct(nfc(replacement).trim())[1];
  const words = tokens.map((tok, i) => [i, normToken(tok)]).filter(([, t]) => t);
  const starts = [];
  for (let k = 0; k + target.length <= words.length; k += 1) {
    if (target.every((t, j) => words[k + j][1] === t)) starts.push(k);
  }
  if (!starts.length) return null;
  const chosen = scope === "one"
    ? (starts.filter((k) => words[k][0] === atWord).slice(0, 1).length
      ? starts.filter((k) => words[k][0] === atWord).slice(0, 1) : starts.slice(0, 1))
    : starts;
  const spans = new Map(chosen.map((k) => [words[k][0], words[k + target.length - 1][0]]));
  const out = [];
  for (let i = 0; i < tokens.length; i += 1) {
    if (spans.has(i)) {
      const last = spans.get(i);
      out.push(`${splitPunct(tokens[i])[0]}${core}${splitPunct(tokens[last])[2]}`);
      i = last;
    } else {
      out.push(tokens[i]);
    }
  }
  return out.join(" ");
}

const ACCENTED = {
  cuando: "cuándo", como: "cómo", donde: "dónde", adonde: "adónde", que: "qué", quien: "quién",
  quienes: "quiénes", cual: "cuál", cuales: "cuáles", cuanto: "cuánto", cuanta: "cuánta",
  cuantos: "cuántos", cuantas: "cuántas",
};

// Mismo algoritmo que lyric_review.apply_punctuation: sólo sobre la frase marcada.
export function applyPunctuation(text, mode, clause = null) {
  const source = nfc(text);
  const target = clause && source.includes(nfc(clause)) ? nfc(clause) : source;
  const body = target.replace(/[¿?]/g, "").replace(/\s{2,}/g, " ").trim();
  let fixed = body;
  if (mode === "exclaim") fixed = `¡${body.replace(/^¡/, "").replace(/!$/, "")}!`;
  else if (mode === "accent_question") {
    const [first, ...rest] = body.split(" ");
    const [lead, core, trail] = splitPunct(first);
    const plain = normToken(core);
    let accented = ACCENTED[plain] || core;
    if (core[0] && core[0] === core[0].toUpperCase()) accented = accented[0].toUpperCase() + accented.slice(1);
    fixed = `¿${lead}${accented}${trail}${rest.length ? ` ${rest.join(" ")}` : ""}?`;
  }
  return target === source ? fixed : source.replace(target, fixed);
}

function lineIndexFor(segments, occurrence) {
  return findAlertLineIndex(segments, {
    line_segment_id: occurrence.line_segment_id,
    start: occurrence.start,
    end: occurrence.end,
  });
}

function joinWords(a, b) {
  const wa = Array.isArray(a.words) ? a.words : null;
  const wb = Array.isArray(b.words) ? b.words : null;
  return wa && wb ? [...wa, ...wb] : undefined;
}

// "Unir" con la línea que vio el servidor (por identidad), nunca con la que
// quedó al lado después de borrar otra.
function mergeLines(segments, index, fix) {
  if (fix.expect && keyText(segments[index].text) !== fix.expect) return null;
  let other = fix.direction === "previous" ? index - 1 : index + 1;
  if (fix.other_segment_id) {
    other = segments.findIndex((s) => String(s.segment_id || "") === String(fix.other_segment_id));
  }
  if (other < 0 || other >= segments.length || Math.abs(other - index) !== 1) return null;
  const first = Math.min(index, other);
  const a = segments[first];
  const b = segments[first + 1];
  const merged = {
    ...a,
    text: `${String(a.text || "").trim()} ${String(b.text || "").trim()}`.trim(),
    start: Math.min(Number(a.start), Number(b.start)),
    end: Math.max(Number(a.end), Number(b.end)),
  };
  const words = joinWords(a, b);
  if (words) merged.words = words; else delete merged.words;
  const dismissed = [...new Set([...(a.qa_dismissed || []), ...(b.qa_dismissed || [])])];
  if (dismissed.length) merged.qa_dismissed = dismissed; else delete merged.qa_dismissed;
  return [...segments.slice(0, first), merged, ...segments.slice(first + 2)];
}

function relayout(segments, fix) {
  const lines = fix.lines || [];
  const indexes = lines.map((line) => segments.findIndex(
    (s) => String(s.segment_id || "") === String(line.segment_id || "")));
  if (indexes.some((i) => i === -1)) return null;
  if (lines.some((line, k) => line.expect && keyText(segments[indexes[k]].text) !== line.expect)) return null;
  return segments.map((segment, i) => {
    const k = indexes.indexOf(i);
    // Las palabras cambian de línea: los tiempos por palabra ya no sirven.
    return k === -1 ? segment : { ...segment, text: lines[k].text, words: undefined };
  });
}

function retime(segments, index, fix) {
  const line = segments[index];
  const prev = segments[index - 1];
  const next = segments[index + 1];
  const start = fix.start != null ? Math.max(Number(fix.start), prev ? Number(prev.end) + 0.05 : 0) : Number(line.start);
  const end = fix.end != null ? Math.min(Number(fix.end), next ? Number(next.start) - 0.05 : Infinity) : Number(line.end);
  if (!(end > start)) return null;
  return segments.map((s, i) => (i === index ? { ...s, start, end, locked: true } : s));
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
  if (fix.type === "relayout") return relayout(segments, fix);
  const index = lineIndexFor(segments, occurrence);
  if (index === -1) return null;
  if (fix.type === "merge") return mergeLines(segments, index, fix);
  if (fix.type === "timing") return retime(segments, index, fix);
  const line = segments[index];
  let text = null;
  if (fix.type === "replace") {
    text = replaceWords(line.text, fix.find, fix.replace, { atWord: fix.at_word, scope: fix.scope || "all" });
  } else if (fix.type === "punctuation") {
    text = applyPunctuation(line.text, fix.mode, fix.clause);
  } else if (fix.type === "insert") {
    text = lineWithAlertInserted(segments, index, fix);
  } else if (fix.type === "set_text") {
    text = fix.expect && keyText(line.text) !== fix.expect ? null : fix.text;
  }
  if (text == null) return null;
  if (text === line.text) return segments;
  return segments.map((segment, i) => (i === index ? { ...segment, text } : segment));
}

// Aplica el arreglo en todas las apariciones. `alternative` es la opción
// elegida cuando hay más de una. Devuelve { segments, applied, lines }.
export function applyReviewItem(segments, item, { mint, alternative = null } = {}) {
  let next = segments;
  let applied = 0;
  const lines = [];
  let nextId = segments.reduce((max, segment) => Math.max(max, Number(segment._id) || 0), -1) + 1;
  const minter = mint || (() => ({ _id: nextId++ }));
  for (const occurrence of item?.occurrences || []) {
    if (!occurrence?.fix) continue;
    const fix = alternative?.fix || (alternative ? { ...occurrence.fix, replace: alternative.replace } : occurrence.fix);
    const result = applyOccurrence(next, occurrence, fix, minter);
    if (result && result !== next) {
      applied += 1;
      const index = lineIndexFor(result, occurrence);
      if (index !== -1 && result[index]?._id != null) lines.push(result[index]._id);
      next = result;
    }
  }
  return { segments: next, applied, lines };
}

// "Está bien así": las claves quedan en la línea y viajan con la versión.
export function dismissReviewItem(segments, item) {
  const occurrence = (item?.occurrences || [])[0];
  if (!occurrence) return segments;
  let index = lineIndexFor(segments, occurrence);
  if (index === -1 && !occurrence.line_segment_id) index = 0;
  if (index === -1) {
    index = findAlertLineIndex(segments, { start: occurrence.start, end: occurrence.end });
  }
  if (index === -1) return segments;
  return segments.map((segment, i) => {
    if (i !== index) return segment;
    const keys = new Set(segment.qa_dismissed || []);
    for (const key of item.keys || []) keys.add(key);
    return { ...segment, qa_dismissed: [...keys] };
  });
}

// Diferencia palabra por palabra (con signos) para mostrar qué se agrega y
// qué se saca: [{text, op: "same" | "ins" | "del"}].
export function diffTokens(before, after) {
  const a = nfc(before).split(/\s+/).filter(Boolean);
  const b = nfc(after).split(/\s+/).filter(Boolean);
  const dp = Array.from({ length: a.length + 1 }, () => new Array(b.length + 1).fill(0));
  for (let i = a.length - 1; i >= 0; i -= 1) {
    for (let j = b.length - 1; j >= 0; j -= 1) {
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const out = [];
  let i = 0;
  let j = 0;
  while (i < a.length || j < b.length) {
    if (i < a.length && j < b.length && a[i] === b[j]) {
      out.push({ text: a[i], op: "same" }); i += 1; j += 1;
    } else if (i < a.length && (j >= b.length || dp[i + 1][j] >= dp[i][j + 1])) {
      // Primero lo que se saca, después lo que entra: "~~tus~~ tres".
      out.push({ text: a[i], op: "del" }); i += 1;
    } else {
      out.push({ text: b[j], op: "ins" }); j += 1;
    }
  }
  // Si lo único que cambia son signos ("¿Cuando vuelva?" → "Cuando vuelva"),
  // se marcan sólo los signos, no la frase entera.
  for (let k = 0; k < out.length; k += 1) {
    if (out[k].op !== "del") continue;
    let m = k;
    while (m < out.length && out[m].op === "del") m += 1;
    let n = m;
    while (n < out.length && out[n].op === "ins") n += 1;
    const dels = out.slice(k, m);
    const inss = out.slice(m, n);
    if (dels.length && dels.length === inss.length
      && dels.every((d, x) => normToken(d.text) && normToken(d.text) === normToken(inss[x].text))) {
      out.splice(k, n - k, ...inss.map((ins, x) => ({ text: ins.text, op: "punct", before: dels[x].text })));
      k += inss.length - 1;
    }
  }
  // Palabras seguidas con la misma operación van juntas ("dormite ya").
  return out.reduce((acc, part) => {
    const last = acc[acc.length - 1];
    if (last && last.op === part.op && part.op !== "punct") last.text += ` ${part.text}`;
    else acc.push({ ...part });
    return acc;
  }, []);
}

// Descarta datos incompletos de un servidor viejo o con error.
// Diferencia letra por letra entre dos versiones de la misma palabra que sólo
// cambian en signos: [{text, op: "same" | "ins" | "del"}].
export function punctuationDiff(before, after) {
  const out = [];
  let i = 0;
  let j = 0;
  while (i < before.length || j < after.length) {
    if (i < before.length && j < after.length && before[i] === after[j]) {
      out.push({ text: before[i], op: "same" }); i += 1; j += 1;
    } else if (i < before.length && !/[\p{L}\p{N}]/u.test(before[i])) {
      out.push({ text: before[i], op: "del" }); i += 1;
    } else if (j < after.length) {
      out.push({ text: after[j], op: "ins" }); j += 1;
    } else {
      out.push({ text: before[i], op: "del" }); i += 1;
    }
  }
  return out.reduce((acc, part) => {
    const last = acc[acc.length - 1];
    if (last && last.op === part.op) last.text += part.text;
    else acc.push({ ...part });
    return acc;
  }, []);
}

export function validItems(items) {
  return (Array.isArray(items) ? items : []).filter(
    (item) => item && item.id && Array.isArray(item.occurrences) && item.occurrences.length
      && item.occurrences.every((occ) => occ && occ.fix),
  );
}
