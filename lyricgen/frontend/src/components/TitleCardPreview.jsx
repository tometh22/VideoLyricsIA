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

// Base font sizes as a fraction of the 1920px render width, matching the
// backend hero-card tiers (artist 100px, song 62px at 1920) and the 80% safe
// card width. min ratio 0.62 mirrors fit_title_text's min_size.
const ARTIST_RATIO = 100 / 1920;
const SONG_RATIO = 62 / 1920;
const SAFE_W_FRAC = 0.8;
const MIN_RATIO = 0.62;

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
  font = "",
  textCase = "upper",
  fontScale = "1.0",
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

  // Song renders in the chosen lyric font (shared catalog); artist in
  // Montserrat ExtraBold. fontScale nudges both, clamped like the backend.
  const songFont = FONT_BY_CODE[font] || FONT_BY_CODE[""];
  const scaleN = Math.max(0.6, Math.min(1.5, parseFloat(fontScale) || 1));
  const maxWidth = boxW * SAFE_W_FRAC;

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
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-[0.4em] px-[6%] text-center">
          {boxW > 0 && (
            <>
              <FitLine
                text={artistU}
                baseSize={boxW * ARTIST_RATIO * scaleN}
                maxWidth={maxWidth}
                fontFamily={ARTIST_FONT}
                weight={800}
                color="#FFFFFF"
                opacity={0.97}
              />
              <FitLine
                text={songD}
                baseSize={boxW * SONG_RATIO * scaleN}
                maxWidth={maxWidth}
                fontFamily={songFont.css || ARTIST_FONT}
                weight={songFont.weight || 700}
                color="#FFFFFF"
                opacity={0.85}
              />
            </>
          )}
          {!artistU && !songD && (
            <span className="text-xs text-gray-500">—</span>
          )}
        </div>
      </div>
    </div>
  );
}
