/** Creative fields restored when a transcribed job is reopened by URL.
 *
 * First-generation jobs persist these choices inside render_params. The old
 * resume adapter only read legacy top-level fields, silently dropping a long
 * operator prompt before POST /generate. Keep the mapping pure and tested.
 */
export function creativeFieldsForReviewResume(job = {}) {
  const params = job.render_params && typeof job.render_params === "object"
    ? job.render_params
    : {};
  return {
    genre: job.genre || params.genre || "",
    concept: job.concept || params.concept || "",
    movementStyle: job.movement_style || params.movement_style || "",
    effect: job.effect || params.effect || "",
    backgroundHint: params.background_hint || job.background_hint || "",
    bgVerbatim: params.bg_verbatim != null
      ? !!params.bg_verbatim
      : !!job.bg_verbatim,
    animateImage: params.animate_image === true || params.animate_image === "true",
  };
}

/** What the operator chose in the wizard for THIS song before /generate.
 *  None of it exists on the server yet (render_params are written by
 *  /generate), so a resume of the wizard's own review must not reset it. */
export const WIZARD_PICK_FIELDS = [
  // Letra: look + variante + tipografía + colores.
  "lyricLook", "lyricGenre", "lyricVariant", "lyricFontMain", "lyricFontKey",
  "lyricAltColor", "lyricAccentColor", "lookResolved",
  "font", "textCase", "frameFormat", "fontScale", "textContrast",
  "lyricsAnimation", "lineTransition", "lyricColor", "lyricSungColor",
  // Fondo.
  "genre", "concept", "movementStyle", "effect", "backgroundHint", "bgVerbatim",
  "matchLyrics",
  // Portada.
  "titleTemplate", "titleSize", "titleArtistFont", "titleSongFont", "titleSongBreak",
];

/**
 * The wizard gives its freshly transcribed review a durable /review/<id>
 * URL, and that URL re-reads /status. The server is the truth for the
 * transcription (segments, language/reference gates), but NOT for the picks
 * above nor for the in-session audio File and the batch queue: overlaying
 * them back keeps the look the operator chose (incident 11-nov-2026: the
 * look never reached the render after "Revisar letra").
 *
 * `prior` is the in-memory review of the same job (see
 * reviewJobRoute.reviewAlreadyOpen); without it the server review is
 * returned untouched.
 */
export function mergeWizardPicksIntoResumedReview(serverReview, prior) {
  if (!prior) return serverReview;
  const merged = { ...serverReview };
  for (const key of WIZARD_PICK_FIELDS) {
    if (prior[key] !== undefined) merged[key] = prior[key];
  }
  // The operator's own audio (local playback, file-name joins with files[*])
  // and the position in a multi-song batch.
  if (prior.file) merged.file = prior.file;
  if (Array.isArray(prior.queue)) merged.queue = prior.queue;
  if (Number.isInteger(prior.queueIdx)) merged.queueIdx = prior.queueIdx;
  return merged;
}
