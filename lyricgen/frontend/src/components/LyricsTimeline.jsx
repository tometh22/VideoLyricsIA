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

// Zoom (px per second) is operator-adjustable so dense sections can be
// spread out. Defaults higher than the cramped v1 (was a fixed 30).
const ZOOM_DEFAULT = 45;
const ZOOM_MIN = 15;
const ZOOM_MAX = 90;
const ZOOM_STEP = 15;
const EDGE_PX = 12;           // width of the start/end grab handles
const MIN_DUR_S = 0.3;        // shortest readable on-screen window
const MIN_BLOCK_PX = 36;      // floor so short lines stay grabbable at any zoom
const CLICK_SLOP_PX = 4;      // movement under this = click (focus/seek), not drag
const LANE_H = 76;            // block height in px (taller = less cramped)
const FOLLOW_SUPPRESS_MS = 2500; // pause auto-follow this long after manual scroll

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
  isPlaying = false,   // gate auto-follow so manual scroll isn't yanked back
  activeId,
  focusedSegId,
  highlightedIds,      // Set<_id>
  gapS = 0.05,
  saveStatus = "idle", // "idle" | "saving" | "saved" — autosave confidence chip
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
  // Operator-controlled zoom (px/s). Higher = more spread out.
  const [pxPerSec, setPxPerSec] = useState(ZOOM_DEFAULT);
  // Timestamp of the last manual scroll/interaction — auto-follow pauses for
  // FOLLOW_SUPPRESS_MS after it so the operator can navigate back (e.g. to
  // 0:40) without the playhead-follow yanking the view forward again.
  const lastUserScrollRef = useRef(0);
  const markUserScroll = useCallback(() => { lastUserScrollRef.current = Date.now(); }, []);

  const total = Math.max(duration || 0, ...segments.map((s) => s.end), 1);
  const laneWidth = total * pxPerSec;

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
      const delta = deltaPx / pxPerSec;
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
      markUserScroll(); // a deliberate seek shouldn't be undone by auto-follow
      const rect = laneRef.current?.getBoundingClientRect();
      if (!rect) return;
      const x = e.clientX - rect.left + (scrollRef.current?.scrollLeft || 0);
      onSeek?.(Math.max(0, x / pxPerSec));
    },
    [onSeek, pxPerSec, markUserScroll]
  );

  // Keep the playhead in view — but ONLY while playing AND when the operator
  // hasn't scrolled/clicked in the last FOLLOW_SUPPRESS_MS. v1 followed on
  // every currentTime tick, which yanked the view back to the playhead the
  // instant the operator tried to scroll elsewhere (e.g. back to 0:40 while
  // audio played at 1:19) — they literally couldn't navigate. Now manual
  // navigation wins; follow resumes a couple seconds after they stop.
  useEffect(() => {
    if (!isPlaying) return;
    if (Date.now() - lastUserScrollRef.current < FOLLOW_SUPPRESS_MS) return;
    const sc = scrollRef.current;
    if (!sc || typeof sc.scrollTo !== "function") return;
    const x = currentTime * pxPerSec;
    const view = sc.scrollLeft;
    const w = sc.clientWidth;
    if (x < view + 40 || x > view + w - 80) {
      sc.scrollTo({ left: Math.max(0, x - w * 0.4), behavior: "smooth" });
    }
  }, [currentTime, isPlaying, pxPerSec]);

  // Ruler ticks every 10s (labelled) with minor ticks at 5s.
  const ticks = [];
  for (let s = 0; s <= total; s += 5) ticks.push(s);

  return (
    <div className="rounded-card bg-surface-2/40 ring-1 ring-white/[0.05] overflow-hidden animate-fade-in">
      {/* Header */}
      <div className="flex items-center justify-between px-3 py-2.5 border-b border-white/[0.05] gap-3 flex-wrap">
        <div className="flex items-center gap-2.5">
          <span className="text-[11px] uppercase tracking-wider text-ink-tertiary font-semibold">
            Línea de tiempo
          </span>
          {/* Autosave confidence chip */}
          {saveStatus === "saving" && (
            <span className="text-[10px] text-ink-tertiary flex items-center gap-1">
              <span className="w-1.5 h-1.5 rounded-full bg-ink-tertiary animate-pulse" />
              Guardando…
            </span>
          )}
          {saveStatus === "saved" && (
            <span className="text-[10px] text-emerald-300 flex items-center gap-1 animate-fade-in">
              <svg className="w-3 h-3" fill="none" stroke="currentColor" strokeWidth="2.5" viewBox="0 0 24 24">
                <path d="M20 6 9 17l-5-5" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
              Guardado
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          {/* Zoom control */}
          <div className="inline-flex items-center rounded-md ring-1 ring-white/[0.08] overflow-hidden">
            <button
              onClick={() => setPxPerSec((z) => Math.max(ZOOM_MIN, z - ZOOM_STEP))}
              disabled={pxPerSec <= ZOOM_MIN}
              className="px-2 py-1 text-ink-secondary hover:text-white hover:bg-white/[0.05] disabled:opacity-30 transition-colors"
              title="Alejar"
              aria-label="Alejar"
            >−</button>
            <span className="px-1.5 text-[10px] text-ink-tertiary tabular-nums select-none">zoom</span>
            <button
              onClick={() => setPxPerSec((z) => Math.min(ZOOM_MAX, z + ZOOM_STEP))}
              disabled={pxPerSec >= ZOOM_MAX}
              className="px-2 py-1 text-ink-secondary hover:text-white hover:bg-white/[0.05] disabled:opacity-30 transition-colors"
              title="Acercar"
              aria-label="Acercar"
            >+</button>
          </div>
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
      </div>

      {/* Scrollable timeline */}
      <div
        ref={scrollRef}
        className="overflow-x-auto overflow-y-hidden"
        onPointerMove={onPointerMove}
        onScroll={markUserScroll}
      >
        <div style={{ width: laneWidth, minWidth: "100%" }}>
          {/* Ruler */}
          <div className="relative h-6 select-none cursor-pointer" onClick={onLaneClick}>
            {ticks.map((s) => (
              <div key={s} className="absolute top-0 bottom-0" style={{ left: s * pxPerSec }}>
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
              const left = start * pxPerSec;
              const width = Math.max(MIN_BLOCK_PX, (end - start) * pxPerSec);
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
              style={{ left: currentTime * pxPerSec }}
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
