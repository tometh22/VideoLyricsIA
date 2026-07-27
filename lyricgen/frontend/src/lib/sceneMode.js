/**
 * Maps the wizard's creative-direction control onto the legacy
 * `match_lyrics` boolean consumed by the existing API.
 *
 * Prompt mode is deliberately independent from the previously selected
 * Auto/Lyrics card: the operator prompt is the creative source, so both
 * Auto -> Prompt and Lyrics -> Prompt must send `match_lyrics=false`.
 */
export function inspiredByLyricsForSceneMode(mode) {
  return mode === "lyrics";
}
