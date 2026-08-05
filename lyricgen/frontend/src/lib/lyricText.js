/**
 * JS mirror of backend/tenant_style.py::strip_trailing_punctuation.
 *
 * The account style profile can ask for lyric lines to render without
 * sentence-final punctuation (UMG asked six separate times between May and
 * August 2026). The backend applies it in pipeline._display_segments, right
 * before handing the segments to the renderers. The previews have to apply
 * the exact same transform or the operator sees one thing and the render
 * produces another — the fidelity bug shared/renderParity.json exists to
 * prevent.
 *
 * Asserted against the generated fixture by lib/renderParity.test.js. If you
 * change this, change tenant_style.py and regenerate:
 *   cd lyricgen/backend && python3 scripts/gen_render_parity_fixture.py
 *
 * Scope note: this is for LYRIC lines only. Title cards go through the same
 * applyCase helper but must keep their punctuation — the backend transform
 * likewise never touches them.
 */

// Only sentence-final marks. `?` and `!` (and the Spanish opening marks)
// carry meaning nobody asked us to drop.
const TRAILING = /[.,;:…]+$/;

export function stripTrailingPunctuation(text) {
  if (!text) return text;
  const stripped = text.replace(/\s+$/, "");
  if (!stripped) return text;
  const trailingWs = text.slice(stripped.length);
  const cleaned = stripped.replace(TRAILING, "");
  // A line that is nothing but punctuation stays as-is: a stylistic "..."
  // card should not silently vanish.
  if (!cleaned) return text;
  return cleaned + trailingWs;
}

/** Apply the account's display preferences to one lyric line. */
export function applyLyricStyleProfile(text, styleProfile) {
  if (!styleProfile || !styleProfile.strip_trailing_punctuation) return text;
  return stripTrailingPunctuation(text);
}
