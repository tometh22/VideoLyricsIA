// Acoustic word clocks are absolute. A moved line must move them by the
// same delta; an incompatible resize/legacy payload must use the renderer's
// synthesized fallback rather than retaining clocks outside the new line.
export function validSegmentWords(words, start, end) {
  let previous = start;
  return Array.isArray(words) && words.length > 0 && words.every(word => {
    const valid = word && typeof word.word === "string"
      && Number.isFinite(word.start) && Number.isFinite(word.end)
      && word.start >= start - 1e-4 && word.end <= end + 1e-4
      && word.start >= previous - 1e-4 && word.end > word.start;
    if (valid) previous = word.end;
    return valid;
  });
}

export function updateSegmentTiming(segment, start, end) {
  const { words, ...rest } = segment;
  const next = { ...rest, start, end };
  const delta = start - Number(segment.start);
  const isMove = Math.abs(delta - (end - Number(segment.end))) < 1e-4;
  if (isMove && validSegmentWords(words, Number(segment.start), Number(segment.end))) {
    next.words = words.map(word => ({ ...word, start: word.start + delta, end: word.end + delta }));
  } else if (validSegmentWords(words, start, end)) {
    next.words = words;
  }
  return next;
}
