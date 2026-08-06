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
import { clampResizeTiming, clampSelectionShiftDelta } from "../lib/segmentTiming";

const ZOOM_DEFAULT = 48;
const ZOOM_MIN = 8;
const ZOOM_MAX = 260;
const ZOOM_STEP = 20;
const ROW_H = 48;
const WAVE_H = 46;
const MIN_BLOCK_PX = 2;
const EDGE_PX = 22;
const MIN_DUR_S = 0.3;
const CLICK_SLOP_PX = 5;
const FOLLOW_SUPPRESS_MS = 2500;
const FOLLOW_LOCK_MS = 750;
const LABEL_W = 264;

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
  onDeleteSelection,
  onSelectionCreated,
  onGroupMoved,
  focusMode = false,
}) {
  const i18n = useI18n?.() || {};
  const t = (key, fallback) => i18n.t?.(key) || fallback || key;
  const scrollRef = useRef(null);
  const trackRef = useRef(null);
  const canvasRef = useRef(null);
  const dragRef = useRef(null);
  const marqueeRef = useRef(null);
  const followTimerRef = useRef(null);
  const followLockRef = useRef(false);
  const selectionAnchorRef = useRef(null);
  const lastInteractionRef = useRef(0);
  const clickSuppressRef = useRef(false);
  const activeRowRefs = useRef(new Map());
  const [viewportWidth, setViewportWidth] = useState(0);
  const [selectedIds, setSelectedIds] = useState(() => new Set());
  const [marquee, setMarquee] = useState(null);
  const [preview, setPreview] = useState(null);
  const [pxPerSec, setPxPerSec] = useState(ZOOM_DEFAULT);
  const [moreActionsOpen, setMoreActionsOpen] = useState(false);
  const [editingTextId, setEditingTextId] = useState(null);
  const [draftText, setDraftText] = useState("");
  const [followEnabled, setFollowEnabled] = useState(true);
  const [followSuppressed, setFollowSuppressed] = useState(false);
  const [limitFeedback, setLimitFeedback] = useState("");

  const normalizedSegments = useMemo(() => normalizeTimelineSegments(segments), [segments]);
  const total = Math.max(Number(duration) || 0, ...normalizedSegments.map((s) => s.end), 1);
  const trackWidth = Math.max(Math.max(320, (viewportWidth || 960) - LABEL_W), total * pxPerSec + 24);
  const surfaceWidth = LABEL_W + trackWidth;
  const trackHeight = Math.max(ROW_H, normalizedSegments.length * ROW_H);
  const rowOfId = useMemo(() => new Map(normalizedSegments.map((s, index) => [s._id, index])), [normalizedSegments]);
  const activeRowIndex = activeId == null ? null : rowOfId.get(activeId);
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

  const fitZoom = useCallback(() => (
    clamp((Math.max(viewportWidth, 960) - LABEL_W - 24) / Math.max(total, 1), ZOOM_MIN, ZOOM_MAX)
  ), [total, viewportWidth]);

  useEffect(() => {
    const valid = new Set(normalizedSegments.map((s) => s._id));
    setSelectedIds((previous) => {
      const next = new Set([...previous].filter((id) => valid.has(id)));
      return next.size === previous.size ? previous : next;
    });
  }, [normalizedSegments]);

  const xToTime = useCallback((clientX) => {
    const rect = trackRef.current?.getBoundingClientRect();
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
    selectionAnchorRef.current = id;
    setSelectedIds((previous) => {
      const next = new Set(previous);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      onSelectionCreated?.({ count: next.size, method: "modifier" });
      return next;
    });
  }, [onSelectionCreated]);

  const selectRangeTo = useCallback((id) => {
    const targetIndex = rowOfId.get(id);
    const anchorIndex = rowOfId.get(selectionAnchorRef.current);
    if (targetIndex == null || anchorIndex == null) {
      selectionAnchorRef.current = id;
      setSelectedIds(new Set([id]));
      return;
    }
    const from = Math.min(anchorIndex, targetIndex);
    const to = Math.max(anchorIndex, targetIndex);
    const next = new Set(normalizedSegments.slice(from, to + 1).map((segment) => segment._id));
    setSelectedIds(next);
    onSelectionCreated?.({ count: next.size, method: "range" });
  }, [normalizedSegments, onSelectionCreated, rowOfId]);

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
      startedAt: performance.now(),
    };
    setMarquee({ originX: event.clientX, originY: event.clientY, currentX: event.clientX, currentY: event.clientY, anchorId });
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
    if (!trackRect) return;
    const left = Math.min(current.originX, current.currentX) - trackRect.left;
    const right = Math.max(current.originX, current.currentX) - trackRect.left;
    const top = Math.min(current.originY, current.currentY) - trackRect.top;
    const bottom = Math.max(current.originY, current.currentY) - trackRect.top;
    const from = clamp(left / pxPerSec, 0, total);
    const to = clamp(right / pxPerSec, 0, total);
    const ids = normalizedSegments
      .filter((segment) => {
        const row = rowOfId.get(segment._id) ?? 0;
        const rowTop = row * ROW_H;
        const rowBottom = rowTop + ROW_H;
        const rowIntersects = rowBottom >= top && rowTop <= bottom;
        return current.anchorId != null
          ? rowIntersects
          : rowIntersects && segment.end >= from && segment.start <= to;
      })
      .map((segment) => segment._id);
    setSelectedIds(new Set(ids));
    onSelectionCreated?.({
      count: ids.length,
      method: "paint",
      durationMs: Math.max(0, performance.now() - current.startedAt),
    });
    if (ids.length) selectionAnchorRef.current = ids[0];
    markInteraction();
  }, [markInteraction, normalizedSegments, onSelectionCreated, pxPerSec, rowOfId, seekAt, toggleSelection, total]);

  const startDrag = useCallback((event, segment, mode) => {
    event.preventDefault();
    event.stopPropagation();
    if ((event.metaKey || event.ctrlKey) && !event.shiftKey) {
      toggleSelection(segment._id);
      return;
    }
    if (mode === "move" && event.shiftKey) {
      selectRangeTo(segment._id);
      return;
    }
    const movingGroup = mode === "move" && selectedIds.has(segment._id) && selectedIds.size > 1;
    const snapshots = (movingGroup ? normalizedSegments.filter((item) => selectedIds.has(item._id)) : [segment])
      .map((item) => ({ id: item._id, start: item.start, end: item.end }));
    if (!selectedIds.has(segment._id)) {
      selectionAnchorRef.current = segment._id;
      setSelectedIds(new Set([segment._id]));
    }
    dragRef.current = {
      id: segment._id,
      mode,
      originX: event.clientX,
      snapshots,
      origStart: segment.start,
      origEnd: segment.end,
      moved: false,
      pxPerSec,
      startedAt: performance.now(),
    };
    event.currentTarget.setPointerCapture?.(event.pointerId);
    markInteraction();
    setPreview(movingGroup ? { changes: snapshots, mode } : { id: segment._id, start: segment.start, end: segment.end, mode });
  }, [markInteraction, normalizedSegments, pxPerSec, selectRangeTo, selectedIds, toggleSelection]);

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
      const safeDelta = clampSelectionShiftDelta(
        normalizedSegments,
        new Set(drag.snapshots.map((item) => item.id)),
        delta,
        total,
        gapS,
      );
      setLimitFeedback(Math.abs(delta) > 1e-4 && Math.abs(safeDelta) < 1e-4
        ? "No hay espacio para mover esta selección."
        : "");
      setPreview({ changes: drag.snapshots.map((item) => ({ id: item.id, start: item.start + safeDelta, end: item.end + safeDelta })), mode: drag.mode });
      return;
    }
    let start = drag.origStart;
    let end = drag.origEnd;
    if (drag.mode === "start" || drag.mode === "end") {
      const bounded = clampResizeTiming(
        normalizedSegments,
        drag.id,
        drag.mode === "start" ? drag.origStart + delta : drag.origStart,
        drag.mode === "end" ? drag.origEnd + delta : drag.origEnd,
        total,
        gapS,
        MIN_DUR_S,
      );
      if (bounded) ({ start, end } = bounded);
      setLimitFeedback(bounded?.blocked ? "La línea no tiene espacio para cambiar su duración." : "");
    } else {
      const safeDelta = clampSelectionShiftDelta(
        normalizedSegments, new Set([drag.id]), delta, total, gapS,
      );
      start = drag.origStart + safeDelta;
      end = drag.origEnd + safeDelta;
      setLimitFeedback(Math.abs(delta) > 1e-4 && Math.abs(safeDelta) < 1e-4
        ? "La línea llegó al límite disponible."
        : "");
    }
    setPreview({ id: drag.id, start, end, mode: drag.mode });
  }, [gapS, normalizedSegments, onDragStart, total, updateMarquee]);

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
      const durationMs = Math.max(0, performance.now() - drag.startedAt);
      if (changes.length) onTimingChangeBatch?.(changes, { durationMs });
      if (changes.length) onGroupMoved?.({ count: changes.length, delta: changes[0].start - drag.snapshots[0].start });
      return;
    }
    if (Math.abs(current.start - segment.start) > 1e-3 || Math.abs(current.end - segment.end) > 1e-3) {
      onTimingChange?.(segment._id, current.start, current.end);
    }
  }, [finishMarquee, onFocus, onGroupMoved, onTimingChange, onTimingChangeBatch, preview, seekAt]);

  const cancelPointerInteraction = useCallback((event) => {
    dragRef.current = null;
    marqueeRef.current = null;
    setPreview(null);
    setMarquee(null);
    setLimitFeedback("Edición cancelada; se restauraron los tiempos anteriores.");
    event?.stopPropagation?.();
  }, []);

  const fitTimeline = useCallback(() => {
    setPxPerSec(fitZoom());
    setMoreActionsOpen(false);
  }, [fitZoom]);

  const nudgeSelection = useCallback((delta) => {
    const snapshots = normalizedSegments.filter((segment) => selectedIds.has(segment._id));
    if (!snapshots.length) return;
    const safeDelta = clampSelectionShiftDelta(normalizedSegments, selectedIds, delta, total, gapS);
    if (Math.abs(safeDelta) < 1e-6) {
      setLimitFeedback("No hay espacio para mover esta selección.");
      return;
    }
    onDragStart?.();
    onTimingChangeBatch?.(snapshots.map((segment) => ({
      id: segment._id,
      start: segment.start + safeDelta,
      end: segment.end + safeDelta,
    })));
    markInteraction();
  }, [gapS, markInteraction, normalizedSegments, onDragStart, onTimingChangeBatch, selectedIds, total]);

  const scrollToPlayhead = useCallback(() => {
    const sc = scrollRef.current;
    if (!sc || typeof sc.scrollTo !== "function" || followLockRef.current || Date.now() - lastInteractionRef.current < FOLLOW_SUPPRESS_MS) return;
    const x = LABEL_W + currentTime * pxPerSec;
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
    if (!isPlaying || !followEnabled || followSuppressed || activeId == null) return;
    const row = activeRowRefs.current.get(activeId);
    row?.scrollIntoView?.({ block: "center", inline: "nearest", behavior: "smooth" });
  }, [activeId, followEnabled, followSuppressed, isPlaying]);

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

  const clearSelection = useCallback(() => {
    selectionAnchorRef.current = null;
    setSelectedIds(new Set());
  }, []);

  const deleteSelection = useCallback(() => {
    if (!selectedIds.size || editingTextId != null) return;
    const deleted = onDeleteSelection?.([...selectedIds]);
    if (deleted !== false) clearSelection();
  }, [clearSelection, editingTextId, onDeleteSelection, selectedIds]);

  const handleKeyDown = (event) => {
    if (event.key === "Escape") clearSelection();
    const targetIsEditable = ["INPUT", "TEXTAREA", "SELECT", "BUTTON"].includes(event.target?.tagName)
      || event.target?.isContentEditable;
    if ((event.key === "Delete" || event.key === "Backspace")
      && selectedIds.size > 0
      && !targetIsEditable) {
      event.preventDefault();
      deleteSelection();
      return;
    }
    if ((event.key === "ArrowLeft" || event.key === "ArrowRight")
      && selectedIds.size > 0
      && !targetIsEditable) {
      event.preventDefault();
      const amount = event.shiftKey ? 0.5 : event.altKey ? 0.01 : 0.1;
      nudgeSelection(event.key === "ArrowLeft" ? -amount : amount);
    }
  };

  const tickStep = nearestTick(120 / pxPerSec);

  return (
    <div
      className="rounded-2xl bg-surface-2/40 ring-1 ring-white/[0.07] overflow-hidden animate-fade-in outline-none focus-visible:ring-2 focus-visible:ring-brand-light"
      tabIndex={0}
      onKeyDown={handleKeyDown}
      aria-label={t("timeline.workspace", "Studio de tiempos")}
    >
      <div className="relative flex items-center justify-between gap-4 px-4 sm:px-5 py-4 border-b border-white/[0.07] flex-wrap bg-gradient-to-r from-white/[0.025] to-brand/[0.035]">
        <div className="flex items-center gap-3 min-w-0">
          <div className="grid h-9 w-9 shrink-0 place-items-center rounded-xl bg-brand/15 text-brand-light ring-1 ring-brand/25" aria-hidden="true">
            <svg className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.8" viewBox="0 0 24 24">
              <path d="M4 7h7v4H4zM13 13h7v4h-7z" /><path d="M4 3v18" strokeLinecap="round" />
            </svg>
          </div>
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <p className="text-sm font-semibold text-white tracking-tight">{t("timeline.title", "Ajustar tiempos")}</p>
              <span className="text-[10px] text-ink-tertiary tabular-nums">{normalizedSegments.length} {t("timeline.lines_count", "líneas")}</span>
            </div>
            <p className="text-[11px] text-ink-tertiary truncate">{t("timeline.subtitle", "Click reproduce · arrastrá el fondo para seleccionar")}</p>
          </div>
          {saveStatus === "saving" && <span className="text-[10px] text-ink-tertiary animate-pulse">{t("timeline.saving", "Guardando…")}</span>}
          {saveStatus === "saved" && <span className="text-[10px] text-emerald-300">✓ {t("timeline.saved", "Guardado")}</span>}
          {saveStatus === "local" && <span className="text-[10px] text-amber-300">{t("timeline.local_changes", "Cambios locales")}</span>}
          {saveStatus === "offline" && <span className="text-[10px] text-red-300">{t("timeline.offline", "Sin conexión")}</span>}
          {saveStatus === "conflict" && <span className="text-[10px] text-amber-300">{t("timeline.conflict", "Conflicto detectado")}</span>}
          {saveStatus === "error" && <span className="text-[10px] text-red-300">{t("timeline.save_error", "Sin guardar")}</span>}
        </div>
        <div className="relative flex items-center gap-2" data-testid="timeline-primary-actions" data-selected-count={selectedIds.size}>
          <button type="button" onClick={fitTimeline} title={t("timeline.fit_hint", "Ver la canción completa")} className="h-9 px-3 rounded-xl text-[11px] font-medium text-white bg-white/[0.055] ring-1 ring-white/[0.1] hover:bg-white/[0.09] transition-colors">{t("timeline.fit", "Ver canción completa")}</button>
          <button
            type="button"
            aria-expanded={moreActionsOpen}
            aria-controls="timeline-more-actions"
            onClick={() => setMoreActionsOpen((value) => !value)}
            aria-label={t("editor.more_actions", "Más acciones")}
            className="grid h-9 w-9 place-items-center rounded-xl text-ink-secondary ring-1 ring-white/[0.1] hover:text-white hover:bg-white/[0.06] transition-colors"
          >
            <svg className="h-4 w-4" fill="currentColor" viewBox="0 0 24 24" aria-hidden="true"><circle cx="5" cy="12" r="1.7"/><circle cx="12" cy="12" r="1.7"/><circle cx="19" cy="12" r="1.7"/></svg>
          </button>
          {moreActionsOpen && (
            <>
            <button type="button" tabIndex={-1} aria-hidden="true" onClick={() => setMoreActionsOpen(false)} className="fixed inset-0 z-40 cursor-default" />
            <div id="timeline-more-actions" role="menu" className="absolute right-0 top-full z-50 mt-2 w-64 overflow-hidden rounded-2xl bg-surface-1 p-1.5 ring-1 ring-white/[0.1] shadow-2xl shadow-black/50">
              <button
                type="button"
                onClick={() => setFollowEnabled((value) => !value)}
                aria-pressed={followEnabled}
                title={t("timeline.follow_hint", "Mantener la línea activa visible")}
                role="menuitem"
                className={`w-full rounded-xl px-3 py-2.5 text-left text-[11px] transition-colors ${followEnabled && !followSuppressed ? "text-brand-light bg-brand/10" : "text-ink-secondary hover:text-white hover:bg-white/[0.05]"}`}
              >
                <span className="block font-medium">{followEnabled && followSuppressed ? t("timeline.resume", "Reanudar seguimiento") : t("timeline.follow", "Seguir reproducción")}</span>
                <span className="mt-0.5 block text-[10px] text-ink-tertiary">{t("timeline.follow_desc", "Mantiene el playhead visible")}</span>
              </button>
              <div className="my-1 flex items-center justify-between rounded-xl px-3 py-2 text-[11px] text-ink-secondary hover:bg-white/[0.04]">
                <span>{t("timeline.zoom", "Zoom")}</span>
                <div className="inline-flex items-center h-7 rounded-lg ring-1 ring-white/[0.1] overflow-hidden">
                  <button type="button" onClick={() => setPxPerSec((value) => Math.max(ZOOM_MIN, value - ZOOM_STEP))} className="w-7 h-full hover:text-white hover:bg-white/[0.06]" aria-label={t("timeline.zoom_out", "Alejar")}>−</button>
                  <span data-testid="timeline-zoom" className="w-16 text-center text-[9px] text-ink-tertiary tabular-nums">{Math.round(pxPerSec)} px/s</span>
                  <button type="button" onClick={() => setPxPerSec((value) => Math.min(ZOOM_MAX, value + ZOOM_STEP))} className="w-7 h-full hover:text-white hover:bg-white/[0.06]" aria-label={t("timeline.zoom_in", "Acercar")}>+</button>
                </div>
              </div>
              <button type="button" role="menuitem" onClick={() => { onReset?.(); setMoreActionsOpen(false); }} className="w-full rounded-xl px-3 py-2.5 text-left text-[11px] text-ink-secondary hover:text-white hover:bg-white/[0.05]">
                <span className="block font-medium">{t("timeline.reset", "Restaurar tiempos originales")}</span>
                <span className="mt-0.5 block text-[10px] text-ink-tertiary">{t("timeline.reset_desc", "Deshace todos los ajustes de timing")}</span>
              </button>
            </div>
            </>
          )}
        </div>
      </div>

      {selectedIds.size > 0 ? (
        <div data-testid="timeline-selection-help" aria-live="polite" className="flex items-center gap-2.5 border-b border-brand/20 bg-gradient-to-r from-brand/20 via-brand/10 to-transparent px-4 sm:px-5 py-2.5 flex-wrap">
          <span className="inline-flex h-7 items-center rounded-lg bg-brand px-2.5 text-[11px] font-semibold text-white shadow-lg shadow-brand/20">{selectedIds.size} {selectedIds.size === 1 ? "línea" : "líneas"}</span>
          <span className="text-[11px] text-ink-secondary">{t("timeline.move_selection", "Mover selección")}</span>
          <div className="inline-flex overflow-hidden rounded-lg ring-1 ring-white/[0.12]">
            <button type="button" onClick={() => nudgeSelection(-0.1)} className="h-7 px-2.5 text-[10px] text-ink-secondary hover:bg-white/[0.07] hover:text-white">{t("timeline.nudge_back", "−100 ms")}</button>
            <button type="button" onClick={() => nudgeSelection(0.1)} className="h-7 border-l border-white/[0.08] px-2.5 text-[10px] text-ink-secondary hover:bg-white/[0.07] hover:text-white">{t("timeline.nudge_forward", "+100 ms")}</button>
          </div>
          <span className="hidden sm:inline text-[10px] text-ink-tertiary">{t("timeline.move_selection_hint", "o arrastrá cualquier bloque seleccionado")}</span>
          <div className="ml-auto flex items-center gap-1.5">
            <button
              type="button"
              onClick={deleteSelection}
              aria-keyshortcuts="Delete Backspace"
              className="h-7 rounded-lg px-2.5 text-[10px] font-medium text-red-300 hover:bg-red-400/10 hover:text-red-200"
            >
              {t("timeline.delete_selection", "Eliminar")}
            </button>
            <button type="button" onClick={clearSelection} className="h-7 px-2.5 rounded-lg text-[10px] text-ink-secondary hover:text-white hover:bg-white/[0.06]" aria-label={t("timeline.clear_selection", "Limpiar selección")}>{t("timeline.clear", "Limpiar")}</button>
          </div>
        </div>
      ) : (
        <div data-testid="timeline-selection-help" className="flex items-center gap-2 border-b border-white/[0.06] bg-surface-1/25 px-4 sm:px-5 py-2 text-[10px] text-ink-tertiary">
          <span className="inline-block h-1.5 w-1.5 rounded-full bg-brand-light shadow-glow" aria-hidden="true" />
          <span>{t("timeline.paint_hint", "Arrastrá el fondo o la lista de letras para seleccionar varias líneas")}</span>
        </div>
      )}
      <p aria-live="polite" className="sr-only">{limitFeedback}</p>

      <div
        ref={scrollRef}
        data-testid="timeline-scroll"
        data-scroll-owner="horizontal-only"
        className="overflow-x-auto overflow-y-hidden overscroll-x-contain"
        style={{ scrollPaddingLeft: LABEL_W + 12 }}
        onWheel={markInteraction}
      >
        <div className="relative min-w-full" style={{ width: surfaceWidth }}>
          <div data-testid="timeline-ruler" className="relative h-9 border-b border-white/[0.06] bg-surface-1/90">
            <div className="sticky left-0 z-40 flex h-full items-center border-r border-white/[0.08] bg-surface-1/95 px-4 text-[9px] font-semibold uppercase tracking-[0.16em] text-ink-tertiary" style={{ width: LABEL_W }}>{t("timeline.lyrics_rail", "Letras")}</div>
            <div className="absolute inset-y-0 cursor-pointer" style={{ left: LABEL_W, width: trackWidth }} onClick={(event) => seekAt(event.clientX)}>
              {ticks.map((time) => (
                <div key={time} className="absolute top-0 bottom-0 border-l border-white/[0.08] pointer-events-none" style={{ left: time * pxPerSec }}>
                  <span className="absolute top-1 left-1 text-[9px] text-ink-tertiary tabular-nums whitespace-nowrap">{fmt(time)}</span>
                </div>
              ))}
            </div>
          </div>

          <div className="relative h-12 border-b border-white/[0.06] bg-brand/[0.035]">
            <div className="sticky left-0 z-40 flex h-full items-center border-r border-white/[0.08] bg-surface-1/95 px-4 text-[10px] text-ink-tertiary" style={{ width: LABEL_W }}>
              <span className="inline-flex items-center gap-2"><span className="h-1.5 w-1.5 rounded-full bg-brand-light/70" />{t("timeline.waveform", "Forma de onda")}</span>
            </div>
            <div className="absolute inset-y-0 cursor-pointer" style={{ left: LABEL_W, width: trackWidth }} onClick={(event) => seekAt(event.clientX)}>
              {waveform?.peaks?.length ? <canvas ref={canvasRef} className="absolute inset-0 pointer-events-none opacity-80" aria-hidden="true" /> : null}
            </div>
          </div>

          <div className="relative" style={{ width: surfaceWidth, height: trackHeight }}>
            <div className="sticky left-0 z-40 border-r border-white/[0.08] bg-surface-1/95 shadow-[12px_0_24px_rgba(0,0,0,.18)]" style={{ width: LABEL_W, height: trackHeight }}>
              {normalizedSegments.map((segment, index) => {
                const isSelected = selectedIds.has(segment._id);
                const isActive = activeId === segment._id;
                return (
                  <div
                    key={`label-${segment._id}`}
                    ref={(node) => {
                      if (node) activeRowRefs.current.set(segment._id, node);
                      else activeRowRefs.current.delete(segment._id);
                    }}
                    data-testid="timeline-label-row"
                    data-active={isActive ? "true" : "false"}
                    data-playing={isActive && isPlaying ? "true" : "false"}
                    data-selected={isSelected ? "true" : "false"}
                    aria-current={isActive ? "true" : undefined}
                    className={`absolute left-0 right-0 flex cursor-crosshair items-center gap-2 border-b px-3 transition-colors ${isActive ? "border-cyan-300/30 bg-cyan-300/[0.11]" : isSelected ? "border-brand/20 bg-brand/15" : "border-white/[0.045] hover:bg-white/[0.035]"}`}
                    style={{ top: index * ROW_H, height: ROW_H, touchAction: "none" }}
                    title={t("timeline.paint_rows_hint", "Arrastrá para seleccionar varias líneas")}
                    onPointerDown={(event) => beginMarquee(event, segment._id)}
                    onPointerMove={updateMarquee}
                    onPointerUp={finishMarquee}
                    onPointerCancel={cancelPointerInteraction}
                  >
                    {isActive && <span className="absolute inset-y-1.5 left-0 w-0.5 rounded-full bg-cyan-300 shadow-[0_0_10px_rgba(103,232,249,.8)]" aria-hidden="true" />}
                    <span className={`grid h-5 w-5 shrink-0 place-items-center rounded-md text-[9px] font-semibold tabular-nums ${isActive ? "bg-cyan-300 text-slate-950" : isSelected ? "bg-brand text-white" : "bg-white/[0.05] text-ink-tertiary"}`}>{isActive ? (isPlaying ? "▶" : "•") : index + 1}</span>
                    <span className={`min-w-0 flex-1 truncate text-[11px] ${isActive ? "font-medium text-white" : "text-ink-secondary"}`}>{segment.text || "Línea sin texto"}</span>
                    {isActive && <span className="hidden shrink-0 text-[8px] font-semibold uppercase tracking-[0.12em] text-cyan-200 xl:inline">{isPlaying ? t("timeline.playing", "Sonando") : t("timeline.current", "Actual")}</span>}
                    <span className="shrink-0 text-[9px] tabular-nums text-ink-tertiary">{fmt(segment.start)}</span>
                  </div>
                );
              })}
            </div>

            <div
              ref={trackRef}
              data-testid="timeline-lane"
              data-px-per-sec={pxPerSec}
              className="absolute top-0 cursor-crosshair bg-[linear-gradient(90deg,rgba(255,255,255,.018)_1px,transparent_1px)]"
              style={{ left: LABEL_W, width: trackWidth, height: trackHeight, backgroundSize: `${Math.max(24, tickStep * pxPerSec)}px 100%` }}
              onPointerDown={(event) => beginMarquee(event)}
              onPointerMove={updateMarquee}
              onPointerUp={finishMarquee}
              onPointerCancel={cancelPointerInteraction}
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
                  tabIndex={0}
                  role="button"
                  aria-pressed={isSelected}
                  aria-label={`Línea ${index + 1}: ${segment.text}. ${fmt(start)} a ${fmt(end)}`}
                  className={`absolute z-10 cursor-grab active:cursor-grabbing rounded-lg ring-1 transition-all select-none ${isSelected ? "bg-brand/30" : isActive ? "bg-cyan-400/25 shadow-[0_0_22px_rgba(34,211,238,.16)]" : "bg-surface-3/95"} ${isSelected ? "ring-2 ring-brand-light shadow-[0_0_0_3px_rgba(139,92,246,.13)]" : isActive ? "ring-cyan-300/60" : "ring-white/[0.18] hover:ring-white/[0.32]"} ${isFocused ? "outline outline-1 outline-white/80" : ""} ${isRecent ? "ring-accent" : ""}`}
                  style={{ left: start * pxPerSec, top: index * ROW_H + 7, width, height: ROW_H - 14, touchAction: "none", scrollMarginLeft: LABEL_W + 12 }}
                  onPointerDown={(event) => startDrag(event, segment, "move")}
                  onPointerMove={updateDrag}
                  onPointerUp={(event) => finishDrag(event, segment)}
                  onPointerCancel={cancelPointerInteraction}
                  onClick={(event) => event.stopPropagation()}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault();
                      toggleSelection(segment._id);
                    }
                  }}
                >
                  <div data-testid="timeline-edge-start" className="group/edge absolute top-0 bottom-0 z-20 cursor-ew-resize" style={{ width: EDGE_PX, left: -EDGE_PX / 2 }} title={t("timeline.drag_start", "Arrastrá: cuándo ENTRA la línea")} onPointerDown={(event) => startDrag(event, segment, "start")} onPointerMove={updateDrag} onPointerUp={(event) => finishDrag(event, segment)} onPointerCancel={cancelPointerInteraction}>
                    <span className="absolute inset-y-1 left-1/2 w-1 -translate-x-1/2 rounded-full bg-brand-light/55 transition-all group-hover/edge:inset-y-0.5 group-hover/edge:bg-white group-hover/edge:shadow-[0_0_8px_rgba(255,255,255,.7)]" />
                  </div>
                  <div data-testid="timeline-edge-end" className="group/edge absolute top-0 bottom-0 z-20 cursor-ew-resize" style={{ width: EDGE_PX, right: -EDGE_PX / 2 }} title={t("timeline.drag_end", "Arrastrá: cuándo SALE la línea")} onPointerDown={(event) => startDrag(event, segment, "end")} onPointerMove={updateDrag} onPointerUp={(event) => finishDrag(event, segment)} onPointerCancel={cancelPointerInteraction}>
                    <span className="absolute inset-y-1 left-1/2 w-1 -translate-x-1/2 rounded-full bg-brand-light/55 transition-all group-hover/edge:inset-y-0.5 group-hover/edge:bg-white group-hover/edge:shadow-[0_0_8px_rgba(255,255,255,.7)]" />
                  </div>
                  <div
                    data-testid="timeline-segment-body"
                    className="flex items-center gap-2 h-full px-3 pl-4 pointer-events-none cursor-grab active:cursor-grabbing"
                    title={t("timeline.move_range_hint", "Arrastrá para mover · Shift+click selecciona un rango")}
                  >
                    {width >= 54 && <span className="text-[9px] text-ink-tertiary tabular-nums shrink-0">{fmt(start)}</span>}
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
                    ) : width >= 32 ? (
                      <span className="min-w-0 truncate text-xs text-white/90 pointer-events-auto cursor-text" onDoubleClick={(event) => { event.stopPropagation(); startTextEdit(segment); }} title={`${segment.text}\n\n— Doble-click para corregir`}>{segment.text}</span>
                    ) : null}
                  </div>
                  {previewItem && (preview?.mode === "start" || preview?.mode === "end") && (
                    <span className="pointer-events-none absolute -top-7 right-0 z-40 rounded-md bg-black/90 px-2 py-1 text-[9px] font-medium text-white shadow-xl ring-1 ring-white/[0.12] tabular-nums whitespace-nowrap">
                      {fmt(start)} → {fmt(end)}
                    </span>
                  )}
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
                  <p className="mt-1 text-[10px] text-ink-tertiary">{t("timeline.empty_hint", "Volvé a Revisar letra o recargá la letra")}</p>
                </div>
              </div>
            )}

            {marquee && (
              <div className="absolute z-20 rounded-lg bg-brand/15 ring-1 ring-brand/70 pointer-events-none" style={{ left: marquee.anchorId != null ? 0 : Math.min(marquee.originX, marquee.currentX) - (trackRef.current?.getBoundingClientRect().left || 0), top: Math.min(marquee.originY, marquee.currentY) - (trackRef.current?.getBoundingClientRect().top || 0), width: marquee.anchorId != null ? trackWidth : Math.abs(marquee.currentX - marquee.originX), height: Math.abs(marquee.currentY - marquee.originY) }} aria-hidden="true" />
            )}

            <div data-testid="timeline-playhead" className="absolute top-0 bottom-0 z-30 w-px bg-cyan-200/15 pointer-events-none" style={{ left: currentTime * pxPerSec }}>
              <span className="absolute -top-1 -left-1.5 w-3 h-3 rounded-full bg-cyan-300 shadow-[0_0_10px_rgba(103,232,249,.8)]" />
            </div>
            {activeRowIndex != null && (
              <div data-testid="timeline-active-playhead" className="absolute z-30 w-0.5 bg-cyan-200 shadow-[0_0_10px_rgba(103,232,249,.75)] pointer-events-none" style={{ left: currentTime * pxPerSec, top: activeRowIndex * ROW_H + 4, height: ROW_H - 8 }} aria-hidden="true" />
            )}
            </div>
          </div>
        </div>
      </div>

      <div className="flex items-center gap-x-5 gap-y-1.5 px-4 sm:px-5 py-2.5 border-t border-white/[0.06] text-[10px] text-ink-tertiary flex-wrap">
        <span><strong className="text-ink-secondary">{t("timeline.click_label", "Click")}</strong> {t("timeline.click_help", "reproduce desde ese punto")}</span>
        <span><strong className="text-ink-secondary">{t("timeline.drag_label", "Arrastrar fondo")}</strong> {t("timeline.drag_help", "selecciona líneas")}</span>
        <span><strong className="text-ink-secondary">{t("timeline.modifier_label", "Cmd/Ctrl-click")}</strong> {t("timeline.modifier_help", "agrega o quita una línea")}</span>
      </div>
    </div>
  );
}
