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
// spread out; lower = more of the song on screen at once. Default is low
// on purpose: at 40 px/s a 4s line was a 160px monolith and you saw ~4
// lines of 60. At 16 px/s the same line is ~64px and ~15-20 lines fit.
const ZOOM_DEFAULT = 16;
const ZOOM_MIN = 8;
const ZOOM_MAX = 60;
const ZOOM_STEP = 8;
const EDGE_PX = 10;            // height of the top/bottom grab handles
const MIN_DUR_S = 0.3;         // shortest readable on-screen window
const MIN_BLOCK_PX = 22;       // floor so short lines stay grabbable at any zoom
const CLICK_SLOP_PX = 4;       // movement under this = click (focus/seek), not drag
const LABEL_W = 38;            // left time-label column
const WAVE_W = 30;             // waveform band width inside the gutter
const GUTTER_PX = LABEL_W + WAVE_W; // total left gutter (labels + waveform)
const MAX_VH_NORMAL = "58vh";   // viewport cap default; the lane scrolls within it
const MAX_VH_FOCUS = "85vh";    // 2026-05-25 — modo enfoque del editor agranda la lane
const FOLLOW_SUPPRESS_MS = 2500;
const INTRO_SKIP_S = 3;        // auto-scroll to first lyric if intro longer than this

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
  waveform = null,     // {peaks:[0..1], duration} | null — drawn in the gutter
  gapS = 0.05,
  saveStatus = "idle", // "idle" | "saving" | "saved"
  onSeek,              // (seconds) => void
  onDragStart,         // () => void  — push one undo snapshot before a drag commits
  onTimingChange,      // (id, newStart, newEnd) => void — commit; parent sets locked
  onTextChange,        // (id, text) => void — inline text fix without leaving timeline
  onFocus,             // (id) => void
  onReset,             // () => void
  focusMode = false,   // 2026-05-25 — passed from LyricsEditor focus toggle
}) {
  const laneRef = useRef(null);
  const scrollRef = useRef(null);
  const canvasRef = useRef(null);
  const didAutoScrollRef = useRef(false);
  const [preview, setPreview] = useState(null); // {id, start, end} | null
  const dragRef = useRef(null); // {id, mode, originY, origStart, origEnd, moved}
  const [pxPerSec, setPxPerSec] = useState(ZOOM_DEFAULT);
  const [editingTextId, setEditingTextId] = useState(null); // line whose text is being fixed inline
  const [draftText, setDraftText] = useState("");
  const lastUserScrollRef = useRef(0);

  const beginTextEdit = useCallback((seg) => {
    setEditingTextId(seg._id);
    setDraftText(seg.text || "");
  }, []);
  const commitTextEdit = useCallback((id) => {
    setEditingTextId((cur) => (cur === id ? null : cur));
    onTextChange?.(id, draftText);
  }, [draftText, onTextChange]);
  const cancelTextEdit = useCallback(() => setEditingTextId(null), []);
  const markUserScroll = useCallback(() => { lastUserScrollRef.current = Date.now(); }, []);

  const total = Math.max(duration || 0, ...segments.map((s) => s.end), 1);
  const laneHeight = total * pxPerSec;

  // INCIDENT 2026-05-25: forced_align/reconcile sometimes emits two segments
  // with (almost) identical `start` — typical of a chorus where lrclib has
  // repeated identical lines ("Legalícenla / Legalícenla / Oh-oh-oh" at the
  // top of the chorus). Before this fix every overlapping segment was
  // rendered at `top = start * pxPerSec` with full-width, so the second one
  // sat literally underneath the first — the operator saw ONE block and
  // assumed the rest were missing. Worse: those "missing" lines DID exist
  // in the segments_json (the list view showed them all), so it looked like
  // the timeline was a buggy view of correct data.
  //
  // Fix: Gantt-style lane assignment. Sort segments by start. For each
  // segment, place it in the first lane whose previous segment has already
  // ended; if none free → open a new lane. The columns then share the
  // available horizontal space equally so the operator sees ALL overlapping
  // lines side by side. When there's no overlap (the common case), the
  // single lane uses the full width — visually identical to before.
  //
  // 50ms epsilon: timestamps that close are functionally simultaneous from
  // the operator's point of view (single bar of music) and should share a
  // lane bucket. Larger gaps go in the same lane sequentially.
  const { laneOfId, laneCount } = (() => {
    const OVERLAP_EPSILON_S = 0.05;
    const sorted = [...segments].sort((a, b) => a.start - b.start);
    const laneEnds = [];                 // laneIdx → latest seg.end in that lane
    const idToLane = new Map();
    for (const s of sorted) {
      let assigned = -1;
      for (let i = 0; i < laneEnds.length; i++) {
        if (laneEnds[i] <= s.start + OVERLAP_EPSILON_S) {
          assigned = i;
          break;
        }
      }
      if (assigned < 0) {
        assigned = laneEnds.length;
        laneEnds.push(s.end);
      } else {
        laneEnds[assigned] = s.end;
      }
      idToLane.set(s._id, assigned);
    }
    return { laneOfId: idToLane, laneCount: Math.max(1, laneEnds.length) };
  })();

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

  // Draw the waveform in the gutter (static — redraws only when the peaks
  // or the time scale change, never per playhead tick). Guarded for jsdom,
  // which has no canvas 2d context.
  useEffect(() => {
    const canvas = canvasRef.current;
    const peaks = waveform?.peaks;
    if (!canvas || !peaks || !peaks.length) return;
    const ctx = canvas.getContext?.("2d");
    if (!ctx) return;
    const dpr = Math.min((typeof window !== "undefined" && window.devicePixelRatio) || 1, 2);
    const w = GUTTER_PX;
    const h = laneHeight;
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    canvas.style.width = `${w}px`;
    canvas.style.height = `${h}px`;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    const N = peaks.length;
    const cx = LABEL_W + WAVE_W / 2;
    const band = h / N;
    ctx.fillStyle = "rgba(139,124,246,0.30)"; // brand violet, faint
    for (let i = 0; i < N; i++) {
      const half = peaks[i] * (WAVE_W / 2);
      if (half <= 0) continue;
      ctx.fillRect(cx - half, i * band, half * 2, Math.max(1, band));
    }
  }, [waveform, laneHeight]);

  // On first mount, skip a long instrumental intro: jump the scroll to the
  // first lyric so the operator doesn't open to a wall of empty time.
  useEffect(() => {
    if (didAutoScrollRef.current) return;
    if (!segments.length) return;
    const firstStart = Math.min(...segments.map((s) => s.start));
    if (firstStart <= INTRO_SKIP_S) { didAutoScrollRef.current = true; return; }
    // rAF: set scrollTop AFTER the lane has its full laid-out height,
    // otherwise the browser clamps scrollTop to 0 (the bug that left the
    // view stuck at 0:00 on top of the instrumental intro).
    const raf = requestAnimationFrame(() => {
      const sc = scrollRef.current;
      if (sc) sc.scrollTop = Math.max(0, firstStart * pxPerSec - 28);
      didAutoScrollRef.current = true;
    });
    return () => cancelAnimationFrame(raf);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [segments]);

  const activeSeg = segments.find((s) => s._id === activeId) || null;

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
            className="text-label px-2.5 py-1 rounded-md text-ink-secondary
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
        style={{ maxHeight: focusMode ? MAX_VH_FOCUS : MAX_VH_NORMAL }}
        onPointerMove={onPointerMove}
        onScroll={markUserScroll}
      >
        <div
          ref={laneRef}
          className="relative cursor-pointer"
          style={{ height: laneHeight, minHeight: "100%" }}
          onClick={onLaneClick}
        >
          {/* Waveform in the gutter (canvas, time-aligned with the lane) */}
          {waveform?.peaks?.length ? (
            <>
              <canvas
                ref={canvasRef}
                className="absolute top-0 left-0 pointer-events-none"
                style={{ width: GUTTER_PX, height: laneHeight }}
                aria-hidden="true"
              />
              {/* active line's slice of the waveform, highlighted */}
              {activeSeg && (
                <div
                  className="absolute pointer-events-none bg-brand/20"
                  style={{
                    left: LABEL_W,
                    width: WAVE_W,
                    top: activeSeg.start * pxPerSec,
                    height: Math.max(2, (activeSeg.end - activeSeg.start) * pxPerSec),
                  }}
                />
              )}
            </>
          ) : null}

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
            // Gantt lane positioning: when there's no overlap (laneCount=1)
            // the block takes the full width like before; when 2+ segments
            // overlap the columns split the available space evenly so the
            // operator sees them side by side instead of stacked invisibly.
            const laneIdx = laneOfId.get(seg._id) ?? 0;
            const LANE_GAP_PCT = 1;        // small horizontal breathing space
            const laneSpan = (100 - LANE_GAP_PCT * (laneCount - 1)) / laneCount;
            const laneLeftPct = laneIdx * (laneSpan + LANE_GAP_PCT);
            return (
              <div
                key={seg._id}
                className={[
                  "absolute rounded-md overflow-hidden text-caption leading-tight ring-1 transition-colors",
                  isActive ? "bg-brand/25" : "bg-surface-3/25",
                  isLocked ? "ring-brand/60" : "ring-white/[0.07]",
                  isFocused ? "outline outline-1 outline-brand-light" : "",
                  isHi ? "ring-2 ring-accent" : "",
                ].join(" ")}
                style={{
                  top, height,
                  // Lane positioning: a CSS var sets the lane area (gutter→right);
                  // each block's left/width are percentages OF the parent's full
                  // width, but we add a translate offset and reduce the span by
                  // the gutter via calc. Result: laneCount=1 → block hugs the
                  // gutter→right strip like before. laneCount>1 → blocks share
                  // that strip evenly.
                  left: `calc(${GUTTER_PX + 4}px + (100% - ${GUTTER_PX + 12}px) * ${laneLeftPct / 100})`,
                  width: `calc((100% - ${GUTTER_PX + 12}px) * ${laneSpan / 100})`,
                }}
                onPointerDown={(e) => onPointerDown(e, seg, "move")}
                onPointerMove={onPointerMove}
                onPointerUp={(e) => onPointerUp(e, seg)}
                title={`${fmt(start)} → ${fmt(end)}`}
              >
                {/* top edge handle = start (when the line ENTERS) */}
                <div
                  className="absolute left-0 right-0 top-0 cursor-ns-resize bg-brand/30 hover:bg-brand/70 flex items-center justify-center group/ht"
                  style={{ height: EDGE_PX, touchAction: "none" }}
                  onPointerDown={(e) => onPointerDown(e, seg, "start")}
                  onPointerMove={onPointerMove}
                  onPointerUp={(e) => onPointerUp(e, seg)}
                  title="Arrastrá: cuándo ENTRA la línea"
                >
                  <div className="w-7 h-[3px] rounded-full bg-white/40 group-hover/ht:bg-white/90 transition-colors" />
                </div>
                {/* bottom edge handle = end (when the line LEAVES) */}
                <div
                  className="absolute left-0 right-0 bottom-0 cursor-ns-resize bg-brand/30 hover:bg-brand/70 flex items-center justify-center group/hb"
                  style={{ height: EDGE_PX, touchAction: "none" }}
                  onPointerDown={(e) => onPointerDown(e, seg, "end")}
                  onPointerMove={onPointerMove}
                  onPointerUp={(e) => onPointerUp(e, seg)}
                  title="Arrastrá: cuándo SALE la línea"
                >
                  <div className="w-7 h-[3px] rounded-full bg-white/40 group-hover/hb:bg-white/90 transition-colors" />
                </div>
                {/* Text anchored to the TOP (where the line enters), not
                    floating in the middle of a tall held block. */}
                <div className="px-3 flex items-start gap-2" style={{ touchAction: "none", paddingTop: EDGE_PX + 4 }}>
                  <span className="text-[9px] text-ink-tertiary tabular-nums shrink-0 mt-px">{fmt(start)}</span>
                  {editingTextId === seg._id ? (
                    <input
                      type="text"
                      autoFocus
                      value={draftText}
                      onChange={(ev) => setDraftText(ev.target.value)}
                      onPointerDown={(ev) => ev.stopPropagation()}
                      onClick={(ev) => ev.stopPropagation()}
                      onBlur={() => commitTextEdit(seg._id)}
                      onKeyDown={(ev) => {
                        if (ev.key === "Enter") { ev.preventDefault(); commitTextEdit(seg._id); }
                        else if (ev.key === "Escape") { ev.preventDefault(); cancelTextEdit(); }
                      }}
                      className="flex-1 min-w-0 bg-surface-1 border border-brand/50 focus:border-brand
                        outline-none rounded px-1 py-0.5 text-caption text-white"
                    />
                  ) : (
                    <span
                      className="text-white/90 line-clamp-2 cursor-text"
                      onPointerDown={(ev) => ev.stopPropagation()}
                      onDoubleClick={(ev) => { ev.stopPropagation(); beginTextEdit(seg); }}
                      title="Doble-click para corregir el texto"
                    >
                      {seg.text}
                    </span>
                  )}
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

      <p className="px-3 py-2 text-[10px] text-ink-tertiary border-t border-white/[0.05] flex flex-wrap items-center gap-x-3 gap-y-1">
        <span><span className="text-ink-secondary">↕ bordes</span> ajustan entra/sale</span>
        <span><span className="text-ink-secondary">cuerpo</span> mueve la línea</span>
        <span><span className="text-ink-secondary">click</span> salta a ese punto</span>
        <span><span className="text-ink-secondary">doble-click</span> corrige el texto</span>
        <span className="text-ink-tertiary/70">lo que ajustás queda fijo</span>
      </p>
    </div>
  );
}
