/**
 * Lyrics timing workspace.
 *
 * The timeline intentionally follows a DAW interaction model:
 * - click anywhere on the canvas/ruler/waveform to seek;
 * - drag the empty canvas to marquee-select lines;
 * - Cmd/Ctrl-click toggles individual lines;
 * - dragging a selected line moves the whole group;
 * - the left/right edges adjust entry/exit independently.
 *
 * The component is still a view over LyricsEditor state. It owns only visual
 * interaction state; persistence and autosave remain in the parent.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useI18n } from "../i18n";

const ZOOM_DEFAULT = 90;
const ZOOM_MIN = 30;
const ZOOM_MAX = 260;
const ZOOM_STEP = 20;
const ROW_H = 48;
const WAVE_H = 46;
const MIN_BLOCK_PX = 56;
const EDGE_PX = 9;
const MIN_DUR_S = 0.3;
const CLICK_SLOP_PX = 5;
const FOLLOW_SUPPRESS_MS = 2500;
const FOLLOW_LOCK_MS = 750;

function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value));
}

function fmt(sec) {
  if (!isFinite(sec) || sec < 0) sec = 0;
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

function nearestTick(step) {
  if (step <= 5) return 1;
  if (step <= 15) return 5;
  if (step <= 45) return 10;
  return 30;
}

function normalizeTimelineSegments(input) {
  if (!Array.isArray(input)) return [];
  return input.map((segment, index) => {
    const rawStart = segment?.start ?? segment?.startTime ?? segment?.start_time ?? 0;
    const rawEnd = segment?.end ?? segment?.endTime ?? segment?.end_time;
    const parsedStart = Number(rawStart);
    const parsedEnd = Number(rawEnd);
    const start = Number.isFinite(parsedStart) ? Math.max(0, parsedStart) : 0;
    const end = Number.isFinite(parsedEnd) ? Math.max(start + MIN_DUR_S, parsedEnd) : start + 1;
    return {
      ...segment,
      _id: segment?._id ?? index,
      start,
      end,
      text: segment?.text == null ? "" : String(segment.text),
    };
  });
}

export default function LyricsTimeline({
  segments,
  duration,
  currentTime,
  isPlaying = false,
  activeId,
  focusedSegId,
  highlightedIds,
  waveform = null,
  gapS = 0.05,
  saveStatus = "idle",
  onSeek,
  onDragStart,
  onTimingChange,
  onTimingChangeBatch,
  onTextChange,
  onFocus,
  onReset,
  focusMode = false,
}) {
  const i18n = useI18n?.() || {};
  const t = (key, fallback) => i18n.t?.(key) || fallback || key;
  const scrollRef = useRef(null);
  const surfaceRef = useRef(null);
  const trackRef = useRef(null);
  const canvasRef = useRef(null);
  const dragRef = useRef(null);
  const marqueeRef = useRef(null);
  const followTimerRef = useRef(null);
  const followLockRef = useRef(false);
  const lastInteractionRef = useRef(0);
  const clickSuppressRef = useRef(false);
  const [viewportWidth, setViewportWidth] = useState(960);
  const [selectedIds, setSelectedIds] = useState(() => new Set());
  const [selectionMode, setSelectionMode] = useState(false);
  const [marquee, setMarquee] = useState(null);
  const [preview, setPreview] = useState(null);
  const [pxPerSec, setPxPerSec] = useState(ZOOM_DEFAULT);
  const [editingTextId, setEditingTextId] = useState(null);
  const [draftText, setDraftText] = useState("");
  const [followEnabled, setFollowEnabled] = useState(true);
  const [followSuppressed, setFollowSuppressed] = useState(false);

  const normalizedSegments = useMemo(() => normalizeTimelineSegments(segments), [segments]);
  const total = Math.max(Number(duration) || 0, ...normalizedSegments.map((s) => s.end), 1);
  const trackWidth = Math.max(viewportWidth, total * pxPerSec + 120);
  const trackHeight = Math.max(ROW_H, normalizedSegments.length * ROW_H);
  const rowOfId = useMemo(() => new Map(normalizedSegments.map((s, index) => [s._id, index])), [normalizedSegments]);
  const ticks = useMemo(() => {
    const step = nearestTick(120 / pxPerSec);
    const out = [];
    for (let time = 0; time <= total + step; time += step) out.push(time);
    return out;
  }, [pxPerSec, total]);

  const markInteraction = useCallback(() => {
    lastInteractionRef.current = Date.now();
    setFollowSuppressed(true);
    if (followTimerRef.current) clearTimeout(followTimerRef.current);
    followTimerRef.current = setTimeout(() => setFollowSuppressed(false), FOLLOW_SUPPRESS_MS);
  }, []);

  useEffect(() => () => {
    if (followTimerRef.current) clearTimeout(followTimerRef.current);
  }, []);

  useEffect(() => {
    const node = scrollRef.current;
    if (!node) return undefined;
    const measure = () => setViewportWidth(Math.max(320, node.clientWidth || 960));
    measure();
    if (typeof ResizeObserver === "undefined") {
      window.addEventListener("resize", measure);
      return () => window.removeEventListener("resize", measure);
    }
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    const valid = new Set(normalizedSegments.map((s) => s._id));
    setSelectedIds((previous) => {
      const next = new Set([...previous].filter((id) => valid.has(id)));
      return next.size === previous.size ? previous : next;
    });
  }, [normalizedSegments]);

  const xToTime = useCallback((clientX) => {
    const rect = surfaceRef.current?.getBoundingClientRect();
    if (!rect) return 0;
    return clamp((clientX - rect.left) / pxPerSec, 0, total);
  }, [pxPerSec, total]);

  const seekAt = useCallback((clientX) => {
    markInteraction();
    onSeek?.(xToTime(clientX));
  }, [markInteraction, onSeek, xToTime]);

  const suppressNextClick = useCallback(() => {
    clickSuppressRef.current = true;
    window.setTimeout(() => { clickSuppressRef.current = false; }, 0);
  }, []);

  const toggleSelection = useCallback((id) => {
    setSelectedIds((previous) => {
      const next = new Set(previous);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const beginMarquee = useCallback((event, anchorId = null) => {
    if (event.button !== 0) return;
    event.preventDefault();
    event.stopPropagation();
    event.currentTarget.setPointerCapture?.(event.pointerId);
    marqueeRef.current = {
      originX: event.clientX,
      originY: event.clientY,
      currentX: event.clientX,
      currentY: event.clientY,
      anchorId,
      moved: false,
    };
    setMarquee({ originX: event.clientX, originY: event.clientY, currentX: event.clientX, currentY: event.clientY });
  }, []);

  const updateMarquee = useCallback((event) => {
    const current = marqueeRef.current;
    if (!current) return;
    current.currentX = event.clientX;
    current.currentY = event.clientY;
    if (Math.abs(current.currentX - current.originX) > CLICK_SLOP_PX || Math.abs(current.currentY - current.originY) > CLICK_SLOP_PX) {
      current.moved = true;
    }
    setMarquee({ ...current });
  }, []);

  const finishMarquee = useCallback((event) => {
    const current = marqueeRef.current;
    marqueeRef.current = null;
    setMarquee(null);
    if (!current) return;
    event.stopPropagation();
    if (!current.moved) {
      suppressNextClick();
      if (current.anchorId != null) toggleSelection(current.anchorId);
      else seekAt(event.clientX);
      return;
    }
    const trackRect = trackRef.current?.getBoundingClientRect();
    const surfaceRect = surfaceRef.current?.getBoundingClientRect();
    if (!trackRect || !surfaceRect) return;
    const left = Math.min(current.originX, current.currentX) - surfaceRect.left;
    const right = Math.max(current.originX, current.currentX) - surfaceRect.left;
    const top = Math.min(current.originY, current.currentY) - trackRect.top;
    const bottom = Math.max(current.originY, current.currentY) - trackRect.top;
    const from = clamp(left / pxPerSec, 0, total);
    const to = clamp(right / pxPerSec, 0, total);
    const ids = normalizedSegments
      .filter((segment) => {
        const row = rowOfId.get(segment._id) ?? 0;
        const rowTop = row * ROW_H;
        const rowBottom = rowTop + ROW_H;
        return rowBottom >= top && rowTop <= bottom && segment.end >= from && segment.start <= to;
      })
      .map((segment) => segment._id);
    setSelectedIds(new Set(ids));
    markInteraction();
  }, [markInteraction, normalizedSegments, onSeek, pxPerSec, rowOfId, seekAt, toggleSelection, total]);

  const startDrag = useCallback((event, segment, mode) => {
    event.preventDefault();
    event.stopPropagation();
    if (event.metaKey || event.ctrlKey || event.shiftKey) {
      toggleSelection(segment._id);
      return;
    }
    if (selectionMode) {
      beginMarquee(event, segment._id);
      return;
    }
    const movingGroup = mode === "move" && selectedIds.has(segment._id) && selectedIds.size > 1;
    const snapshots = (movingGroup ? normalizedSegments.filter((item) => selectedIds.has(item._id)) : [segment])
      .map((item) => ({ id: item._id, start: item.start, end: item.end }));
    if (!selectedIds.has(segment._id)) setSelectedIds(new Set([segment._id]));
    dragRef.current = {
      id: segment._id,
      mode,
      originX: event.clientX,
      snapshots,
      origStart: segment.start,
      origEnd: segment.end,
      moved: false,
      pxPerSec,
    };
    event.currentTarget.setPointerCapture?.(event.pointerId);
    markInteraction();
    setPreview(movingGroup ? { changes: snapshots } : { id: segment._id, start: segment.start, end: segment.end });
  }, [beginMarquee, markInteraction, normalizedSegments, onDragStart, pxPerSec, selectedIds, selectionMode, toggleSelection]);

  const updateDrag = useCallback((event) => {
    const drag = dragRef.current;
    if (!drag) {
      updateMarquee(event);
      return;
    }
    const delta = (event.clientX - drag.originX) / drag.pxPerSec;
    if (Math.abs(event.clientX - drag.originX) > CLICK_SLOP_PX) {
      drag.moved = true;
      if (!drag.historyStarted) {
        drag.historyStarted = true;
        onDragStart?.();
      }
    }
    if (drag.snapshots.length > 1) {
      const minStart = Math.min(...drag.snapshots.map((item) => item.start));
      const maxEnd = Math.max(...drag.snapshots.map((item) => item.end));
      const safeDelta = clamp(delta, -minStart, total - maxEnd);
      setPreview({ changes: drag.snapshots.map((item) => ({ id: item.id, start: item.start + safeDelta, end: item.end + safeDelta })) });
      return;
    }
    let start = drag.origStart;
    let end = drag.origEnd;
    if (drag.mode === "start") start = clamp(drag.origStart + delta, 0, drag.origEnd - MIN_DUR_S);
    else if (drag.mode === "end") end = clamp(drag.origEnd + delta, drag.origStart + MIN_DUR_S, total);
    else {
      const durationS = drag.origEnd - drag.origStart;
      start = clamp(drag.origStart + delta, 0, total - durationS);
      end = start + durationS;
    }
    setPreview({ id: drag.id, start, end });
  }, [onDragStart, total, updateMarquee]);

  const finishDrag = useCallback((event, segment) => {
    if (marqueeRef.current) {
      finishMarquee(event);
      return;
    }
    const drag = dragRef.current;
    dragRef.current = null;
    const current = preview;
    setPreview(null);
    if (!drag || !current) return;
    event.stopPropagation();
    if (!drag.moved) {
      onFocus?.(segment._id);
      seekAt(event.clientX);
      return;
    }
    if (current.changes?.length > 1) {
      const changes = current.changes.filter((change) => {
        const original = drag.snapshots.find((item) => item.id === change.id);
        return original && (Math.abs(original.start - change.start) > 1e-3 || Math.abs(original.end - change.end) > 1e-3);
      });
      if (changes.length) onTimingChangeBatch?.(changes);
      return;
    }
    if (Math.abs(current.start - segment.start) > 1e-3 || Math.abs(current.end - segment.end) > 1e-3) {
      onTimingChange?.(segment._id, current.start, current.end);
    }
  }, [finishMarquee, onFocus, onTimingChange, onTimingChangeBatch, preview, seekAt]);

  const fitTimeline = useCallback(() => {
    setPxPerSec(clamp(viewportWidth / Math.max(total, 1), ZOOM_MIN, 140));
  }, [total, viewportWidth]);

  const scrollToPlayhead = useCallback(() => {
    const sc = scrollRef.current;
    if (!sc || typeof sc.scrollTo !== "function" || followLockRef.current || Date.now() - lastInteractionRef.current < FOLLOW_SUPPRESS_MS) return;
    const x = currentTime * pxPerSec;
    const left = sc.scrollLeft;
    const right = left + sc.clientWidth;
    if (x < left + 80 || x > right - 120) {
      followLockRef.current = true;
      sc.scrollTo({ left: Math.max(0, x - sc.clientWidth * 0.4), behavior: "smooth" });
      window.setTimeout(() => { followLockRef.current = false; }, FOLLOW_LOCK_MS);
    }
  }, [currentTime, pxPerSec]);

  useEffect(() => {
    if (isPlaying && followEnabled) scrollToPlayhead();
  }, [isPlaying, followEnabled, scrollToPlayhead]);

  useEffect(() => {
    const canvas = canvasRef.current;
    const peaks = waveform?.peaks;
    if (!canvas || !peaks?.length) return;
    const ctx = canvas.getContext?.("2d");
    if (!ctx) return;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.round(trackWidth * dpr);
    canvas.height = Math.round(WAVE_H * dpr);
    canvas.style.width = `${trackWidth}px`;
    canvas.style.height = `${WAVE_H}px`;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, trackWidth, WAVE_H);
    const stride = Math.max(1, Math.ceil(peaks.length / Math.max(1, Math.floor(trackWidth / 4))));
    ctx.fillStyle = "rgba(139,124,246,0.45)";
    for (let i = 0; i < peaks.length; i += stride) {
      const x = (i / peaks.length) * trackWidth;
      const height = Math.max(2, peaks[i] * (WAVE_H - 8));
      ctx.fillRect(x, (WAVE_H - height) / 2, Math.max(1, 2.5 * stride), height);
    }
  }, [trackWidth, waveform]);

  const startTextEdit = useCallback((segment) => {
    setEditingTextId(segment._id);
    setDraftText(segment.text || "");
  }, []);
  const commitTextEdit = useCallback((id) => {
    setEditingTextId(null);
    onTextChange?.(id, draftText);
  }, [draftText, onTextChange]);

  const handleKeyDown = (event) => {
    if (event.key === "Escape") setSelectedIds(new Set());
  };

  const tickStep = nearestTick(120 / pxPerSec);

  return (
    <div
      className="rounded-2xl bg-surface-2/40 ring-1 ring-white/[0.07] overflow-hidden animate-fade-in"
      tabIndex={0}
      onKeyDown={handleKeyDown}
      aria-label={t("timeline.workspace", "Studio de tiempos")}
    >
      <div className="flex items-center justify-between gap-3 px-4 py-3 border-b border-white/[0.07] flex-wrap">
        <div className="flex items-center gap-3 min-w-0">
          <div>
            <p className="text-xs font-semibold text-white tracking-wide">{t("timeline.title", "Studio de tiempos")}</p>
            <p className="text-[10px] text-ink-tertiary">{t("timeline.subtitle", "Click para reproducir · arrastrá para seleccionar")}</p>
          </div>
          {saveStatus === "saving" && <span className="text-[10px] text-ink-tertiary animate-pulse">{t("timeline.saving", "Guardando…")}</span>}
          {saveStatus === "saved" && <span className="text-[10px] text-emerald-300">✓ {t("timeline.saved", "Guardado")}</span>}
          {saveStatus === "error" && <span className="text-[10px] text-red-300">{t("timeline.save_error", "Sin guardar")}</span>}
          <span className="text-[10px] text-ink-tertiary tabular-nums" aria-live="polite">
            {normalizedSegments.length} {t("timeline.lines_count", "líneas")}
          </span>
        </div>
        <div className="flex items-center gap-1.5 flex-wrap">
          <button
            type="button"
            onClick={() => setSelectionMode((value) => !value)}
            aria-pressed={selectionMode}
            title={t("timeline.select_hint", "Arrastrá sobre las líneas para seleccionar")}
            className={`h-8 px-3 rounded-lg text-[11px] font-medium ring-1 transition-colors ${selectionMode ? "bg-brand/20 text-brand-light ring-brand/40" : "text-ink-secondary ring-white/[0.1] hover:text-white hover:bg-white/[0.05]"}`}
          >
            {selectionMode ? t("timeline.selecting", "Seleccionando") : t("timeline.select", "Seleccionar líneas")}
          </button>
          {selectedIds.size > 0 && (
            <span className="inline-flex items-center gap-1.5 h-8 px-2.5 rounded-lg bg-brand/10 text-brand-light text-[11px]" aria-live="polite">
              {selectedIds.size} {t("timeline.selected", "seleccionadas")}
              <button type="button" onClick={() => setSelectedIds(new Set())} className="text-brand-light/70 hover:text-white" aria-label={t("timeline.clear_selection", "Limpiar selección")}>×</button>
            </span>
          )}
          <button
            type="button"
            onClick={() => setFollowEnabled((value) => !value)}
            aria-pressed={followEnabled}
            title={t("timeline.follow_hint", "Mantener la línea activa visible")}
            className={`h-8 px-3 rounded-lg text-[11px] ring-1 transition-colors ${followEnabled && !followSuppressed ? "text-brand-light bg-brand/10 ring-brand/30" : "text-ink-secondary ring-white/[0.1] hover:text-white"}`}
          >
            {followEnabled && followSuppressed ? t("timeline.resume", "Reanudar seguimiento") : t("timeline.follow", "Seguir reproducción")}
          </button>
          <button type="button" onClick={fitTimeline} className="h-8 px-3 rounded-lg text-[11px] text-ink-secondary ring-1 ring-white/[0.1] hover:text-white">{t("timeline.fit", "Ajustar")}</button>
          <div className="inline-flex items-center h-8 rounded-lg ring-1 ring-white/[0.1] overflow-hidden">
            <button type="button" onClick={() => setPxPerSec((value) => Math.max(ZOOM_MIN, value - ZOOM_STEP))} className="w-8 h-full text-ink-secondary hover:text-white hover:bg-white/[0.06]" aria-label={t("timeline.zoom_out", "Alejar")}>−</button>
            <span className="px-2 text-[10px] text-ink-tertiary tabular-nums">{Math.round(pxPerSec)} px/s</span>
            <button type="button" onClick={() => setPxPerSec((value) => Math.min(ZOOM_MAX, value + ZOOM_STEP))} className="w-8 h-full text-ink-secondary hover:text-white hover:bg-white/[0.06]" aria-label={t("timeline.zoom_in", "Acercar")}>+</button>
          </div>
          <button type="button" onClick={onReset} className="h-8 px-3 rounded-lg text-[11px] text-ink-secondary ring-1 ring-white/[0.1] hover:text-white">{t("timeline.reset", "Restaurar")}</button>
        </div>
      </div>

      <div ref={scrollRef} data-testid="timeline-scroll" className="overflow-auto overscroll-contain" style={{ maxHeight: focusMode ? "calc(100vh - 220px)" : "min(620px, calc(100vh - 300px))" }}>
        <div ref={surfaceRef} className="relative min-w-full" style={{ width: trackWidth }}>
          <div className="relative h-9 border-b border-white/[0.06] bg-surface-1/80" onClick={(event) => seekAt(event.clientX)}>
            {ticks.map((time) => (
              <div key={time} className="absolute top-0 bottom-0 border-l border-white/[0.08] pointer-events-none" style={{ left: time * pxPerSec }}>
                <span className="absolute top-1 left-1 text-[9px] text-ink-tertiary tabular-nums whitespace-nowrap">{fmt(time)}</span>
              </div>
            ))}
          </div>

          <div className="relative h-12 border-b border-white/[0.06] bg-brand/[0.035]" onClick={(event) => seekAt(event.clientX)}>
            <span className="absolute left-3 top-1 text-[9px] uppercase tracking-wider text-ink-tertiary pointer-events-none">{t("timeline.waveform", "forma de onda")}</span>
            {waveform?.peaks?.length ? <canvas ref={canvasRef} className="absolute inset-0 pointer-events-none opacity-80" aria-hidden="true" /> : null}
          </div>

          <div
            ref={trackRef}
            data-testid="timeline-lane"
            className={`relative ${selectionMode ? "cursor-crosshair" : "cursor-crosshair"}`}
            style={{ height: trackHeight }}
            onPointerDown={(event) => beginMarquee(event)}
            onPointerMove={updateMarquee}
            onPointerUp={finishMarquee}
            onClick={(event) => {
              event.stopPropagation();
              if (!marqueeRef.current && !clickSuppressRef.current) seekAt(event.clientX);
            }}
          >
            {normalizedSegments.map((segment, index) => {
              const previewItem = preview?.changes
                ? preview.changes.find((change) => change.id === segment._id) || null
                : preview?.id === segment._id ? preview : null;
              const start = previewItem?.start ?? segment.start;
              const end = previewItem?.end ?? segment.end;
              const width = Math.max(MIN_BLOCK_PX, (end - start) * pxPerSec);
              const isSelected = selectedIds.has(segment._id);
              const isActive = activeId === segment._id;
              const isFocused = focusedSegId === segment._id;
              const isRecent = highlightedIds?.has?.(segment._id);
              return (
                <div
                  key={segment._id}
                  title={`${fmt(start)} → ${fmt(end)}`}
                  data-testid="timeline-segment"
                  className={`absolute z-10 rounded-xl overflow-hidden ring-1 transition-colors select-none ${isActive ? "bg-brand/45" : "bg-surface-3/90"} ${isSelected ? "ring-2 ring-brand-light bg-brand/35" : "ring-white/[0.18]"} ${isFocused ? "outline outline-1 outline-white/80" : ""} ${isRecent ? "ring-accent" : ""}`}
                  style={{ left: start * pxPerSec, top: index * ROW_H + 7, width, height: ROW_H - 14, touchAction: "none" }}
                  onPointerDown={(event) => startDrag(event, segment, "move")}
                  onPointerMove={updateDrag}
                  onPointerUp={(event) => finishDrag(event, segment)}
                  onClick={(event) => event.stopPropagation()}
                >
                  <div className="absolute left-0 top-0 bottom-0 w-2 cursor-ew-resize bg-brand/25 hover:bg-brand/80" title={t("timeline.drag_start", "Arrastrá: cuándo ENTRA la línea")} onPointerDown={(event) => startDrag(event, segment, "start")} onPointerMove={updateDrag} onPointerUp={(event) => finishDrag(event, segment)} />
                  <div className="absolute right-0 top-0 bottom-0 w-2 cursor-ew-resize bg-brand/25 hover:bg-brand/80" title={t("timeline.drag_end", "Arrastrá: cuándo SALE la línea")} onPointerDown={(event) => startDrag(event, segment, "end")} onPointerMove={updateDrag} onPointerUp={(event) => finishDrag(event, segment)} />
                  <div className="flex items-center gap-2 h-full px-3 pl-4 pointer-events-none">
                    <span className="text-[9px] text-ink-tertiary tabular-nums shrink-0">{fmt(start)}</span>
                    {editingTextId === segment._id ? (
                      <input
                        autoFocus
                        type="text"
                        value={draftText}
                        onChange={(event) => setDraftText(event.target.value)}
                        onPointerDown={(event) => event.stopPropagation()}
                        onClick={(event) => event.stopPropagation()}
                        onBlur={() => commitTextEdit(segment._id)}
                        onKeyDown={(event) => {
                          if (event.key === "Enter") { event.preventDefault(); commitTextEdit(segment._id); }
                          if (event.key === "Escape") { event.preventDefault(); setEditingTextId(null); }
                        }}
                        className="pointer-events-auto min-w-0 flex-1 bg-surface-1 border border-brand/50 rounded px-1 py-0.5 text-xs text-white outline-none"
                      />
                    ) : (
                      <span className="min-w-0 truncate text-xs text-white/90 pointer-events-auto cursor-text" onDoubleClick={(event) => { event.stopPropagation(); startTextEdit(segment); }} title={`${segment.text}\n\n— Doble-click para corregir`}>{segment.text}</span>
                    )}
                  </div>
                </div>
              );
            })}

            {normalizedSegments.map((segment, index) => (
              <div key={`row-${segment._id}`} className="absolute left-0 right-0 border-b border-white/[0.035] pointer-events-none" style={{ top: index * ROW_H + ROW_H - 1 }} />
            ))}

            {normalizedSegments.length === 0 && (
              <div className="absolute inset-0 grid place-items-center text-center pointer-events-none">
                <div className="rounded-xl bg-surface-2/80 ring-1 ring-white/[0.1] px-4 py-3">
                  <p className="text-xs text-white">{t("timeline.empty_title", "No hay líneas para ajustar")}</p>
                  <p className="mt-1 text-[10px] text-ink-tertiary">{t("timeline.empty_hint", "Volvé a la vista básica o recargá la letra")}</p>
                </div>
              </div>
            )}

            {marquee && (
              <div className="absolute z-20 rounded-lg bg-brand/15 ring-1 ring-brand/70 pointer-events-none" style={{ left: Math.min(marquee.originX, marquee.currentX) - (surfaceRef.current?.getBoundingClientRect().left || 0), top: Math.min(marquee.originY, marquee.currentY) - (trackRef.current?.getBoundingClientRect().top || 0), width: Math.abs(marquee.currentX - marquee.originX), height: Math.abs(marquee.currentY - marquee.originY) }} aria-hidden="true" />
            )}

            <div className="absolute top-0 bottom-0 z-30 w-px bg-brand-light shadow-[0_0_10px_rgba(167,139,250,.9)] pointer-events-none" style={{ left: currentTime * pxPerSec }}>
              <span className="absolute -top-1 -left-1.5 w-3 h-3 rounded-full bg-brand-light" />
            </div>
          </div>
        </div>
      </div>

      <div className="flex items-center justify-between gap-3 px-4 py-2.5 border-t border-white/[0.06] text-[10px] text-ink-tertiary flex-wrap">
        <span><strong className="text-ink-secondary">{t("timeline.click_label", "Click")}</strong> {t("timeline.click_help", "reproduce desde ese punto")}</span>
        <span><strong className="text-ink-secondary">{t("timeline.drag_label", "Arrastrar")}</strong> {t("timeline.drag_help", "selecciona o mueve líneas")}</span>
        <span><strong className="text-ink-secondary">{t("timeline.modifier_label", "Cmd/Ctrl-click")}</strong> {t("timeline.modifier_help", "agrega o quita una línea")}</span>
        <span><strong className="text-ink-secondary">{t("timeline.space_label", "Space")}</strong> {t("timeline.space_help", "reproducir / pausar")}</span>
      </div>
    </div>
  );
}
