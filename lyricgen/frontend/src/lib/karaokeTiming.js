// Índice de la palabra "cantándose" en currentTime, para el karaoke del
// EDITOR (list view + WizardLivePreview). El video final (ass_render.py
// `_word_timings`) ya usa los word-stamps reales; el preview repartía la
// ventana de la línea en partes iguales — aceptable cuando la línea
// abrazaba el canto, pero desde el lead-in (#801: la línea aparece 0.4s
// ANTES del primer canto) y los finales sostenidos de CTC, el reparto
// uniforme corre adelantado (bug 05/07: "el karaoke va a destiempo").
//
// Contrato (espejo del guard de ass_render._word_timings):
// - `words` se usa sólo si es un array con EXACTAMENTE una entrada por
//   token visible del texto y starts numéricos — protege contra words
//   viejos tras una edición manual del texto.
// - Devuelve -1 mientras nadie cantó todavía (el lead-in: línea visible,
//   ninguna palabra iluminada) — igual que el render, donde la primera
//   palabra se enciende en SU start real, no en el start de la línea.
// - Sin words válidos: fallback al reparto uniforme histórico.

export function countTokens(text) {
  return (text || "").split(/\s+/).filter(Boolean).length;
}

function wordsUsable(words, nTokens) {
  return (
    Array.isArray(words) &&
    words.length === nTokens &&
    nTokens > 0 &&
    words.every((w) => w && Number.isFinite(Number(w.start)))
  );
}

export function activeWordIndex(text, words, segStart, segEnd, currentTime) {
  const n = countTokens(text);
  if (n === 0) return -1;
  const ct = Number(currentTime);

  if (wordsUsable(words, n)) {
    // Última palabra cuyo start real ya pasó. Antes de la primera: -1
    // (lead-in en pantalla, nada cantado aún).
    let idx = -1;
    for (let i = 0; i < n; i++) {
      if (ct >= Number(words[i].start)) idx = i;
      else break;
    }
    return idx;
  }

  // Fallback histórico: reparto uniforme de la ventana de la línea.
  const start = Number(segStart) || 0;
  const dur = Math.max(0.001, (Number(segEnd) || 0) - start);
  const wDur = dur / Math.max(1, n);
  const elapsed = Math.max(0, ct - start);
  return Math.min(n - 1, Math.max(0, Math.floor(elapsed / wDur)));
}
