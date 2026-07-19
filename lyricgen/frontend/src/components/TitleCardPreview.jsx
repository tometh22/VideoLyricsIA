import { useRef, useState, useLayoutEffect } from "react";
import { FONT_BY_CODE, applyCase } from "./fontCatalog";

/**
 * Live, faithful preview of the intro title card (artist over song) as it
 * will render in the lyric video. Mirrors the backend `title_card_lines`
 * layout AND fonts:
 *   - artist: Montserrat ExtraBold (weight 800), UPPERCASE — hardcoded in the
 *     backend at pipeline.py (Montserrat-ExtraBold.ttf).
 *   - song:   the operator's chosen lyric font, resolved through the SAME
 *     shared catalog (fontCatalog.FONT_BY_CODE) the WizardLivePreview uses, so
 *     what the operator sees here matches what the worker burns via libass.
 *
 * It also mirrors the backend's "shrink, then wrap" fitting
 * (ass_render.fit_title_text): measure the real text at the base size, shrink
 * toward 62% to fit the safe card width, and only then wrap to two lines — so
 * the operator can SEE whether a long title/artist shrinks or wraps BEFORE the
 * ~5-10 min re-render. Approximate (CSS/DOM vs libass), but font-faithful.
 */

// Per-template geometry — mirrors ass_render._TITLE_LAYOUTS so the preview
// matches the burn. Base sizes are px-at-1920 (scaled to the box width);
// `safe` is the card width fraction; justify/align/padB place the block.
const PREVIEW_LAYOUTS = {
  centered:    { artist: 100, song: 62, safe: 0.80, justify: "center",   align: "center",     textAlign: "center", padB: 0 },
  lower_third: { artist: 64,  song: 40, safe: 0.80, justify: "flex-end", align: "center",     textAlign: "center", padB: "20%" },
  badge:       { artist: 36,  song: 28, safe: 0.45, justify: "flex-end", align: "flex-start", textAlign: "left",   padB: "8%" },
};
const MIN_RATIO = 0.62;   // mirrors fit_title_text's min_size

const ARTIST_FONT = "'Montserrat', system-ui, sans-serif"; // ExtraBold (800)

function nfc(s) {
  try {
    return (s || "").normalize("NFC");
  } catch {
    return s || "";
  }
}

/**
 * One title-card line that shrinks to fit `maxWidth` and, only if it still
 * doesn't fit at the minimum size, wraps onto two lines. A hidden measuring
 * span gives the natural single-line width at the base size; everything else
 * is derived from that one measurement (width scales ~linearly with size).
 */
function FitLine({ text, baseSize, maxWidth, fontFamily, weight, color, opacity }) {
  const measureRef = useRef(null);
  const [{ size, wrap }, setFit] = useState({ size: baseSize, wrap: false });

  useLayoutEffect(() => {
    const m = measureRef.current;
    if (!m || !text || !maxWidth) {
      setFit({ size: baseSize, wrap: false });
      return;
    }
    const natural = m.getBoundingClientRect().width; // measured at baseSize, nowrap
    const minSize = Math.max(8, baseSize * MIN_RATIO);
    if (natural <= maxWidth || natural === 0) {
      setFit({ size: baseSize, wrap: false });
    } else {
      const fit = (maxWidth * baseSize) / natural;
      setFit(fit >= minSize ? { size: fit, wrap: false } : { size: minSize, wrap: true });
    }
  }, [text, baseSize, maxWidth, fontFamily, weight]);

  if (!text) return null;

  const common = {
    fontFamily,
    fontWeight: weight,
    lineHeight: 1.05,
    letterSpacing: "-0.01em",
    color,
    opacity,
    // Mirror the libass black outline so legibility reads like the render.
    textShadow:
      "-1px -1px 0 #000, 1px -1px 0 #000, -1px 1px 0 #000, 1px 1px 0 #000, 0 2px 6px rgba(0,0,0,.5)",
  };

  return (
    <>
      <span
        ref={measureRef}
        aria-hidden="true"
        style={{
          position: "absolute",
          visibility: "hidden",
          whiteSpace: "nowrap",
          pointerEvents: "none",
          fontSize: `${baseSize}px`,
          ...common,
        }}
      >
        {text}
      </span>
      <div
        style={{
          fontSize: `${size}px`,
          whiteSpace: wrap ? "normal" : "nowrap",
          maxWidth: `${maxWidth}px`,
          textAlign: "center",
          textWrap: "balance",
          ...common,
        }}
      >
        {text}
      </div>
    </>
  );
}

export default function TitleCardPreview({
  artist = "",
  song = "",
  font = "",            // song font fallback (the lyric font, backward-compat)
  textCase = "upper",
  // Full Rotor v1 controls. Defaults reproduce the historical hero look.
  template = "auto",
  titleSize = 1.0,
  artistFont = "",      // "" → Montserrat ExtraBold (backend default)
  songFont = "",        // "" → falls back to `font` (the lyric font)
  // UI v1.1 (2026-05-30): explicit line break for the song. When the operator
  // chose to split the title manually, songLines holds the 2 lines exactly as
  // they will be rendered (no auto-wrap). null/undefined or single entry =>
  // historical behaviour (FitLine shrinks the whole title and only wraps if
  // it still overflows at the minimum size). Defaults preserve the exact
  // previous render so no existing job is affected by this prop being
  // threaded everywhere.
  songLines = null,
  // First sung line's start, in seconds. Lets the "auto" template resolve the
  // SAME way the backend does (long instrumental intro → centered hero, else
  // the compact badge) so the preview stops lying about short-intro songs.
  // null/undefined = intro length not known yet (transcription still running);
  // we assume the centered hero and the preview auto-corrects once it arrives.
  firstLyricStart = null,
  label,
}) {
  const boxRef = useRef(null);
  const [boxW, setBoxW] = useState(0);

  useLayoutEffect(() => {
    const el = boxRef.current;
    if (!el || typeof ResizeObserver === "undefined") {
      if (el) setBoxW(el.clientWidth);
      return undefined;
    }
    const ro = new ResizeObserver((entries) => {
      for (const e of entries) setBoxW(e.contentRect.width);
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // The backend always UPPERCASEs the artist; the song follows the operator's
  // textCase (same as the lyric lines). NFC-normalise to match the render's
  // accent handling (ass_render.title_card_lines).
  const artistU = nfc(artist).trim().toUpperCase();
  const songD = applyCase(nfc(song).trim(), textCase);

  // Manual split (UI v1.1): when songLines is a non-empty array with 2+
  // entries, render each line through its own FitLine so each shrinks to fit
  // independently and the operator-chosen break is respected. A single
  // entry falls back to the legacy single-line behaviour.
  const manualSongLines = Array.isArray(songLines)
    ? songLines.map((s) => applyCase(nfc(s || "").trim(), textCase)).filter(Boolean)
    : null;
  const useManualSplit = !!(manualSongLines && manualSongLines.length >= 2);

  // Per-element fonts: artist → chosen font or Montserrat ExtraBold (800);
  // song → chosen font or the lyric font (`font`). Mirrors the backend's
  // _render_lyrics_ass resolution.
  const artistResolved = FONT_BY_CODE[artistFont];
  const artistFamily = artistResolved?.css || ARTIST_FONT;
  const artistWeight = artistResolved?.weight || 800;
  // FONT_BY_CODE[""] is a real (truthy) entry with css:undefined, so guard on
  // the code being non-empty before using it; otherwise fall back to `font`
  // (the lyric font), then to the catalog default.
  const songResolved =
    (songFont && FONT_BY_CODE[songFont]) || FONT_BY_CODE[font] || FONT_BY_CODE[""];
  const songFamily = songResolved.css || ARTIST_FONT;
  const songWeight = songResolved.weight || 700;

  // Layout geometry mirrors ass_render._TITLE_LAYOUTS. "auto" resolves EXACTLY
  // like the backend (ass_render.title_card_lines): a long instrumental intro
  // (first sung line > 0.8s) renders the centered hero, otherwise the compact
  // lower-left badge. When the intro length is unknown (firstLyricStart null,
  // transcription not finished) we assume the hero — the common case — and the
  // preview auto-corrects once the segments arrive. size × titleSize, clamped
  // 0.5–2.0 like the backend.
  const resolveAuto = () =>
    firstLyricStart == null ? "centered" : firstLyricStart > 0.8 ? "centered" : "badge";
  const tmpl = ["centered", "lower_third", "badge"].includes(template)
    ? template
    : resolveAuto();
  const L = PREVIEW_LAYOUTS[tmpl];
  const sizeN = Math.max(0.5, Math.min(2.0, parseFloat(titleSize) || 1));
  const maxWidth = boxW * L.safe;

  return (
    <div className="flex flex-col gap-1">
      {label ? (
        <span className="text-[10px] uppercase tracking-wider text-gray-500">
          {label}
        </span>
      ) : null}
      <div
        ref={boxRef}
        className="relative w-full aspect-video rounded-xl overflow-hidden ring-1 ring-white/[0.08] select-none"
        style={{
          background:
            "radial-gradient(120% 90% at 65% 25%, #2b1d52 0%, #15102e 50%, #07050f 100%)",
        }}
      >
        {/* subtle vignette so the outlined text reads like the real render */}
        <div
          className="absolute inset-0 pointer-events-none"
          style={{
            background:
              "radial-gradient(120% 80% at 50% 50%, transparent 55%, rgba(0,0,0,.5))",
          }}
        />
        <div
          className="absolute inset-0 flex flex-col gap-[0.45em] px-[6%]"
          style={{
            justifyContent: L.justify,
            alignItems: L.align,
            textAlign: L.textAlign,
            paddingBottom: L.padB || 0,
          }}
        >
          {boxW > 0 && (
            <>
              <FitLine
                text={artistU}
                baseSize={boxW * (L.artist / 1920) * sizeN}
                maxWidth={maxWidth}
                fontFamily={artistFamily}
                weight={artistWeight}
                color="#FFFFFF"
                opacity={0.97}
              />
              {useManualSplit ? (
                <div
                  className="flex flex-col"
                  style={{ gap: "0.18em", alignItems: L.align, width: "100%" }}
                >
                  {manualSongLines.slice(0, 2).map((line, i) => (
                    <FitLine
                      key={i}
                      text={line}
                      baseSize={boxW * (L.song / 1920) * sizeN}
                      maxWidth={maxWidth}
                      fontFamily={songFamily}
                      weight={songWeight}
                      color="#FFFFFF"
                      opacity={0.85}
                    />
                  ))}
                </div>
              ) : (
                <FitLine
                  text={songD}
                  baseSize={boxW * (L.song / 1920) * sizeN}
                  maxWidth={maxWidth}
                  fontFamily={songFamily}
                  weight={songWeight}
                  color="#FFFFFF"
                  opacity={0.85}
                />
              )}
            </>
          )}
          {!artistU && !songD && !useManualSplit && (
            <span className="text-xs text-gray-500">—</span>
          )}
        </div>
      </div>
    </div>
  );
}
