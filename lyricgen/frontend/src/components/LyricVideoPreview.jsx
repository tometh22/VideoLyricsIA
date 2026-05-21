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

// Base font size as a fraction of frame WIDTH at scale=1. ~85px / 1920px in
// the render → keeps the preview proportional to the real output.
const BASE_FS_FRAC = 0.046;
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

export default function LyricVideoPreview({
  segments,           // [{_id, start, end, text, pos?, scale?, rot?}]
  currentTime,
  backgroundUrl = null,   // signed video URL (modal) | null (wizard → gradient)
  backgroundStyle = "default",
  font,                   // css font-family for parity with render (optional)
  videoRef = null,        // optional shared <video> ref for bg sync
  onSelect,               // (id) => void — bidirectional selection w/ list/timeline
  onLayoutChange,         // (id, {pos, scale, rot}) => void — commit
  onDragStart,            // () => void — push one undo snapshot
  showSafeArea = true,    // broadcast-safe guide
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

  // The line on screen = the one whose [start,end] contains currentTime —
  // exactly what the rendered video shows now and what the timeline marks.
  // In a gap / instrumental intro there is NO active line → blank stage
  // (matches the video + the timeline). We deliberately do NOT fall back to
  // the upcoming line: that showed text the timeline didn't mark and the
  // video wouldn't show, which read as a bug.
  const activeSeg = useMemo(() => {
    return segments.find((s) => currentTime >= s.start && currentTime < s.end) || null;
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
  const fsPx = l ? `${BASE_FS_FRAC * 100 * l.scale}cqw` : undefined;

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
      {activeSeg && l && (
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
          className="absolute cursor-move"
          style={{
            left: `${l.pos.x * 100}%`,
            top: `${l.pos.y * 100}%`,
            transform: `translate(-50%,-50%) rotate(${l.rot}deg)`,
            touchAction: "none",
          }}
          onPointerDown={(e) => onPointerDown(e, activeSeg, "move")}
          onPointerUp={(e) => onPointerUp(e, activeSeg)}
        >
          <div
            className="whitespace-nowrap font-extrabold text-white text-center px-1"
            style={{
              fontSize: fsPx,
              fontFamily: font || undefined,
              textShadow: "0 2px 0 #000, 0 0 18px rgba(0,0,0,.6)",
              WebkitTextStroke: "1px rgba(0,0,0,.55)",
              lineHeight: 1.1,
            }}
          >
            {activeSeg.text}
          </div>
          {/* selection box + handles */}
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
        </div>
      )}

      {!activeSeg && (
        <div className="absolute inset-0 flex items-center justify-center text-ink-tertiary text-sm">
          Reproducí o seleccioná una línea para previsualizarla
        </div>
      )}
    </div>
  );
}
