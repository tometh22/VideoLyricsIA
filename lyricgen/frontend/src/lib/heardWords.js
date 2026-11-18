// Ubicar e insertar palabras en la letra en pantalla. Lo usa la revisión
// rápida (lyricReview.js) para "Agregar" lo que se escucha y no está.

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
    // Si la línea tiene identidad y ya no existe (se borró o se partió), el
    // arreglo NO se aplica en la vecina: se devuelve -1 y el editor avisa.
    return segments.findIndex((s) => String(s.segment_id || "") === String(alert.line_segment_id));
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
  const hinted = Number.isInteger(alert.insert_at_word) ? alert.insert_at_word : null;
  // La posición que calculó el servidor manda si la palabra de al lado sigue
  // ahí ("Dame, dame tu amor": no confundir con el otro "dame").
  if (hinted != null && hinted <= tokens.length) {
    if (alert.anchor_before && hinted > 0 && norms[hinted - 1] === alert.anchor_before) return hinted;
    if (alert.anchor_after && norms[hinted] === alert.anchor_after) return hinted;
    if (!alert.anchor_before && !alert.anchor_after) return hinted;
  }
  if (alert.anchor_before) {
    const index = norms.lastIndexOf(alert.anchor_before);
    if (index !== -1) return index + 1;
  }
  if (alert.anchor_after) {
    const index = norms.indexOf(alert.anchor_after);
    if (index !== -1) return index;
  }
  return Math.max(0, Math.min(tokens.length, hinted ?? tokens.length));
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
