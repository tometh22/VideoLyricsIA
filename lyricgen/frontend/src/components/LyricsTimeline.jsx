import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useI18n } from "../i18n";

const DEFAULT_ZOOM = 28;
const MIN_ZOOM = 10;
const MAX_ZOOM = 56;
const ZOOM_STEP = 8;
const MIN_BLOCK_HEIGHT = 30;
const MIN_DURATION = 0.3;

function formatTime(seconds) {
  const value = Math.max(0, Number(seconds) || 0);
  const minutes = Math.floor(value / 60);
  const secs = Math.floor(value % 60);
  return `${minutes}:${String(secs).padStart(2, "0")}`;
}

function clamp(value, min, max) {
  return Math.min(Math.max(value, min), max);
}

/**
 * Advanced timing view. It intentionally owns only interaction state;
 * LyricsEditor remains the source of truth for the segment data.
 */
export default function LyricsTimeline({
  segments,
  audioFile = null,
  duration = 0,
  currentTime = 0,
  isPlaying = false,
  syncMode = false,
  syncCursor = -1,
  onSeek,
  onFocus,
  onTimingChange,
  onTimingChangeBatch,
  onTextChange,
  onReset,
}) {
  const { t } = useI18n();
  const copy = (key, fallback) => t(key) || fallback;
  const [selectedIds, setSelectedIds] = useState(() => new Set());
  const [selectionMode, setSelectionMode] = useState(true);
  const [groupMoveMode, setGroupMoveMode] = useState(false);
  const [zoom, setZoom] = useState(DEFAULT_ZOOM);
  const [editingId, setEditingId] = useState(null);
  const [draftText, setDraftText] = useState("");
  const [preview, setPreview] = useState(null);
  const [selectionBox, setSelectionBox] = useState(null);
  const [waveform, setWaveform] = useState([]);
  const dragRef = useRef(null);
  const selectionRef = useRef(null);
  const selectionAnchorRef = useRef(null);
  const suppressClickRef = useRef(false);
  const laneRef = useRef(null);

  useEffect(() => {
    let cancelled = false;
    if (!audioFile || typeof window === "undefined") {
      setWaveform([]);
      return undefined;
    }
    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextClass) return undefined;

    const context = new AudioContextClass();
    audioFile.arrayBuffer()
      .then((bytes) => context.decodeAudioData(bytes))
      .then((buffer) => {
        if (cancelled) return;
        const channel = buffer.getChannelData(0);
        const count = 72;
        const bucketSize = Math.max(1, Math.floor(channel.length / count));
        const peaks = Array.from({ length: count }, (_, index) => {
          const from = index * bucketSize;
          const to = Math.min(channel.length, from + bucketSize);
          let peak = 0;
          for (let sample = from; sample < to; sample += 1) peak = Math.max(peak, Math.abs(channel[sample]));
          return peak;
        });
        const maxPeak = Math.max(...peaks, 0.01);
        setWaveform(peaks.map((peak) => peak / maxPeak));
      })
      .catch(() => {
        if (!cancelled) setWaveform([]);
      })
      .finally(() => context.close().catch(() => {}));

    return () => { cancelled = true; };
  }, [audioFile]);

  const total = Math.max(
    Number(duration) || 0,
    ...segments.map((segment) => Number(segment.end) || 0),
    1,
  );
  const laneHeight = Math.max(420, total * zoom);
  const selectedCount = selectedIds.size;

  const selectedSegments = useMemo(
    () => segments.filter((segment) => selectedIds.has(segment._id)),
    [segments, selectedIds],
  );

  const timeFromClientY = useCallback((clientY) => {
    const rect = laneRef.current?.getBoundingClientRect();
    if (!rect) return 0;
    return clamp((clientY - rect.top) / zoom, 0, total);
  }, [total, zoom]);

  const toggleSelected = useCallback((id) => {
    setSelectedIds((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const selectBetween = useCallback((fromId, toId, additive = false) => {
    const from = segments.findIndex((segment) => segment._id === fromId);
    const to = segments.findIndex((segment) => segment._id === toId);
    if (from < 0 || to < 0) return;
    const min = Math.min(from, to);
    const max = Math.max(from, to);
    setSelectedIds((current) => {
      const next = additive ? new Set(current) : new Set();
      for (let index = min; index <= max; index += 1) next.add(segments[index]._id);
      return next;
    });
  }, [segments]);

  const clearSelection = useCallback(() => {
    setSelectedIds(new Set());
    setGroupMoveMode(false);
  }, []);

  const beginTextEdit = (segment) => {
    setEditingId(segment._id);
    setDraftText(segment.text || "");
  };

  const commitTextEdit = (id) => {
    onTextChange?.(id, draftText);
    setEditingId(null);
  };

  const startDrag = useCallback((event, segment, mode) => {
    event.stopPropagation();

    const modifierSelection = event.metaKey || event.ctrlKey || event.shiftKey;
    if (modifierSelection) {
      if (event.shiftKey && selectionAnchorRef.current !== null) {
        selectBetween(selectionAnchorRef.current, segment._id, event.metaKey || event.ctrlKey);
      } else {
        toggleSelected(segment._id);
      }
      selectionAnchorRef.current = segment._id;
      return;
    }

    const movingGroup = mode === "move" && selectedIds.has(segment._id) && selectedCount > 1;
    if (selectionMode && mode === "move" && !movingGroup) {
      event.currentTarget.setPointerCapture?.(event.pointerId);
      selectionRef.current = {
        originY: event.clientY,
        moved: false,
        anchorId: segment._id,
        baseSelection: new Set(),
      };
      selectionAnchorRef.current = segment._id;
      setSelectedIds(new Set([segment._id]));
      setSelectionBox({ startY: event.clientY, endY: event.clientY });
      return;
    }
    const snapshot = (movingGroup ? selectedSegments : [segment]).map((item) => ({
      id: item._id,
      start: item.start,
      end: item.end,
    }));

    event.currentTarget.setPointerCapture?.(event.pointerId);
    dragRef.current = {
      id: segment._id,
      mode,
      originY: event.clientY,
      snapshot,
      moved: false,
      zoom,
    };
    setPreview({ changes: snapshot });
  }, [selectedCount, selectedIds, selectedSegments, selectBetween, selectionMode, toggleSelected, zoom]);

  const moveDrag = useCallback((event) => {
    const selection = selectionRef.current;
    if (selection) {
      if (Math.abs(event.clientY - selection.originY) > Math.max(3, zoom * 0.05)) selection.moved = true;
      setSelectionBox({ startY: selection.originY, endY: event.clientY });
      if (selection.moved) {
        const low = Math.min(selection.originY, event.clientY);
        const high = Math.max(selection.originY, event.clientY);
        const rect = laneRef.current?.getBoundingClientRect();
        if (rect) {
          const from = clamp((low - rect.top) / zoom, 0, total);
          const to = clamp((high - rect.top) / zoom, 0, total);
          const painted = segments.filter((segment) => segment.end >= from && segment.start <= to).map((segment) => segment._id);
          const next = selection.baseSelection ? new Set(selection.baseSelection) : new Set();
          painted.forEach((id) => next.add(id));
          setSelectedIds(next);
        }
      }
      return;
    }
    const drag = dragRef.current;
    if (!drag) return;

    const deltaPixels = event.clientY - drag.originY;
    const slop = Math.max(3, drag.zoom * 0.05);
    if (Math.abs(deltaPixels) > slop) drag.moved = true;

    const delta = deltaPixels / drag.zoom;
    if (drag.snapshot.length > 1) {
      const minStart = Math.min(...drag.snapshot.map((item) => item.start));
      const maxEnd = Math.max(...drag.snapshot.map((item) => item.end));
      const safeDelta = clamp(delta, -minStart, total - maxEnd);
      setPreview({
        changes: drag.snapshot.map((item) => ({
          id: item.id,
          start: item.start + safeDelta,
          end: item.end + safeDelta,
        })),
      });
      return;
    }

    const original = drag.snapshot[0];
    if (!original) return;
    let start = original.start;
    let end = original.end;
    if (drag.mode === "start") {
      start = clamp(original.start + delta, 0, original.end - MIN_DURATION);
    } else if (drag.mode === "end") {
      end = clamp(original.end + delta, original.start + MIN_DURATION, total);
    } else {
      const safeDelta = clamp(delta, -original.start, total - original.end);
      start = original.start + safeDelta;
      end = original.end + safeDelta;
    }
    setPreview({ changes: [{ id: original.id, start, end }] });
  }, [segments, total, zoom]);

  const finishDrag = useCallback((event, segment) => {
    const selection = selectionRef.current;
    if (selection) {
      selectionRef.current = null;
      setSelectionBox(null);
      // A click on an unselected block selects it; a drag paints the range.
      if (!selection.moved) setSelectedIds(new Set([segment._id]));
      else suppressClickRef.current = true;
      return;
    }
    const drag = dragRef.current;
    dragRef.current = null;
    const currentPreview = preview;
    setPreview(null);
    if (!drag || !currentPreview) return;

    if (!drag.moved) {
      onFocus?.(segment._id);
      onSeek?.(timeFromClientY(event.clientY));
      return;
    }

    const changes = currentPreview.changes.filter((change) => {
      const original = drag.snapshot.find((item) => item.id === change.id);
      return original && (
        Math.abs(original.start - change.start) > 0.001 ||
        Math.abs(original.end - change.end) > 0.001
      );
    });
    if (!changes.length) return;

    if (changes.length > 1) onTimingChangeBatch?.(changes);
    else {
      const change = changes[0];
      onTimingChange?.(change.id, change.start, change.end);
    }
    setGroupMoveMode(false);
  }, [onFocus, onSeek, onTimingChange, onTimingChangeBatch, preview, timeFromClientY]);

  const seekFromLane = (event) => {
    if (suppressClickRef.current) {
      suppressClickRef.current = false;
      return;
    }
    if (selectionRef.current || dragRef.current || event.target.closest?.("[data-timeline-block]")) return;
    onSeek?.(timeFromClientY(event.clientY));
  };

  const startLaneSelection = (event) => {
    if (!selectionMode) return;
    if (event.target.closest?.("[data-timeline-block]")) return;
    selectionRef.current = {
      originY: event.clientY,
      moved: false,
      baseSelection: event.metaKey || event.ctrlKey ? new Set(selectedIds) : new Set(),
    };
    setSelectionBox({ startY: event.clientY, endY: event.clientY });
    event.currentTarget.setPointerCapture?.(event.pointerId);
  };

  const previewFor = (segment) => {
    const change = preview?.changes?.find((item) => item.id === segment._id);
    return change || segment;
  };

  return (
    <section className="rounded-card bg-surface-2/40 ring-1 ring-white/[0.05] overflow-hidden" aria-label={copy("editor.advanced_view", "Ajustar tiempos")}>
      <header className="px-3 py-3 border-b border-white/[0.06] space-y-3">
        <div className="flex items-center justify-between gap-3 flex-wrap">
          <div>
            <h3 className="text-xs uppercase tracking-wider text-white font-semibold">{copy("editor.timeline_title", "Ajustar tiempos")}</h3>
            <p className="text-[11px] text-ink-secondary mt-0.5">{copy("editor.timeline_hint", "Arrastrá una línea para moverla en el audio.")}</p>
          </div>
          <div className="flex items-center gap-2 flex-wrap" data-testid="timeline-primary-actions" data-selected-count={selectedCount}>
            <button
              type="button"
              aria-pressed={selectionMode}
              onClick={() => { setSelectionMode((value) => !value); setGroupMoveMode(false); }}
              className={`h-8 px-3 rounded-lg text-[11px] ring-1 transition-colors ${selectionMode ? "bg-brand/20 text-brand-light ring-brand/40" : "text-gray-300 ring-white/[0.10] hover:text-white hover:bg-white/[0.05]"}`}
            >
              {selectionMode ? copy("editor.selecting_lines", "Pintar selección") : copy("editor.select_lines", "Seleccionar líneas")}
            </button>
            <div className="inline-flex items-center rounded-lg ring-1 ring-white/[0.10] overflow-hidden">
              <button type="button" onClick={() => setZoom((value) => Math.max(MIN_ZOOM, value - ZOOM_STEP))} aria-label="Alejar" className="w-8 h-8 text-gray-300 hover:text-white hover:bg-white/[0.06]">−</button>
              <span className="px-2 text-[10px] text-gray-500 tabular-nums">{Math.round(zoom)} px/s</span>
              <button type="button" onClick={() => setZoom((value) => Math.min(MAX_ZOOM, value + ZOOM_STEP))} aria-label="Acercar" className="w-8 h-8 text-gray-300 hover:text-white hover:bg-white/[0.06]">+</button>
            </div>
            <button type="button" onClick={onReset} className="h-8 px-3 rounded-lg text-[11px] text-gray-300 ring-1 ring-white/[0.10] hover:text-white hover:bg-white/[0.05]">{copy("editor.restore_timings", "Restaurar tiempos")}</button>
          </div>
        </div>

        {selectedCount > 0 && (
          <div className="flex items-center gap-2 flex-wrap rounded-lg bg-brand/[0.08] ring-1 ring-brand/25 px-3 py-2" aria-live="polite">
            <strong className="text-xs text-brand-light">{selectedCount} {selectedCount === 1 ? copy("editor.selected_line", "línea seleccionada") : copy("editor.selected_lines", "líneas seleccionadas")}</strong>
            <span className="text-[11px] text-gray-400">{copy("editor.selection_hint", "Elegí qué hacer con esta selección.")}</span>
            {selectedCount > 1 && (
              <button
                type="button"
                onClick={() => { setSelectionMode(false); setGroupMoveMode(true); }}
                className={`h-7 px-2.5 rounded-md text-[11px] font-medium ${groupMoveMode ? "bg-brand text-white" : "bg-brand/20 text-brand-light hover:bg-brand/30"}`}
              >
                {groupMoveMode ? copy("editor.moving_selection", "Moviendo selección…") : copy("editor.move_selection", "Mover selección")}
              </button>
            )}
            <button type="button" onClick={clearSelection} className="h-7 px-2.5 rounded-md text-[11px] text-gray-300 hover:text-white hover:bg-white/[0.06]">{copy("editor.clear_selection", "Limpiar")}</button>
          </div>
        )}
      </header>

      <div className="relative">
        <div className="absolute inset-y-0 left-0 w-12 border-r border-white/[0.06] bg-surface-1/30 pointer-events-none">
          <div className="absolute inset-0 opacity-60" style={{ backgroundImage: "repeating-linear-gradient(to bottom, transparent 0, transparent 7px, rgba(139,124,246,.24) 8px, transparent 9px)" }} />
        </div>
        <div
          ref={laneRef}
          className={`relative overflow-y-auto max-h-[58vh] ${selectionMode ? "cursor-crosshair" : "cursor-pointer"}`}
          style={{ height: Math.min(laneHeight, 620) }}
          onPointerDown={startLaneSelection}
          onPointerMove={moveDrag}
          onPointerUp={(event) => {
            if (dragRef.current || selectionRef.current) {
              const id = dragRef.current?.id || selectionRef.current?.anchorId;
              const segment = segments.find((item) => item._id === id);
              if (segment) finishDrag(event, segment);
              else if (selectionRef.current) {
                const selection = selectionRef.current;
                selectionRef.current = null;
                setSelectionBox(null);
                if (selection.moved) suppressClickRef.current = true;
              }
            }
          }}
          onClick={seekFromLane}
          data-testid="lyrics-timeline-lane"
        >
          {selectionBox && (() => {
            const rect = laneRef.current?.getBoundingClientRect();
            if (!rect) return null;
            const top = clamp((Math.min(selectionBox.startY, selectionBox.endY) - rect.top) / zoom, 0, total) * zoom;
            const height = Math.max(2, Math.abs(selectionBox.endY - selectionBox.startY));
            return <div className="absolute left-12 right-4 border border-brand-light/70 bg-brand/10 rounded pointer-events-none z-30" style={{ top, height }} aria-hidden="true" />;
          })()}
          <div className="absolute left-12 right-4 top-0" style={{ height: laneHeight }}>
            <div className="absolute left-[-48px] top-0 w-10 pointer-events-none opacity-80" style={{ height: laneHeight }} aria-hidden="true">
              {waveform.length > 0 ? waveform.map((peak, index) => (
                <span
                  key={index}
                  className="absolute left-1/2 -translate-x-1/2 rounded-full bg-brand/50"
                  style={{ top: `${(index / waveform.length) * 100}%`, height: `${Math.max(2, laneHeight / waveform.length)}px`, width: `${Math.max(2, peak * 28)}px` }}
                />
              )) : (
                <div className="absolute inset-0 opacity-60" style={{ backgroundImage: "repeating-linear-gradient(to bottom, transparent 0, transparent 7px, rgba(139,124,246,.28) 8px, transparent 9px)" }} />
              )}
            </div>
            {Array.from({ length: Math.ceil(total / 5) + 1 }, (_, index) => index * 5).map((second) => (
              <div key={second} className="absolute left-0 right-0 border-t border-white/[0.06]" style={{ top: second * zoom }}>
                <span className="absolute -left-10 -top-2 text-[9px] text-gray-500 tabular-nums">{formatTime(second)}</span>
              </div>
            ))}

            <div className="absolute left-0 right-0 h-0.5 bg-brand z-20 pointer-events-none" style={{ top: currentTime * zoom }}>
              <span className="absolute -left-1 -top-1 w-2 h-2 rounded-full bg-brand-light" />
            </div>

            {segments.map((segment, index) => {
              const value = previewFor(segment);
              const isSelected = selectedIds.has(segment._id);
              const isActive = segment.start <= currentTime && currentTime < segment.end;
              const isArmed = syncMode && index === syncCursor;
              const height = Math.max(MIN_BLOCK_HEIGHT, (value.end - value.start) * zoom);
              return (
                <div
                  key={segment._id}
                  title={`${formatTime(value.start)} → ${formatTime(value.end)}`}
                  data-timeline-block="true"
                  className={`absolute left-2 right-2 rounded-lg ring-1 overflow-hidden transition-colors ${isArmed ? "bg-brand/35 ring-2 ring-brand-light shadow-glow" : isSelected ? "bg-brand/25 ring-brand-light" : isActive ? "bg-brand/20 ring-brand/60" : "bg-surface-3/80 ring-white/[0.10]"}`}
                  style={{ top: value.start * zoom, height }}
                  onPointerDown={(event) => startDrag(event, segment, "move")}
                >
                  <div className="absolute left-0 right-0 top-0 h-2 bg-brand/30 hover:bg-brand/80 cursor-ns-resize" onPointerDown={(event) => startDrag(event, segment, "start")} title="Ajustar inicio" />
                  <div className="absolute left-0 right-0 bottom-0 h-2 bg-brand/30 hover:bg-brand/80 cursor-ns-resize" onPointerDown={(event) => startDrag(event, segment, "end")} title="Ajustar final" />
                  <div className="px-2.5 pt-3 pb-2 flex items-start gap-2 min-w-0" onDoubleClick={(event) => { event.stopPropagation(); beginTextEdit(segment); }}>
                    <span className="text-[9px] text-gray-400 tabular-nums shrink-0">{formatTime(value.start)}</span>
                    {editingId === segment._id ? (
                      <input
                        autoFocus
                        value={draftText}
                        onChange={(event) => setDraftText(event.target.value)}
                        onPointerDown={(event) => event.stopPropagation()}
                        onKeyDown={(event) => {
                          if (event.key === "Enter") commitTextEdit(segment._id);
                          if (event.key === "Escape") setEditingId(null);
                        }}
                        onBlur={() => commitTextEdit(segment._id)}
                        className="min-w-0 flex-1 bg-surface-1 border border-brand/50 rounded px-1 text-xs text-white outline-none"
                      />
                    ) : (
                      <span className="text-xs text-white/90 line-clamp-3 break-words">{segment.text || "(sin texto)"}</span>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      </div>

      <footer className="px-3 py-2 border-t border-white/[0.06] flex flex-wrap gap-x-4 gap-y-1 text-[10px] text-gray-500">
        <span><strong className="text-gray-300">{copy("editor.timeline_click", "Click")}</strong> {copy("editor.timeline_click_hint", "reproduce desde ese punto")}</span>
        <span><strong className="text-gray-300">{copy("editor.timeline_double_click", "Doble click")}</strong> {copy("editor.timeline_double_click_hint", "corrige el texto")}</span>
        <span><strong className="text-gray-300">{copy("editor.timeline_edges", "Bordes")}</strong> {copy("editor.timeline_edges_hint", "ajustan entrada y salida")}</span>
        {isPlaying && <span className="text-brand-light">{copy("editor.timeline_playing", "Reproduciendo")}</span>}
      </footer>
    </section>
  );
}
