/**
 * Single source of truth for lyric font-size tiers on the FRONTEND.
 *
 * These mirror the render backend exactly so every live preview is WYSIWYG:
 *   - fontPx  = ass_render.lyric_fontsize tiers (lyricgen/backend/ass_render.py)
 *   - wrapPx  = pipeline.py caption-wrap widths (_make_text_clip)
 * at the 1920×1080 baseline (text_scale = height/1080 = 1). The backend keeps
 * its own copy (Python); if you change a number here, change it there too.
 */
export const REF_W = 1920;

export const LYRIC_TIERS = [
  { maxChars: 50, fontPx: 85, wrapPx: 1500 },
  { maxChars: 80, fontPx: 70, wrapPx: 1650 },
  { maxChars: Infinity, fontPx: 55, wrapPx: 1700 },
];

export function tierForLength(len) {
  return LYRIC_TIERS.find((t) => len <= t.maxChars) || LYRIC_TIERS[LYRIC_TIERS.length - 1];
}

// font_scale clamp matches the backend (ass_render.lyric_fontsize / pipeline):
// the UI must never offer or preview a multiplier the render won't honor.
export const FONT_SCALE_MIN = 0.6;
export const FONT_SCALE_MAX = 1.5;

export function clampFontScale(fs) {
  return Math.max(FONT_SCALE_MIN, Math.min(FONT_SCALE_MAX, Number(fs) || 1));
}
