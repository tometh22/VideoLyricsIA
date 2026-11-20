// Place only the edited occurrence. Neighbouring lyrics keep their timings.
// With no silent gap the estimate overlaps the predecessor; it is explicitly
// unvalidated so the operator can listen and set its real sung boundary.
export function placeLineAt(segments, line, index, duration = 0) {
  const rest = segments.filter(row => row._id !== line._id);
  const at = Math.max(0, Math.min(index, rest.length));
  const previous = rest[at - 1];
  const next = rest[at];
  const ceiling = next ? next.start : (duration > 0 ? duration : (previous?.end ?? 0) + 3);
  const floor = previous?.start ?? 0;
  const gapStart = previous?.end ?? 0;
  const available = ceiling - gapStart;
  let start = available >= 0.02
    ? gapStart + Math.min(0.1, available / 3)
    : floor + Math.max(0, ceiling - floor) / 2;
  if (duration > 0) start = Math.min(start, Math.max(0, duration - 0.01));
  start = Math.max(0, start);
  const end = Math.max(start + 0.01, Math.min(start + 1, ceiling));
  const { words: _words, locked: _locked, ...fields } = line;
  const placed = {
    ...fields, start: Math.round(start * 10000) / 10000,
    end: Math.round(end * 10000) / 10000, review: true,
    timing_validation: { status: "unvalidated", reason: "estimated_placement" },
  };
  return [...rest.slice(0, at), placed, ...rest.slice(at)];
}
