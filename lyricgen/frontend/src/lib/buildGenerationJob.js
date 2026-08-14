// Canonical adapter between an approved review and the generation queue.
// Keeping this shape outside App.jsx makes it testable: fields such as the
// editor selectors and background cache key must survive this boundary.
export function buildGenerationJob(a) {
  return {
    filename: (a.file && a.file.name) || "audio.mp3",
    _file: a.file,
    artist: a.artist,
    songTitle: (a.songTitle || "").trim(),
    language: a.language,
    genre: a.genre || "",
    font: a.font || "",
    concept: a.concept || "",
    movementStyle: a.movementStyle || "",
    effect: a.effect || "",
    backgroundHint: a.backgroundHint || "",
    bgVerbatim: !!a.bgVerbatim,
    textCase: a.textCase || "upper",
    fontScale: a.fontScale || "1.0",
    lyricsAnimation: a.lyricsAnimation || "none",
    lineTransition: a.lineTransition || "none",
    textContrast: a.textContrast || "medium",
    titleTemplate: a.titleTemplate || "auto",
    titleSize: a.titleSize || "1.0",
    titleArtistFont: a.titleArtistFont || "",
    titleSongFont: a.titleSongFont || "",
    titleSongBreak: a.titleSongBreak || "",
    segments: a.segments,
    segmentsRevision: Number.isInteger(a.segmentsRevision) ? a.segmentsRevision : 0,
    editorRevision: Number.isInteger(a.editorRevision) ? a.editorRevision : null,
    editorVersionId: a.editorVersionId || null,
    transcribeJobId: a.transcribeJobId || null,
    operatorMetrics: a.operatorMetrics || null,
    // The backend recomputes and validates this key; preserving it here only
    // avoids throwing away a completed preview between wizard steps.
    bgCacheKey: a.bgCacheKey || null,
    status: "queued",
    current_step: null,
    progress: 0,
    job_id: null,
    error: null,
  };
}
