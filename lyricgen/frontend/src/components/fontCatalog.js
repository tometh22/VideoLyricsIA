// Shared font catalog for the wizard previews — the single frontend mirror
// of the backend's _FONT_CATALOGUE (lyricgen/backend/pipeline.py). The font
// CODES here match the backend ids exactly, so a preview renders with the
// SAME family + weight the worker will burn via libass. The 8 families are
// preloaded in index.html (Montserrat also at weight 800 for the title-card
// artist line). "" = Auto → no override (keep the wrapper's default look).
//
// WizardLivePreview.jsx and LyricsEditor.jsx historically carry their own
// inline copies of this table; new code should import from here instead of
// duplicating, so the catalog can't drift.

export const FONT_BY_CODE = {
  "":                { css: undefined,                weight: undefined },
  "jost-bold":       { css: "'Jost', sans-serif",       weight: 700 },
  "montserrat-bold": { css: "'Montserrat', sans-serif", weight: 700 },
  "poppins-bold":    { css: "'Poppins', sans-serif",    weight: 700 },
  "outfit-bold":     { css: "'Outfit', sans-serif",     weight: 700 },
  "roboto-bold":     { css: "'Roboto', sans-serif",     weight: 700 },
  "bebas-neue":      { css: "'Bebas Neue', sans-serif", weight: 400 },
  "oswald-bold":     { css: "'Oswald', sans-serif",     weight: 700 },
  "anton":           { css: "'Anton', sans-serif",      weight: 400 },
};

// Mirrors UploadZone.applyTextCase / LyricsEditor.applyCase / the libass
// render's case transform, so previews read exactly like the output.
export function applyCase(text, c) {
  if (!text) return text;
  if (c === "upper") return text.toUpperCase();
  if (c === "title") return text.replace(/\b\w/g, (ch) => ch.toUpperCase());
  if (c === "lower") return text.toLowerCase();
  return text;
}
