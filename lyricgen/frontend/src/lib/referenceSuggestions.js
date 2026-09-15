// Reference text is a hypothesis. A shared word is not a replacement verdict.
const tokens = (text) => String(text || '').normalize('NFC').toLocaleLowerCase()
  .match(/[\p{L}\p{N}]+/gu) || [];
const accents = (text) => text.normalize('NFD').replace(/[\u0300-\u036f]/g, '');

function oneEdit(a, b) {
  if (Math.abs(a.length - b.length) > 1) return false;
  let i = 0, j = 0, edits = 0;
  while (i < a.length && j < b.length) {
    if (a[i] === b[j]) { i++; j++; continue; }
    if (++edits > 1) return false;
    if (a.length >= b.length) i++;
    if (b.length >= a.length) j++;
  }
  return edits + (i < a.length || j < b.length ? 1 : 0) <= 1;
}

export function findReferenceSuggestion(text, referenceLines) {
  const source = tokens(text);
  if (!source.length) return null;
  const key = source.join(' ');
  if (referenceLines.some((line) => tokens(line).join(' ') === key)) return null;
  const matches = new Map();
  for (const line of referenceLines) {
    const target = tokens(line);
    if (target.length !== source.length) continue;
    const changed = source.map((word, i) => word === target[i] ? -1 : i).filter((i) => i >= 0);
    if (changed.length !== 1) continue;
    const i = changed[0], a = source[i], b = target[i];
    const diacritic = a === accents(a) && b !== accents(b) && accents(a) === accents(b)
      && !new Set(["tu", "el", "mi", "si", "te", "de", "se", "mas", "aun", "solo"]).has(a);
    // Lexical substitutions need context and a single near-typo. Never join
    // lines, translate a phrase, reorder words, or replace short chorus names.
    const typo = source.length >= 4 && Math.min(a.length, b.length) >= 5 && accents(a) !== accents(b) && oneEdit(a, b);
    if (diacritic || typo) matches.set(target.join(' '), line.trim());
  }
  return matches.size === 1 ? [...matches.values()][0] : null;
}

export function referenceSuggestionsById(segments, referenceLines) {
  return Object.fromEntries(segments.map((segment) => [segment._id,
    findReferenceSuggestion(segment.text, referenceLines)]));
}
