/**
 * Live, EDITABLE preview of the lyric video — the right-hand stage of the
 * editor workspace. Shows the line active at `currentTime` rendered over the
 * background (real cached video in the post-render modal, or a style-tinted
 * template gradient in the wizard where no background exists yet), and lets
 * the operator lay out that line: drag to move, corner handle to scale,
 * rotation handle to tilt ("letras torcidas").
 *
 * Layout is stored per segment as resolution-independent overrides in
 * segments_json: pos:{x,y} (0..1 fractions of the frame), scale (multiplier
 * on the base font size), rot (degrees). Absent → centered default, which is
 * what the render pipeline already produces. Edits commit through the parent
 * (onLayoutChange → setEdited) — this component owns no persistence.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { REF_W, tierForLength, clampFontScale } from "../lib/lyricTiers";

// Font fidelity: the render (ass_render.lyric_fontsize + pipeline wrap tiers)
// sizes each line by CHARACTER COUNT and wraps it at a tier-specific width,
// at the 1920×1080 baseline (text_scale = 1). We mirror those exact tiers
// (lib/lyricTiers — shared with the editor) so what you lay out is what
// renders; fontPx is the render's \fs (px in the 1080-tall frame), expressed
// as a fraction of the 1920 frame WIDTH (cqw) to match the 16:9 stage.
const DEFAULT_POS = { x: 0.5, y: 0.5 };
const MIN_SCALE = 0.4;
const MAX_SCALE = 2.6;
const CLICK_SLOP_PX = 4;

// Style → template gradient (wizard preview, no real bg yet). Mirrors the
// pipeline's gradient moods loosely; purely indicative.
const STYLE_GRADIENTS = {
  calido: "radial-gradient(120% 90% at 30% 0%,#4a3422,#1c140d 55%,#0c0907)",
  oscuro: "radial-gradient(120% 90% at 60% 0%,#1c2030,#0f1117 55%,#08090d)",
  vibrante: "radial-gradient(120% 90% at 40% 10%,#3a1a4e,#171022 55%,#0b0810)",
  default: "radial-gradient(120% 90% at 30% 0%,#3a2d5e,#15131f 55%,#0a0910)",
};

function clamp(v, lo, hi) { return Math.min(Math.max(v, lo), hi); }

function applyCase(text, code) {
  if (code === "upper") return (text || "").toUpperCase();
  if (code === "lower") return (text || "").toLowerCase();
  if (code === "title") return (text || "").replace(/\b\w/g, (c) => c.toUpperCase());
  return text || "";
}

// Outline/shadow per contrast level (approximates the render look).
const CONTRAST_STYLES = {
  subtle: { WebkitTextStroke: "0px", textShadow: "0 1px 5px rgba(0,0,0,.55)" },
  medium: { WebkitTextStroke: "1px rgba(0,0,0,.55)", textShadow: "0 2px 0 #000, 0 0 18px rgba(0,0,0,.6)" },
  strong: { WebkitTextStroke: "1.5px #000", textShadow: "0 0 6px rgba(0,0,0,1), -1px -1px 0 #000, 1px 1px 0 #000, 0 2px 0 #000" },
};

// Fade duration per transition (seconds), mirroring ass_render.fade_seconds.
// cut = hard in/out; capped to dur/3 so short lines never fade the whole time.
const FADE_SECONDS = { cut: 0, fade: 0.15, fade_slow: 0.3 };

export default function LyricVideoPreview({
  segments,           // [{_id, start, end, text, pos?, scale?, rot?}]
  currentTime,
  backgroundUrl = null,   // signed video URL (modal) | null (wizard → gradient)
  backgroundStyle = "default",
  font,                   // css font-family for parity with render (optional)
  textCase = "upper",     // upper | lower | title | original — applied to displayed text
  textContrast = "medium",// subtle | medium | strong — outline/shadow strength
  transition = "cut",     // cut | fade | fade_slow — previewed as opacity ramp
  fontScale = 1,          // global size multiplier (render clamps 0.6–1.5)
  videoRef = null,        // optional shared <video> ref for bg sync
  onSelect,               // (id) => void — bidirectional selection w/ list/timeline
  onLayoutChange,         // (id, {pos, scale, rot}) => void — commit
  onDragStart,            // () => void — push one undo snapshot
  showSafeArea = true,    // broadcast-safe guide
  editable = true,        // false → read-only preview (no handles/selection box)
  t = null,               // optional i18n hook from useI18n() — falls back to ES copy
}) {
  const frameRef = useRef(null);
  const bgVideoRef = useRef(null);
  const dragRef = useRef(null);  // {mode, id, ...origin}
  const [live, setLive] = useState(null); // {id, pos, scale, rot} during drag

  // Keep the background frame in step with the audio scrub. The bg video is
  // a short seamless loop, so map the audio time into it via modulo. Guarded
  // for jsdom (no video impl) and NaN duration before metadata loads.
  useEffect(() => {
    const v = bgVideoRef.current;
    if (!v || !backgroundUrl) return;
    const dur = v.duration;
    if (!dur || !isFinite(dur)) return;
    try { v.currentTime = currentTime % dur; } catch { /* not seekable yet */ }
  }, [currentTime, backgroundUrl]);

  // The line on screen = the one whose [start,end] contains currentTime,
  // PLUS a sticky-last-line fallback during short gaps so the placeholder
  // doesn't flicker between back-to-back lines.
  //
  // INCIDENT 2026-05-24: with songs that have lines packed tight (a 0.3 s
  // gap between two segments is normal), the old "containing-only" logic
  // dropped to `null` every gap, showing the "Reproducí o seleccioná…"
  // placeholder for a frame and ripping it out the next. From the user's
  // chair: the preview felt epileptic during the chorus.
  //
  // New rule mirrors what LyricsEditor.activeId already does (`containing
  // || lastStarted`): if currentTime is inside a segment, show it; if not,
  // hold the last segment that already started — UNTIL either the next
  // segment kicks in or we've drifted >TAIL_HOLD_S seconds past the last
  // segment's end (truly empty section, e.g. instrumental break). That
  // window is small enough to not lie during long instrumentals but big
  // enough to bridge any gap inside a verse.
  const TAIL_HOLD_S = 1.2;
  const activeSeg = useMemo(() => {
    let containing = null;
    let lastStarted = null;
    for (const s of segments) {
      if (currentTime >= s.start && currentTime < s.end) {
        containing = s;
      }
      if (currentTime >= s.start) {
        lastStarted = s;
      }
    }
    if (containing) return containing;
    if (lastStarted && (currentTime - lastStarted.end) <= TAIL_HOLD_S) return lastStarted;
    return null;
  }, [segments, currentTime]);

  const layoutOf = useCallback((seg) => {
    if (live && seg && live.id === seg._id) return live;
    return {
      pos: seg?.pos || DEFAULT_POS,
      scale: typeof seg?.scale === "number" ? seg.scale : 1,
      rot: typeof seg?.rot === "number" ? seg.rot : 0,
    };
  }, [live]);

  const frameRect = () => frameRef.current?.getBoundingClientRect();

  const onPointerDown = useCallback((e, seg, mode) => {
    e.stopPropagation();
    e.currentTarget.setPointerCapture?.(e.pointerId);
    onDragStart?.();
    const rect = frameRect();
    const l = layoutOf(seg);
    const cx = rect ? rect.left + l.pos.x * rect.width : e.clientX;
    const cy = rect ? rect.top + l.pos.y * rect.height : e.clientY;
    dragRef.current = {
      mode, id: seg._id,
      originX: e.clientX, originY: e.clientY,
      origPos: l.pos, origScale: l.scale, origRot: l.rot,
      centerX: cx, centerY: cy,
      startDist: Math.hypot(e.clientX - cx, e.clientY - cy) || 1,
      startAngle: Math.atan2(e.clientY - cy, e.clientX - cx),
      moved: false,
    };
    setLive({ id: seg._id, ...l });
  }, [layoutOf, onDragStart]);

  const onPointerMove = useCallback((e) => {
    const d = dragRef.current;
    if (!d) return;
    const rect = frameRect();
    if (!rect) return;
    if (Math.hypot(e.clientX - d.originX, e.clientY - d.originY) > CLICK_SLOP_PX) d.moved = true;
    let { origPos: pos, origScale: scale, origRot: rot } = d;
    if (d.mode === "move") {
      pos = {
        x: clamp(d.origPos.x + (e.clientX - d.originX) / rect.width, 0, 1),
        y: clamp(d.origPos.y + (e.clientY - d.originY) / rect.height, 0, 1),
      };
    } else if (d.mode === "resize") {
      const dist = Math.hypot(e.clientX - d.centerX, e.clientY - d.centerY);
      scale = clamp(d.origScale * (dist / d.startDist), MIN_SCALE, MAX_SCALE);
    } else if (d.mode === "rotate") {
      const ang = Math.atan2(e.clientY - d.centerY, e.clientX - d.centerX);
      rot = Math.round((d.origRot + (ang - d.startAngle) * 180 / Math.PI) * 10) / 10;
    }
    setLive({ id: d.id, pos, scale, rot });
  }, []);

  const onPointerUp = useCallback((e, seg) => {
    const d = dragRef.current;
    dragRef.current = null;
    if (!d) { setLive(null); return; }
    const committed = live;
    setLive(null);
    if (!d.moved) { onSelect?.(seg._id); return; }
    if (committed) onLayoutChange?.(seg._id, {
      pos: committed.pos, scale: committed.scale, rot: committed.rot,
    });
  }, [live, onSelect, onLayoutChange]);

  const bg = backgroundUrl
    ? null
    : (STYLE_GRADIENTS[backgroundStyle] || STYLE_GRADIENTS.default);

  const l = activeSeg ? layoutOf(activeSeg) : null;
  // Cased display text drives BOTH what we show and which size/wrap tier the
  // render would pick — so the preview length tier matches the render's.
  const displayText = activeSeg ? applyCase(activeSeg.text, textCase) : "";
  const tier = tierForLength(displayText.length);
  // Render size = tier × per-line scale × global font_scale (clamped like the
  // backend), as a fraction of the 1920 frame width → cqw.
  const fsPx = l ? `${(tier.fontPx / REF_W) * 100 * l.scale * clampFontScale(fontScale)}cqw` : undefined;
  const wrapMaxCqw = `${(tier.wrapPx / REF_W) * 100}cqw`;

  // Fade-in/out opacity so the chosen transition is visible while scrubbing
  // the active line. Ramps 0→1 over the first `fd` seconds and 1→0 over the
  // last `fd`; full opacity in the middle. cut → always 1 (no change).
  const textOpacity = useMemo(() => {
    if (!activeSeg) return 1;
    const base = FADE_SECONDS[transition] ?? 0;
    if (base <= 0) return 1;
    const fd = Math.min(base, (activeSeg.end - activeSeg.start) / 3);
    if (fd <= 0) return 1;
    const ramp = Math.min((currentTime - activeSeg.start) / fd, (activeSeg.end - currentTime) / fd, 1);
    return clamp(ramp, 0, 1);
  }, [activeSeg, transition, currentTime]);

  return (
    <div
      ref={frameRef}
      className="relative w-full rounded-xl overflow-hidden select-none"
      style={{
        aspectRatio: "16 / 9",
        background: bg || "#0a0910",
        containerType: "inline-size",
        touchAction: "none",
      }}
      onPointerMove={onPointerMove}
    >
      {backgroundUrl && (
        <video
          ref={(el) => { bgVideoRef.current = el; if (videoRef) videoRef.current = el; }}
          src={backgroundUrl}
          muted playsInline preload="auto"
          className="absolute inset-0 w-full h-full object-cover"
        />
      )}
      <div className="absolute inset-0 pointer-events-none"
        style={{ background: "radial-gradient(130% 80% at 50% 120%,transparent,rgba(0,0,0,.5))" }} />

      {/* Broadcast-safe area guide (keeps text off the edges). */}
      {showSafeArea && (
        <div className="absolute pointer-events-none rounded"
          style={{ inset: "5%", border: "1px dashed rgba(255,255,255,.12)" }} />
      )}

      {/* Readouts + reset for the selected line. */}
      {editable && activeSeg && l && (
        <div className="absolute top-2 right-2 flex items-center gap-1.5 z-10">
          <span className="rounded-md bg-black/55 backdrop-blur-sm ring-1 ring-white/10 px-2 py-1 text-[10px] text-white/80 tabular-nums">
            {Math.round(l.scale * 100)}% · {Math.round(l.rot)}°
          </span>
          {(l.scale !== 1 || l.rot !== 0 || l.pos.x !== DEFAULT_POS.x || l.pos.y !== DEFAULT_POS.y) && (
            <button
              type="button"
              className="rounded-md bg-black/55 backdrop-blur-sm ring-1 ring-white/10 px-2 py-1 text-[10px] text-white/80 hover:text-white hover:ring-white/25"
              onClick={(e) => {
                e.stopPropagation();
                onDragStart?.();
                onLayoutChange?.(activeSeg._id, { pos: DEFAULT_POS, scale: 1, rot: 0 });
              }}
              title="Volver esta línea al centro, tamaño y orientación por defecto"
            >
              ↺ Resetear
            </button>
          )}
        </div>
      )}

      {activeSeg && l && (
        <div
          className={`absolute ${editable ? "cursor-move" : "pointer-events-none"}`}
          style={{
            left: `${l.pos.x * 100}%`,
            top: `${l.pos.y * 100}%`,
            transform: `translate(-50%,-50%) rotate(${l.rot}deg)`,
            touchAction: "none",
          }}
          onPointerDown={editable ? (e) => onPointerDown(e, activeSeg, "move") : undefined}
          onPointerUp={editable ? (e) => onPointerUp(e, activeSeg) : undefined}
        >
          <div
            className="font-extrabold text-white text-center px-1"
            style={{
              fontSize: fsPx,
              fontFamily: font || undefined,
              lineHeight: 1.1,
              opacity: textOpacity,
              maxWidth: wrapMaxCqw,
              whiteSpace: "normal",
              overflowWrap: "break-word",
              ...(CONTRAST_STYLES[textContrast] || CONTRAST_STYLES.medium),
            }}
          >
            {displayText}
          </div>
          {/* selection box + handles (edit mode only) */}
          {editable && (
            <>
              <div className="absolute -inset-2 ring-1 ring-accent rounded pointer-events-none" />
              {/* resize handle (bottom-right corner) */}
              <span
                className="absolute -bottom-2 -right-2 w-3 h-3 bg-white ring-1 ring-accent rounded-sm cursor-nwse-resize pointer-events-auto"
                style={{ touchAction: "none" }}
                onPointerDown={(e) => onPointerDown(e, activeSeg, "resize")}
                onPointerUp={(e) => onPointerUp(e, activeSeg)}
                title="Escalar"
              />
              {/* rotation handle (above) */}
              <span
                className="absolute left-1/2 -translate-x-1/2 -top-7 w-3.5 h-3.5 bg-accent ring-2 ring-white rounded-full cursor-grab pointer-events-auto"
                style={{ touchAction: "none" }}
                onPointerDown={(e) => onPointerDown(e, activeSeg, "rotate")}
                onPointerUp={(e) => onPointerUp(e, activeSeg)}
                title="Rotar"
              />
            </>
          )}
        </div>
      )}

      {!activeSeg && (
        <div className="absolute inset-0 flex items-center justify-center text-ink-tertiary text-sm px-4 text-center">
          {(t && t("editor.preview_empty")) ||
            "Reproducí o seleccioná una línea para previsualizarla"}
        </div>
      )}
    </div>
  );
}
