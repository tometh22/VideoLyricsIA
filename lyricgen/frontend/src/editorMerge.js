// Three-way merge for lyric segments. The editor keeps `_id` local-only and
// persists `segment_id` so a correction in another row can be merged without
// treating the whole document as one conflicting blob.

export function decorateSegments(segments = []) {
  return segments.map((segment, index) => {
    const stable = segment?.segment_id ?? segment?.id ?? String(index);
    const localId = Number.isFinite(segment?._id) ? segment._id : index;
    return { ...segment, _id: localId, segment_id: String(stable) };
  });
}

export function persistedSegments(segments = []) {
  return segments.map(({ _id, ...segment }) => segment);
}

function comparable(segment) {
  if (segment == null) return null;
  const { _id, ...rest } = segment;
  return rest;
}

function equal(a, b) {
  return JSON.stringify(comparable(a)) === JSON.stringify(comparable(b));
}

function keyOf(segment, index) {
  return String(segment?.segment_id ?? segment?.id ?? `index:${index}`);
}

function indexed(segments) {
  const map = new Map();
  segments.forEach((segment, index) => map.set(keyOf(segment, index), segment));
  return map;
}

/**
 * Merge base → local and base → remote. A field/document is only considered
 * conflicting when both sides changed the same stable segment differently.
 */
export function mergeThreeWay(base = [], local = [], remote = []) {
  const baseMap = indexed(base);
  const localMap = indexed(local);
  const remoteMap = indexed(remote);
  const order = [];
  local.forEach((segment, index) => order.push(keyOf(segment, index)));
  remote.forEach((segment, index) => {
    const key = keyOf(segment, index);
    if (!order.includes(key)) order.push(key);
  });

  const conflicts = [];
  const merged = [];
  for (const key of order) {
    const b = baseMap.get(key);
    const l = localMap.get(key);
    const r = remoteMap.get(key);
    if (equal(l, r)) {
      if (l != null) merged.push(l);
      continue;
    }
    if (equal(b, l)) {
      if (r != null) merged.push(r);
      continue;
    }
    if (equal(b, r)) {
      if (l != null) merged.push(l);
      continue;
    }
    conflicts.push({ key, base: b || null, local: l || null, remote: r || null });
    if (l != null) merged.push(l);
  }
  return { merged: decorateSegments(merged), conflicts };
}
