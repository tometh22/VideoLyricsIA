/**
 * Stable segment identity across reseeds.
 *
 * Rows in the editor are keyed by `_id`. Historically every reseed of the
 * `segments` prop re-assigned `_id` BY INDEX, so React saw all-new keys and
 * remounted every row — the amplifier behind the reseed-storm/freeze family
 * (P0 UMG: 59-line songs remounting 6-7×/s). The guards in LyricsEditor
 * suppress most spurious reseeds, but any reseed that does run must not
 * reassign identity wholesale.
 *
 * `reseedPreservingIds(current, incoming)` maps the incoming array to rows
 * that KEEP the `_id` of a value-matching row in `current` (same start/end
 * within EPSILON_S and same text — the same tolerance segmentsValuesEqual
 * uses), and mints fresh ids (max+1, collision-free by construction) only
 * for rows with no match. A pure echo therefore preserves every id → zero
 * remounts; a genuine external change (undo restore, another song) gets
 * fresh ids only where content actually changed.
 */

const EPSILON_S = 1e-3;

// Namespace aleatorio por carga de página: dos pestañas (o dos sesiones sobre
// el mismo job) no pueden acuñar el mismo id, y el prefijo no numérico hace
// imposible chocar con los ids derivados de índice/`_id` que `decorateSegments`
// le pone a las filas que llegan del backend sin `segment_id`.
const MINT_NAMESPACE = Math.random().toString(36).slice(2, 8);
let mintCounter = 0;

/**
 * Id estable para una fila NUEVA del editor.
 *
 * `segment_id` es la clave con la que el merge a tres puntas identifica cada
 * línea. Duplicar o dividir una fila con `{ ...seg }` le heredaba el id del
 * padre, y dos filas con la misma clave hacían que el merge devolviera N veces
 * la MISMA línea; el deduplicador de colisiones borraba después las copias
 * sobrantes y el operador perdía letra que nunca tocó (job f866cbcf0e49, UMG
 * Chile, 1-sep-2026: 44 líneas → 38, entre ellas 4 repeticiones del estribillo
 * que había duplicado a mano). Toda fila creada en el editor tiene que pasar
 * por acá.
 */
export function mintSegmentId() {
  mintCounter += 1;
  return `n${MINT_NAMESPACE}-${mintCounter}`;
}

function valueMatches(a, b) {
  if (Math.abs((Number(a.start) || 0) - (Number(b.start) || 0)) > EPSILON_S) return false;
  if (Math.abs((Number(a.end) || 0) - (Number(b.end) || 0)) > EPSILON_S) return false;
  return String(a.text || "") === String(b.text || "");
}

export function reseedPreservingIds(current, incoming) {
  const cur = Array.isArray(current) ? current : [];
  const inc = Array.isArray(incoming) ? incoming : [];
  const usedCurIdx = new Set();
  let maxId = cur.reduce((m, s) => Math.max(m, Number.isFinite(s?._id) ? s._id : -1), -1);

  return inc.map((seg) => {
    for (let i = 0; i < cur.length; i++) {
      if (usedCurIdx.has(i)) continue;
      if (valueMatches(cur[i], seg)) {
        usedCurIdx.add(i);
        return { ...seg, _id: cur[i]._id };
      }
    }
    maxId += 1;
    return { ...seg, _id: maxId };
  });
}
