/**
 * Visual per-line timings editor (Rotor-style "Timings" tab), rendered as a
 * VIEW of the shared LyricsEditor — same `edited` state, no new data model.
 *
 * Each lyric line is a block on a horizontal time axis. The operator drags:
 *   - the left edge  → move START (clamped to prev.end + gap .. end - MIN_DUR)
 *   - the right edge → set END independently (the gap the list view can't do)
 *   - the body       → move the whole block, preserving duration
 * A playhead tracks audio currentTime; clicking the lane seeks. Any drag
 * marks the line `locked` so pipeline._apply_display_timing respects the
 * operator's manual end instead of auto-extending it (hold-until-next).
 *
 * All edits go up through the parent's setEdited → existing autosave /
 * onEditedChange / onApprove. This component owns NO persistence.
 *
 * Phase 1: time ruler + draggable blocks + playhead + click-seek + reset.
 * Phase 2 (separate): waveform background + word-level karaoke.
 */
import { useCallback, useEffect, useRef, useState } from "react";

const PX_PER_SEC = 30;        // zoom: 30px = 1s → a 3s line is ~90px (draggable)
const EDGE_PX = 10;           // width of the start/end grab handles
const MIN_DUR_S = 0.3;        // shortest readable on-screen window
const CLICK_SLOP_PX = 4;      // movement under this = click (focus/seek), not drag
const LANE_H = 56;            // block height in px

function fmt(sec) {
  if (!isFinite(sec) || sec < 0) sec = 0;
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

export default function LyricsTimeline({
  segments,            // [{_id, start, end, text, locked?}]
  duration,
  currentTime,
  activeId,
  focusedSegId,
  highlightedIds,      // Set<_id>
  gapS = 0.05,
  onSeek,              // (seconds) => void
  onDragStart,         // () => void  — push one undo snapshot before a drag commits
  onTimingChange,      // (id, newStart, newEnd) => void — commit; parent sets locked
  onFocus,             // (id) => void
  onReset,             // () => void
}) {
  const laneRef = useRef(null);
  const scrollRef = useRef(null);
  // Live drag preview so the block follows the pointer at 60fps without
  // committing to parent state every frame (perf with 40+ lines).
  const [preview, setPreview] = useState(null); // {id, start, end} | null
  const dragRef = useRef(null); // {id, mode, originX, origStart, origEnd, moved}
  const lastScrollIdRef = useRef(null);

  const total = Math.max(duration || 0, ...segments.map((s) => s.end), 1);
  const laneWidth = total * PX_PER_SEC;

  // Neighbours by chronological order (exclude self), for clamping.
  const neighbours = useCallback(
    (id) => {
      const sorted = [...segments].sort((a, b) => a.start - b.start);
      const i = sorted.findIndex((s) => s._id === id);
      return { prev: i > 0 ? sorted[i - 1] : null, next: i < sorted.length - 1 ? sorted[i + 1] : null };
    },
    [segments]
  );

  const onPointerDown = useCallback(
    (e, seg, mode) => {
      e.stopPropagation();
      e.currentTarget.setPointerCapture?.(e.pointerId);
      onDragStart?.();
      dragRef.current = {
        id: seg._id, mode, originX: e.clientX,
        origStart: seg.start, origEnd: seg.end, moved: false,
      };
      setPreview({ id: seg._id, start: seg.start, end: seg.end });
    },
    [onDragStart]
  );

  const onPointerMove = useCallback(
    (e) => {
      const d = dragRef.current;
      if (!d) return;
      const deltaPx = e.clientX - d.originX;
      if (Math.abs(deltaPx) > CLICK_SLOP_PX) d.moved = true;
      const delta = deltaPx / PX_PER_SEC;
      const { prev, next } = neighbours(d.id);
      const lo = prev ? prev.end + gapS : 0;
      const hi = next ? next.start - gapS : total;
      let start = d.origStart;
      let end = d.origEnd;
      if (d.mode === "start") {
        start = Math.min(Math.max(d.origStart + delta, lo), end - MIN_DUR_S);
      } else if (d.mode === "end") {
        end = Math.max(Math.min(d.origEnd + delta, hi), start + MIN_DUR_S);
      } else {
        const dur = d.origEnd - d.origStart;
        start = Math.min(Math.max(d.origStart + delta, lo), hi - dur);
        end = start + dur;
      }
      setPreview({ id: d.id, start, end });
    },
    [neighbours, gapS, total]
  );

  const onPointerUp = useCallback(
    (e, seg) => {
      const d = dragRef.current;
      dragRef.current = null;
      if (!d) return;
      const p = preview;
      setPreview(null);
      if (!d.moved) {
        // Treated as a click: focus the line + seek to its start.
        onFocus?.(seg._id);
        onSeek?.(seg.start);
        return;
      }
      if (p && (Math.abs(p.start - seg.start) > 1e-3 || Math.abs(p.end - seg.end) > 1e-3)) {
        onTimingChange?.(seg._id, p.start, p.end);
      }
    },
    [preview, onFocus, onSeek, onTimingChange]
  );

  // Click the empty lane / ruler to seek.
  const onLaneClick = useCallback(
    (e) => {
      if (dragRef.current) return;
      const rect = laneRef.current?.getBoundingClientRect();
      if (!rect) return;
      const x = e.clientX - rect.left + (scrollRef.current?.scrollLeft || 0);
      onSeek?.(Math.max(0, x / PX_PER_SEC));
    },
    [onSeek]
  );

  // Keep the playhead in view while playing (throttled by tracking the last
  // segment we scrolled to, same pattern as the list view).
  useEffect(() => {
    const sc = scrollRef.current;
    if (!sc) return;
    const x = currentTime * PX_PER_SEC;
    const view = sc.scrollLeft;
    const w = sc.clientWidth;
    if ((x < view + 40 || x > view + w - 80) && typeof sc.scrollTo === "function") {
      sc.scrollTo({ left: Math.max(0, x - w * 0.4), behavior: "smooth" });
    }
  }, [currentTime]);

  // Ruler ticks every 10s (labelled) with minor ticks at 5s.
  const ticks = [];
  for (let s = 0; s <= total; s += 5) ticks.push(s);

  return (
    <div className="rounded-card bg-surface-2/40 ring-1 ring-white/[0.05] overflow-hidden animate-fade-in">
      {/* Header */}
      <div className="flex items-center justify-between px-3 py-2 border-b border-white/[0.05]">
        <span className="text-[11px] uppercase tracking-wider text-ink-tertiary font-semibold">
          Línea de tiempo
        </span>
        <button
          onClick={onReset}
          className="text-[11px] font-medium px-2.5 py-1 rounded-md text-ink-secondary
            ring-1 ring-white/[0.08] hover:ring-white/20 hover:text-white transition-colors flex items-center gap-1.5"
          title="Volver los timings al estado original"
        >
          <svg className="w-3 h-3" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
            <path d="M3 12a9 9 0 1 0 3-6.7L3 8" strokeLinecap="round" strokeLinejoin="round" />
            <path d="M3 3v5h5" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          Resetear timings
        </button>
      </div>

      {/* Scrollable timeline */}
      <div
        ref={scrollRef}
        className="overflow-x-auto overflow-y-hidden"
        onPointerMove={onPointerMove}
      >
        <div style={{ width: laneWidth, minWidth: "100%" }}>
          {/* Ruler */}
          <div className="relative h-6 select-none cursor-pointer" onClick={onLaneClick}>
            {ticks.map((s) => (
              <div key={s} className="absolute top-0 bottom-0" style={{ left: s * PX_PER_SEC }}>
                <div className={`w-px ${s % 10 === 0 ? "h-3 bg-white/20" : "h-2 bg-white/10"}`} />
                {s % 10 === 0 && (
                  <span className="absolute top-2.5 left-1 text-[9px] text-ink-tertiary tabular-nums">
                    {fmt(s)}
                  </span>
                )}
              </div>
            ))}
          </div>

          {/* Lane with blocks */}
          <div
            ref={laneRef}
            className="relative cursor-pointer"
            style={{ height: LANE_H + 12 }}
            onClick={onLaneClick}
          >
            {segments.map((seg) => {
              const pv = preview && preview.id === seg._id ? preview : null;
              const start = pv ? pv.start : seg.start;
              const end = pv ? pv.end : seg.end;
              const left = start * PX_PER_SEC;
              const width = Math.max(2, (end - start) * PX_PER_SEC);
              const isActive = seg._id === activeId;
              const isFocused = seg._id === focusedSegId;
              const isLocked = !!seg.locked || !!pv;
              const isHi = highlightedIds?.has?.(seg._id);
              return (
                <div
                  key={seg._id}
                  className={[
                    "absolute top-1.5 rounded-md overflow-hidden text-[11px] leading-tight",
                    "ring-1 transition-colors",
                    isActive ? "bg-brand/30" : "bg-surface-3/50",
                    isLocked ? "ring-brand/60" : "ring-white/[0.08]",
                    isFocused ? "outline outline-1 outline-brand-light" : "",
                    isHi ? "ring-2 ring-accent" : "",
                  ].join(" ")}
                  style={{ left, width, height: LANE_H }}
                  onPointerDown={(e) => onPointerDown(e, seg, "move")}
                  onPointerMove={onPointerMove}
                  onPointerUp={(e) => onPointerUp(e, seg)}
                  title={`${fmt(start)} → ${fmt(end)}`}
                >
                  {/* left edge handle */}
                  <div
                    className="absolute left-0 top-0 bottom-0 cursor-ew-resize bg-brand/40 hover:bg-brand/70"
                    style={{ width: EDGE_PX, touchAction: "none" }}
                    onPointerDown={(e) => onPointerDown(e, seg, "start")}
                    onPointerMove={onPointerMove}
                    onPointerUp={(e) => onPointerUp(e, seg)}
                  />
                  {/* right edge handle */}
                  <div
                    className="absolute right-0 top-0 bottom-0 cursor-ew-resize bg-brand/40 hover:bg-brand/70"
                    style={{ width: EDGE_PX, touchAction: "none" }}
                    onPointerDown={(e) => onPointerDown(e, seg, "end")}
                    onPointerMove={onPointerMove}
                    onPointerUp={(e) => onPointerUp(e, seg)}
                  />
                  <div className="px-2.5 py-1.5 h-full flex flex-col justify-center" style={{ touchAction: "none" }}>
                    <span className="text-[9px] text-ink-tertiary tabular-nums">{fmt(start)}</span>
                    <span className="text-white/90 line-clamp-2">{seg.text}</span>
                  </div>
                </div>
              );
            })}

            {/* Playhead */}
            <div
              className="absolute top-0 bottom-0 w-0.5 bg-brand pointer-events-none z-10 transition-[left] duration-100 ease-linear"
              style={{ left: currentTime * PX_PER_SEC }}
            >
              <div className="w-2 h-2 rounded-full bg-brand -ml-[3px] -mt-0.5" />
            </div>
          </div>
        </div>
      </div>

      <p className="px-3 py-2 text-[10px] text-ink-tertiary border-t border-white/[0.05]">
        Arrastrá el borde derecho para ajustar cuándo SALE cada línea · el cuerpo para moverla · click para ir a ese punto.
        Las líneas que ajustes manualmente quedan fijas (no se auto-extienden).
      </p>
    </div>
  );
}
