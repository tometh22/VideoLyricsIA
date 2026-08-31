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
  };
}
