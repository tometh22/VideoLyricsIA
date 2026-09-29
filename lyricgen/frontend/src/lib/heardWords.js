// Palabras que se escuchan en el audio y no están en la letra.
//
// El backend (heard_words.py) compara la letra guardada contra el testigo
// independiente y la transcripción original de la máquina, y devuelve cada
// tramo faltante con dónde va. Acá sólo se aplica la decisión del revisor
// sobre la letra en pantalla: "Agregar" o "No se canta". La decisión "No se
// canta" se guarda en la línea (`heard_dismissed`) y viaja con cada versión.

export function normToken(token) {
  return String(token || "")
    .toLowerCase()
    .normalize("NFKD")
    .replace(/\p{M}/gu, "")
    .replace(/[^\p{L}\p{N}]/gu, "");
}

function overlapDistance(segment, start, end) {
  const a = Number(segment.start);
  const b = Number(segment.end);
  if (a <= end && start <= b) return 0;
  return Math.min(Math.abs(start - b), Math.abs(a - end));
}

// La línea a la que pertenece la alerta en la letra ACTUAL de pantalla: por
// identidad estable si existe, si no la más cercana en el tiempo.
export function findAlertLineIndex(segments, alert) {
  if (!Array.isArray(segments) || segments.length === 0 || !alert) return -1;
  if (alert.line_segment_id) {
    const byId = segments.findIndex((s) => String(s.segment_id || "") === String(alert.line_segment_id));
    if (byId !== -1) return byId;
  }
  const start = Number(alert.start);
  const end = Number(alert.end);
  let best = -1;
  let bestDistance = Infinity;
  segments.forEach((segment, index) => {
    const distance = overlapDistance(segment, start, end);
    if (distance < bestDistance) {
      best = index;
      bestDistance = distance;
    }
  });
  return best;
}

function insertionIndex(tokens, alert) {
  const norms = tokens.map(normToken);
  if (alert.anchor_before) {
    const index = norms.lastIndexOf(alert.anchor_before);
    if (index !== -1) return index + 1;
  }
  if (alert.anchor_after) {
    const index = norms.indexOf(alert.anchor_after);
    if (index !== -1) return index;
  }
  const hinted = Number.isInteger(alert.insert_at_word) ? alert.insert_at_word : tokens.length;
  return Math.max(0, Math.min(tokens.length, hinted));
}

function capitalize(text) {
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : text;
}

// "Hace" pasa a "hace" cuando el "Que" que faltaba vuelve al principio, salvo
// que la palabra aparezca con mayúscula en medio de otra línea (un nombre).
function lowerIfCommonWord(word, segments) {
  if (!word || word !== capitalize(word.toLowerCase())) return word;
  const lower = word.toLowerCase();
  const properNoun = segments.some((segment) => String(segment.text || "")
    .split(/\s+/).slice(1).some((token) => token.replace(/[^\p{L}]/gu, "") === word.replace(/[^\p{L}]/gu, "")));
  return properNoun ? word : lower;
}

// Texto de la línea con lo faltante insertado donde se escuchó.
export function lineWithAlertInserted(segments, lineIndex, alert) {
  const line = segments[lineIndex];
  const tokens = String(line?.text || "").split(/\s+/).filter(Boolean);
  const words = String(alert.text || "").trim();
  if (!words) return String(line?.text || "");
  const at = insertionIndex(tokens, alert);
  if (at === 0 && tokens.length > 0) {
    return [capitalize(words), lowerIfCommonWord(tokens[0], segments), ...tokens.slice(1)].join(" ");
  }
  const inserted = at === 0 ? capitalize(words) : words;
  return [...tokens.slice(0, at), inserted, ...tokens.slice(at)].join(" ");
}

// Aplica "Agregar". `mint` crea la identidad de una línea nueva.
export function applyHeardWordsAlert(segments, alert, mint) {
  if (!Array.isArray(segments) || !alert) return segments;
  if (alert.action === "new_line" && alert.new_line) {
    const line = {
      ...mint(),
      start: Number(alert.new_line.start),
      end: Number(alert.new_line.end),
      text: capitalize(String(alert.text || "").trim()),
    };
    return [...segments, line].sort((a, b) => a.start - b.start);
  }
  const index = findAlertLineIndex(segments, alert);
  if (index === -1) return segments;
  const text = lineWithAlertInserted(segments, index, alert);
  return segments.map((segment, i) => (i === index ? { ...segment, text } : segment));
}

// Aplica "No se canta": la clave queda en la línea más cercana.
export function dismissHeardWordsAlert(segments, alert) {
  const index = findAlertLineIndex(segments, alert);
  if (index === -1 || !alert?.key) return segments;
  return segments.map((segment, i) => {
    if (i !== index) return segment;
    const keys = Array.isArray(segment.heard_dismissed) ? segment.heard_dismissed : [];
    return keys.includes(alert.key)
      ? segment
      : { ...segment, heard_dismissed: [...keys, alert.key] };
  });
}

// Vista previa de "Agregar" para decidir sin tocar nada.
export function previewHeardWordsAlert(segments, alert) {
  if (alert?.action === "new_line") {
    return { kind: "new_line", after: capitalize(String(alert.text || "").trim()) };
  }
  const index = findAlertLineIndex(segments, alert);
  if (index === -1) return { kind: "insert", before: "", after: String(alert?.text || "") };
  return {
    kind: "insert",
    before: String(segments[index].text || ""),
    after: lineWithAlertInserted(segments, index, alert),
  };
}
