// Approval identities describe occurrences in the exact persisted snapshot.
// Legacy split/copy operations can leave repeated IDs. Preserve unambiguous
// IDs, but qualify ambiguous occurrences without changing any saved row.
const EDGE_WHITESPACE = /^[ \t\n\v\f\r\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+|[ \t\n\v\f\r\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]+$/g;
const identityText = (value) => typeof value === "string" ? value.replace(EDGE_WHITESPACE, "")
  : Number.isSafeInteger(value) ? String(value) : "";

export function reviewLineIds(segments) {
  const candidates = segments.map((segment, index) =>
    identityText(segment.segment_id) || identityText(segment.id) || `index:${index}`);
  const counts = new Map();
  for (const value of candidates) counts.set(value, (counts.get(value) || 0) + 1);
  const reserved = new Set(candidates);
  const used = new Set();
  return candidates.map((value, index) => {
    let key = value;
    if (counts.get(value) > 1) {
      const base = `index:${index}`;
      key = base;
      for (let suffix = 1; reserved.has(key) || used.has(key); suffix += 1) key = `${base}#${suffix}`;
    }
    used.add(key);
    return key;
  });
}
