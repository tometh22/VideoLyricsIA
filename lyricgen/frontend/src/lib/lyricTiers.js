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

// Per-font size normalization — mirrors ass_render._FONT_SIZE_NORM (keep in sync).
// libass scales \fs by each family's internal vertical metrics, so the render
// multiplies \fs by these to land every font on the same cap-height (Montserrat
// reference). The preview MUST apply the same factor or non-Montserrat fonts
// (e.g. Poppins, which renders ~14% smaller) preview at the wrong size.
export const FONT_SIZE_NORM = {
  montserrat: 1.0,
  poppins: 1.159,
  oswald: 0.962,
  anton: 0.927,
  jost: 0.911,
  roboto: 0.85,
  "bebas neue": 0.85,
  outfit: 0.810,
};

// Returns a family's render size factor. Robust to either a font CODE
// ("poppins-bold", "bebas-neue") or a CSS family ("'Poppins', sans-serif") so
// both previews can call it with whatever they hold. Hyphens → spaces, then
// match any known family name as a substring. "" / unknown → 1.0.
export function fontSizeFactor(font) {
  const s = String(font || "").replace(/-/g, " ").toLowerCase();
  for (const fam of Object.keys(FONT_SIZE_NORM)) {
    if (s.includes(fam)) return FONT_SIZE_NORM[fam];
  }
  return 1.0;
}

// Python's round() is banker's rounding (half-to-even): round(52.5) = 52,
// where JS Math.round gives 53. The render computes int px with Python
// round(), so the preview must round the same way or e.g. 70×0.75 lands on
// a different pixel than the video. Positive inputs only (font sizes).
function pyRound(x) {
  const floor = Math.floor(x);
  const diff = x - floor;
  if (diff > 0.5) return floor + 1;
  if (diff < 0.5) return floor;
  return floor % 2 === 0 ? floor : floor + 1;
}

// THE preview-side font size, in integer px at the 1920×1080 baseline —
// numerically identical to ass_render.lyric_fontsize(text_len, scale=1,
// font_scale, font_size_factor(font)). Previews convert to cqw via
// lyricFontPx(...)/REF_W*100. Guarded against the render by
// lib/renderParity.test.js over the generated shared/renderParity.json
// fixture — change the formula on either side without the other and the
// parity test fails in CI, not a client render weeks later.
export function lyricFontPx(textLen, fontScale, font) {
  const base = tierForLength(textLen).fontPx;
  const scaled = base * clampFontScale(fontScale) * fontSizeFactor(font);
  return Math.max(18, pyRound(scaled));
}

// Fade duration per transition (seconds) — mirrors ass_render._FADE_DURATIONS_S
// ("cut" has no entry there; it maps to 0). Shared by LyricVideoPreview's
// opacity ramp and the parity test.
export const FADE_SECONDS = { cut: 0, fade: 0.15, fade_slow: 0.3 };

// Mirrors ass_render.fade_seconds: capped at 1/3 of the segment so short
// lines never spend their whole duration fading.
export function fadeSeconds(transition, segDuration) {
  const base = FADE_SECONDS[transition] ?? 0;
  return Math.min(base, Math.max(0, segDuration) / 3);
}
