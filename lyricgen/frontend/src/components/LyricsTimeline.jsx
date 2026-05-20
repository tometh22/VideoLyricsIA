/**
 * Visual per-line timings editor (Rotor-style "Timings" tab), rendered as a
 * VIEW of the shared LyricsEditor — same `edited` state, no new data model.
 *
 * VERTICAL orientation: time flows top → bottom (matches the list view's
 * mental model and Rotor's actual Timings tab). Each lyric line is a
 * full-width block. The operator drags:
 *   - the TOP edge    → move START (when the line enters)
 *   - the BOTTOM edge → set END independently (when it leaves — the gap the
 *                       list view can't do)
 *   - the body        → move the whole block, preserving duration
 * A horizontal playhead tracks audio currentTime; clicking the lane seeks to
 * that exact time. Any drag marks the line `locked` so
 * pipeline._apply_display_timing respects the manual end (no auto-extend).
 *
 * All edits go up through the parent's setEdited → existing autosave /
 * onEditedChange / onApprove. This component owns NO persistence.
 */
import { useCallback, useEffect, useRef, useState } from "react";

// Zoom = px per second (vertical). Operator-adjustable so dense sections
// spread out; lower = more of the song on screen at once.
const ZOOM_DEFAULT = 40;
const ZOOM_MIN = 20;
const ZOOM_MAX = 100;
const ZOOM_STEP = 20;
const EDGE_PX = 12;            // height of the top/bottom grab handles
const MIN_DUR_S = 0.3;         // shortest readable on-screen window
const MIN_BLOCK_PX = 30;       // floor so short lines stay grabbable at any zoom
const CLICK_SLOP_PX = 4;       // movement under this = click (focus/seek), not drag
const GUTTER_PX = 52;          // left time-label gutter
const MAX_VH = "58vh";         // viewport cap; the lane scrolls within it
const FOLLOW_SUPPRESS_MS = 2500;

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
  isPlaying = false,
  activeId,
  focusedSegId,
  highlightedIds,      // Set<_id>
  gapS = 0.05,
  saveStatus = "idle", // "idle" | "saving" | "saved"
  onSeek,              // (seconds) => void
  onDragStart,         // () => void  — push one undo snapshot before a drag commits
  onTimingChange,      // (id, newStart, newEnd) => void — commit; parent sets locked
  onFocus,             // (id) => void
  onReset,             // () => void
}) {
  const laneRef = useRef(null);
  const scrollRef = useRef(null);
  const [preview, setPreview] = useState(null); // {id, start, end} | null
  const dragRef = useRef(null); // {id, mode, originY, origStart, origEnd, moved}
  const [pxPerSec, setPxPerSec] = useState(ZOOM_DEFAULT);
  const lastUserScrollRef = useRef(0);
  const markUserScroll = useCallback(() => { lastUserScrollRef.current = Date.now(); }, []);

  const total = Math.max(duration || 0, ...segments.map((s) => s.end), 1);
  const laneHeight = total * pxPerSec;

  const neighbours = useCallback(
    (id) => {
      const sorted = [...segments].sort((a, b) => a.start - b.start);
      const i = sorted.findIndex((s) => s._id === id);
      return { prev: i > 0 ? sorted[i - 1] : null, next: i < sorted.length - 1 ? sorted[i + 1] : null };
    },
    [segments]
  );

  // clientY → time. laneRef is the inner lane content (full laneHeight); its
  // rect.top already shifts with vertical scroll, so we do NOT add scrollTop.
  const clientYToTime = useCallback((clientY) => {
    const rect = laneRef.current?.getBoundingClientRect();
    if (!rect) return 0;
    return Math.max(0, (clientY - rect.top) / pxPerSec);
  }, [pxPerSec]);

  const onPointerDown = useCallback(
    (e, seg, mode) => {
      e.stopPropagation();
      e.currentTarget.setPointerCapture?.(e.pointerId);
      onDragStart?.();
      dragRef.current = {
        id: seg._id, mode, originY: e.clientY,
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
      const deltaPx = e.clientY - d.originY;
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
    [neighbours, gapS, total, pxPerSec]
  );

  const onPointerUp = useCallback(
    (e, seg) => {
      const d = dragRef.current;
      dragRef.current = null;
      if (!d) return;
      const p = preview;
      setPreview(null);
      if (!d.moved) {
        markUserScroll();
        onFocus?.(seg._id);
        onSeek?.(clientYToTime(e.clientY)); // exact clicked point, not block start
        return;
      }
      if (p && (Math.abs(p.start - seg.start) > 1e-3 || Math.abs(p.end - seg.end) > 1e-3)) {
        onTimingChange?.(seg._id, p.start, p.end);
      }
    },
    [preview, onFocus, onSeek, onTimingChange, clientYToTime, markUserScroll]
  );

  const onLaneClick = useCallback(
    (e) => {
      if (dragRef.current) return;
      markUserScroll();
      onSeek?.(clientYToTime(e.clientY));
    },
    [onSeek, clientYToTime, markUserScroll]
  );

  // Auto-follow the playhead vertically — only while playing AND when the
  // operator hasn't scrolled/clicked in the last FOLLOW_SUPPRESS_MS, so
  // manual navigation (e.g. back to 0:40) isn't yanked away.
  useEffect(() => {
    if (!isPlaying) return;
    if (Date.now() - lastUserScrollRef.current < FOLLOW_SUPPRESS_MS) return;
    const sc = scrollRef.current;
    if (!sc || typeof sc.scrollTo !== "function") return;
    const y = currentTime * pxPerSec;
    const view = sc.scrollTop;
    const h = sc.clientHeight;
    if (y < view + 40 || y > view + h - 80) {
      sc.scrollTo({ top: Math.max(0, y - h * 0.4), behavior: "smooth" });
    }
  }, [currentTime, isPlaying, pxPerSec]);

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
          <div className="inline-flex items-center rounded-md ring-1 ring-white/[0.08] overflow-hidden">
            <button
              onClick={() => setPxPerSec((z) => Math.max(ZOOM_MIN, z - ZOOM_STEP))}
              disabled={pxPerSec <= ZOOM_MIN}
              className="px-2 py-1 text-ink-secondary hover:text-white hover:bg-white/[0.05] disabled:opacity-30 transition-colors"
              title="Alejar" aria-label="Alejar"
            >−</button>
            <span className="px-1.5 text-[10px] text-ink-tertiary tabular-nums select-none">zoom</span>
            <button
              onClick={() => setPxPerSec((z) => Math.min(ZOOM_MAX, z + ZOOM_STEP))}
              disabled={pxPerSec >= ZOOM_MAX}
              className="px-2 py-1 text-ink-secondary hover:text-white hover:bg-white/[0.05] disabled:opacity-30 transition-colors"
              title="Acercar" aria-label="Acercar"
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

      {/* Scrollable timeline (vertical) */}
      <div
        ref={scrollRef}
        className="overflow-y-auto overflow-x-hidden"
        style={{ maxHeight: MAX_VH }}
        onPointerMove={onPointerMove}
        onScroll={markUserScroll}
      >
        <div
          ref={laneRef}
          className="relative cursor-pointer"
          style={{ height: laneHeight, minHeight: "100%" }}
          onClick={onLaneClick}
        >
          {/* Time gutter ticks (horizontal grid lines + labels) */}
          {ticks.map((s) => (
            <div key={s} className="absolute left-0 right-0 pointer-events-none" style={{ top: s * pxPerSec }}>
              <div className={`${s % 10 === 0 ? "bg-white/10" : "bg-white/[0.04]"}`} style={{ height: 1, marginLeft: GUTTER_PX }} />
              {s % 10 === 0 && (
                <span className="absolute left-1 -top-1.5 text-[9px] text-ink-tertiary tabular-nums">{fmt(s)}</span>
              )}
            </div>
          ))}

          {/* Blocks */}
          {segments.map((seg) => {
            const pv = preview && preview.id === seg._id ? preview : null;
            const start = pv ? pv.start : seg.start;
            const end = pv ? pv.end : seg.end;
            const top = start * pxPerSec;
            const height = Math.max(MIN_BLOCK_PX, (end - start) * pxPerSec);
            const isActive = seg._id === activeId;
            const isFocused = seg._id === focusedSegId;
            const isLocked = !!seg.locked || !!pv;
            const isHi = highlightedIds?.has?.(seg._id);
            return (
              <div
                key={seg._id}
                className={[
                  "absolute rounded-md overflow-hidden text-[12px] leading-tight ring-1 transition-colors",
                  isActive ? "bg-brand/30" : "bg-surface-3/50",
                  isLocked ? "ring-brand/60" : "ring-white/[0.08]",
                  isFocused ? "outline outline-1 outline-brand-light" : "",
                  isHi ? "ring-2 ring-accent" : "",
                ].join(" ")}
                style={{ top, height, left: GUTTER_PX + 4, right: 8 }}
                onPointerDown={(e) => onPointerDown(e, seg, "move")}
                onPointerMove={onPointerMove}
                onPointerUp={(e) => onPointerUp(e, seg)}
                title={`${fmt(start)} → ${fmt(end)}`}
              >
                {/* top edge handle = start */}
                <div
                  className="absolute left-0 right-0 top-0 cursor-ns-resize bg-brand/40 hover:bg-brand/70"
                  style={{ height: EDGE_PX, touchAction: "none" }}
                  onPointerDown={(e) => onPointerDown(e, seg, "start")}
                  onPointerMove={onPointerMove}
                  onPointerUp={(e) => onPointerUp(e, seg)}
                  title="Arrastrá: cuándo ENTRA"
                />
                {/* bottom edge handle = end */}
                <div
                  className="absolute left-0 right-0 bottom-0 cursor-ns-resize bg-brand/40 hover:bg-brand/70"
                  style={{ height: EDGE_PX, touchAction: "none" }}
                  onPointerDown={(e) => onPointerDown(e, seg, "end")}
                  onPointerMove={onPointerMove}
                  onPointerUp={(e) => onPointerUp(e, seg)}
                  title="Arrastrá: cuándo SALE"
                />
                <div className="px-3 h-full flex items-center gap-2" style={{ touchAction: "none" }}>
                  <span className="text-[9px] text-ink-tertiary tabular-nums shrink-0">{fmt(start)}</span>
                  <span className="text-white/90 line-clamp-2">{seg.text}</span>
                </div>
              </div>
            );
          })}

          {/* Playhead (horizontal) */}
          <div
            className="absolute left-0 right-0 h-0.5 bg-brand pointer-events-none z-10 transition-[top] duration-100 ease-linear"
            style={{ top: currentTime * pxPerSec }}
          >
            <div className="w-2 h-2 rounded-full bg-brand -mt-[3px] ml-0.5" />
          </div>
        </div>
      </div>

      <p className="px-3 py-2 text-[10px] text-ink-tertiary border-t border-white/[0.05]">
        Arrastrá el borde de abajo de una línea para ajustar cuándo SALE · el de arriba cuándo ENTRA ·
        el cuerpo para moverla · click en cualquier punto para ir ahí. Las líneas que ajustes manualmente
        quedan fijas (no se auto-extienden).
      </p>
    </div>
  );
}
