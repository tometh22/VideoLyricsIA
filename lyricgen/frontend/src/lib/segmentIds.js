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
