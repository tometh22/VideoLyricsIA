// Sugerencias de corte/unión de la segmentación de pantalla
// (DISPLAY_SEGMENTATION_MODE=suggest, backend display_segmentation.suggestions).
//
// El servidor las calcula sobre las líneas guardadas y nunca las aplica. Acá
// se ubican sobre la letra que está en pantalla por identidad de línea y por
// TEXTO EXACTO: si el revisor ya tocó esa línea, la sugerencia deja de valer y
// no se muestra (nunca se corta a ciegas otra línea). Aplicar pasa por las
// mismas funciones que el corte/unión manual, que conservan el timing por
// palabra (lib/splitWords).
import { mergeLineRows, splitLineAtTokens } from "./splitWords";

const START_TOLERANCE_S = 0.05;

function sameIdentity(row, id) {
  if (id == null || row == null) return false;
  const own = row.segment_id ?? row.id ?? row._id;
  return own != null && String(own) === String(id);
}

function findRow(segments, item) {
  let index = segments.findIndex((row) => sameIdentity(row, item.segment_id));
  if (index === -1) {
    // Rows that arrive without segment_id are matched by exact text + start.
    index = segments.findIndex((row) => row?.text === item.text
      && Math.abs(Number(row.start) - Number(item.start)) <= START_TOLERANCE_S);
  }
  return index;
}

/**
 * Suggestions still valid for `segments`, each with the local row `_id`
 * (`rowId`) and, for merges, the following row's `_id` (`nextRowId`).
 */
export function resolveDisplaySuggestions(items, segments, dismissed = new Set()) {
  if (!Array.isArray(items) || !Array.isArray(segments)) return [];
  const out = [];
  for (const item of items) {
    if (!item || !item.id || dismissed.has(item.id)) continue;
    const index = findRow(segments, item);
    if (index === -1) continue;
    const row = segments[index];
    if (row.text !== item.text) continue;
    if (item.kind === "split") {
      if (!splitLineAtTokens(row, item.cuts)) continue;
      out.push({ ...item, rowId: row._id });
    } else if (item.kind === "merge") {
      const next = segments[index + 1];
      if (!next || next.text !== item.next_text) continue;
      if (item.next_segment_id != null && (next.segment_id ?? next.id) != null
        && !sameIdentity(next, item.next_segment_id)) continue;
      out.push({ ...item, rowId: row._id, nextRowId: next._id });
    }
  }
  return out;
}

/**
 * Apply resolved suggestions in one pass. `nextId` is the first free local
 * `_id`; `mintSegmentId` gives every new row its own durable identity (a
 * shared one makes the three-way merge duplicate and then delete lyrics).
 * Returns { segments, applied: [{ kind, start, end, words }] }.
 */
export function applyDisplaySuggestions(segments, resolved, { nextId, mintSegmentId }) {
  const byRow = new Map((resolved || []).map((item) => [item.rowId, item]));
  const out = [];
  const applied = [];
  let id = nextId;
  for (let i = 0; i < segments.length; i += 1) {
    const row = segments[i];
    const item = byRow.get(row._id);
    if (!item || row.text !== item.text) {
      out.push(row);
      continue;
    }
    if (item.kind === "split") {
      const parts = splitLineAtTokens(row, item.cuts);
      if (!parts) { out.push(row); continue; }
      const { words: _parentWords, ...base } = row;
      parts.forEach((part) => out.push({
        ...base, _id: id++, segment_id: mintSegmentId(),
        text: part.text, start: part.start, end: part.end,
        ...(part.words ? { words: part.words } : {}),
      }));
      applied.push({ kind: "split", row, words: parts.every((part) => part.words) });
      continue;
    }
    const next = segments[i + 1];
    if (item.kind === "merge" && next && next._id === item.nextRowId && next.text === item.next_text) {
      const merged = mergeLineRows(row, next);
      out.push(merged);
      applied.push({ kind: "merge", row, next, words: Array.isArray(merged.words) });
      i += 1;
      continue;
    }
    out.push(row);
  }
  return { segments: out, applied };
}
