import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useI18n } from "../i18n";

const CONTEXT_S = 1.5;
const GAP_S = 0.05;
const NUDGE_S = 0.1;

function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value));
}

function formatTime(value, precise = false) {
  const seconds = Math.max(0, Number(value) || 0);
  const minutes = Math.floor(seconds / 60);
  const rest = precise ? (seconds % 60).toFixed(1).padStart(4, "0") : Math.floor(seconds % 60).toString().padStart(2, "0");
  return `${minutes}:${rest}`;
}

function reasonLabel(window, t) {
  const codes = window?.reasons || [];
  if (codes.some((code) => /structur|cardinal|event_count|motif/.test(String(code)))) {
    return t("editor.quality_reason_structure") || "Puede faltar o sobrar una frase";
  }
  if (codes.some((code) => /timing|align|boundary|overlap|inversion|start|end/.test(String(code)))) {
    return t("editor.quality_reason_timing") || "El inicio o final puede estar corrido";
  }
  if (codes.some((code) => /voic|coverage|uncovered|vocal/.test(String(code)))) {
    return t("editor.quality_reason_voice") || "Detectamos voz sin una frase asociada";
  }
  if (codes.some((code) => /text|lexical|asr|content/.test(String(code)))) {
    return t("editor.quality_reason_text") || "La letra puede no coincidir con el audio";
  }
  return t("editor.quality_reason_uncertain") || "Conviene escuchar este tramo";
}

function segmentShiftBounds(segments, segment, duration) {
  const ordered = [...segments].sort((a, b) => a.start - b.start || a.end - b.end);
  const index = ordered.findIndex((candidate) => candidate._id === segment._id);
  const previous = index > 0 ? ordered[index - 1] : null;
  const next = index >= 0 && index < ordered.length - 1 ? ordered[index + 1] : null;
  const min = (previous ? previous.end + GAP_S : 0) - segment.start;
  const max = (next ? next.start - GAP_S : Math.max(duration || segment.end, segment.end)) - segment.end;
  // A guided nudge must never reverse the operator's requested direction.
  // Existing destructive overlaps need the full timeline because there may
  // be no safe single-line position without also moving a neighbour.
  if (index < 0 || min > 0 || max < 0 || min > max) return { min: 0, max: 0, blocked: true };
  return { min, max, blocked: false };
}

function FocusedWaveform({
  waveform,
  waveformLoading,
  duration,
  rangeStart,
  rangeEnd,
  segments,
  constraintSegments,
  selectedId,
  currentTime,
  onSeek,
  onSelect,
  onMove,
}) {
  const { t } = useI18n();
  const canvasRef = useRef(null);
  const surfaceRef = useRef(null);
  const dragRef = useRef(null);
  const [width, setWidth] = useState(720);
  const [dragPreview, setDragPreview] = useState(null);
  const span = Math.max(0.5, rangeEnd - rangeStart);

  useEffect(() => {
    const node = surfaceRef.current;
    if (!node) return undefined;
    const measure = () => setWidth(Math.max(1, node.clientWidth || 720));
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
    const canvas = canvasRef.current;
    const peaks = waveform?.peaks;
    if (!canvas || !peaks?.length) return;
    const context = canvas.getContext?.("2d");
    if (!context) return;
    const height = 76;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
    context.setTransform(dpr, 0, 0, dpr, 0, 0);
    context.clearRect(0, 0, width, height);
    const audioDuration = Math.max(Number(waveform?.duration) || Number(duration) || rangeEnd, rangeEnd);
    const first = clamp(Math.floor((rangeStart / audioDuration) * peaks.length), 0, peaks.length - 1);
    const last = clamp(Math.ceil((rangeEnd / audioDuration) * peaks.length), first + 1, peaks.length);
    const visibleCount = Math.max(1, last - first);
    const bars = Math.max(1, Math.floor(width / 4));
    const stride = Math.max(1, Math.ceil(visibleCount / bars));
    context.fillStyle = "rgba(167, 139, 250, 0.82)";
    for (let index = first; index < last; index += stride) {
      const x = ((index - first) / visibleCount) * width;
      const magnitude = clamp(Number(peaks[index]) || 0, 0, 1);
      const barHeight = Math.max(2, magnitude * (height - 10));
      context.fillRect(x, (height - barHeight) / 2, Math.max(1.5, Math.min(3, (width / visibleCount) * stride - 1)), barHeight);
    }
  }, [duration, rangeEnd, rangeStart, waveform, width]);

  const positionPct = useCallback((time) => clamp(((time - rangeStart) / span) * 100, 0, 100), [rangeStart, span]);
  const commitShift = useCallback((segment, requestedDelta, operation) => {
    const bounds = segmentShiftBounds(constraintSegments, segment, duration);
    const delta = clamp(requestedDelta, bounds.min, bounds.max);
    if (Math.abs(delta) < 0.0001) return;
    onMove?.(segment._id, segment.start + delta, segment.end + delta, { operation });
  }, [constraintSegments, duration, onMove]);

  const beginDrag = (event, segment) => {
    if (event.button != null && event.button !== 0) return;
    const bounds = segmentShiftBounds(constraintSegments, segment, duration);
    if (bounds.blocked) return;
    event.preventDefault();
    event.stopPropagation();
    event.currentTarget.setPointerCapture?.(event.pointerId);
    dragRef.current = {
      pointerId: event.pointerId,
      clientX: event.clientX,
      segment,
      baseStart: segment.start,
      baseEnd: segment.end,
    };
    setDragPreview({ id: segment._id, delta: 0 });
    onSelect?.(segment._id);
  };
  const updateDrag = (event) => {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    const rawDelta = ((event.clientX - drag.clientX) / Math.max(1, width)) * span;
    const bounds = segmentShiftBounds(constraintSegments, drag.segment, duration);
    setDragPreview({ id: drag.segment._id, delta: clamp(rawDelta, bounds.min, bounds.max) });
  };
  const finishDrag = (event) => {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    const delta = dragPreview?.id === drag.segment._id ? dragPreview.delta : 0;
    dragRef.current = null;
    setDragPreview(null);
    event.currentTarget.releasePointerCapture?.(event.pointerId);
    const latest = constraintSegments.find((segment) => segment._id === drag.segment._id);
    if (!latest || latest.start !== drag.baseStart || latest.end !== drag.baseEnd) return;
    commitShift(drag.segment, delta, "guided_drag");
  };
  const cancelDrag = (event) => {
    const drag = dragRef.current;
    if (!drag || (event?.pointerId != null && drag.pointerId !== event.pointerId)) return;
    dragRef.current = null;
    setDragPreview(null);
    if (event?.currentTarget?.hasPointerCapture?.(drag.pointerId)) {
      event.currentTarget.releasePointerCapture?.(drag.pointerId);
    }
  };

  return (
    <div className="overflow-hidden rounded-2xl bg-black/25 ring-1 ring-white/[0.09]" data-testid="guided-waveform">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-white/[0.07] px-4 py-2.5">
        <div>
          <p className="text-[11px] font-semibold text-white">{t("timing_review.audio_title") || "Audio de la canción"}</p>
          <p className="mt-0.5 text-[10px] text-ink-tertiary">{t("timing_review.audio_hint") || "Los picos muestran dónde hay voz o sonido"}</p>
        </div>
        <span className="font-mono text-[10px] tabular-nums text-ink-tertiary">{formatTime(rangeStart, true)}–{formatTime(rangeEnd, true)}</span>
      </div>
      <div
        ref={surfaceRef}
        role="group"
        tabIndex={0}
        aria-label={t("timing_review.seek_audio") || "Buscar dentro de este tramo de audio"}
        className="relative h-[118px] cursor-pointer overflow-hidden bg-[linear-gradient(90deg,rgba(255,255,255,.035)_1px,transparent_1px)]"
        style={{ backgroundSize: "48px 100%" }}
        onClick={(event) => {
          const rect = event.currentTarget.getBoundingClientRect();
          onSeek?.(rangeStart + clamp((event.clientX - rect.left) / Math.max(1, rect.width), 0, 1) * span);
        }}
        onKeyDown={(event) => {
          if (event.target !== event.currentTarget) return;
          let next = null;
          if (event.key === "ArrowLeft") next = currentTime - NUDGE_S;
          if (event.key === "ArrowRight") next = currentTime + NUDGE_S;
          if (event.key === "Home") next = rangeStart;
          if (event.key === "End") next = rangeEnd;
          if (next == null) return;
          event.preventDefault();
          onSeek?.(clamp(next, rangeStart, rangeEnd));
        }}
      >
        {waveform?.peaks?.length ? (
          <canvas ref={canvasRef} className="absolute inset-x-0 top-1 pointer-events-none" aria-hidden="true" />
        ) : waveformLoading ? (
          <div className="absolute inset-x-4 top-4 h-14 overflow-hidden rounded-lg bg-white/[0.025]" role="status">
            <div className="flex h-full items-center justify-center gap-1.5 opacity-60" aria-hidden="true">
              {[22, 38, 55, 31, 64, 44, 26, 52, 35, 58, 29, 45].map((height, index) => (
                <span key={index} className="w-1 animate-pulse rounded-full bg-brand-light/50" style={{ height: `${height}%`, animationDelay: `${index * 70}ms` }} />
              ))}
            </div>
            <span className="sr-only">{t("timing_review.waveform_loading") || "Preparando guía de audio"}</span>
          </div>
        ) : (
          <div className="absolute inset-x-4 top-4 flex h-14 items-center justify-center rounded-lg border border-dashed border-white/[0.09] bg-white/[0.02] px-4 text-center text-[10px] text-ink-tertiary" role="status">
            {t("timing_review.waveform_unavailable") || "La guía visual no está disponible. Podés escuchar y ajustar normalmente."}
          </div>
        )}

        <div className="absolute inset-x-0 bottom-0 h-10 border-t border-white/[0.07] bg-surface-1/55">
          {segments.map((segment) => {
            const previewDelta = dragPreview?.id === segment._id ? dragPreview.delta : 0;
            const start = segment.start + previewDelta;
            const end = segment.end + previewDelta;
            const left = positionPct(start);
            const blockWidth = Math.max(2.5, positionPct(end) - left);
            const selected = segment._id === selectedId;
            return (
              <button
                key={segment._id}
                type="button"
                data-testid={`guided-segment-${segment._id}`}
                aria-label={`${t("timing_review.move_phrase") || "Mover frase"}: ${segment.text || t("timing_review.empty_phrase") || "Sin texto"}`}
                aria-pressed={selected}
                title={t("timing_review.drag_hint") || "Arrastrá para mover esta frase"}
                className={`absolute top-1.5 h-7 min-w-[28px] touch-none truncate rounded-lg border px-2 text-left text-[10px] font-medium shadow-lg transition-colors ${selected ? "z-20 border-brand-light/80 bg-brand text-white shadow-brand/25" : "z-10 border-white/15 bg-surface-2 text-ink-secondary hover:border-white/30 hover:text-white"}`}
                style={{ left: `${left}%`, width: `${blockWidth}%` }}
                onClick={(event) => { event.stopPropagation(); onSelect?.(segment._id); }}
                onPointerDown={(event) => beginDrag(event, segment)}
                onPointerMove={updateDrag}
                onPointerUp={finishDrag}
                onPointerCancel={cancelDrag}
                onLostPointerCapture={cancelDrag}
              >
                {segment.text || t("timing_review.empty_phrase") || "Sin texto"}
              </button>
            );
          })}
        </div>
        {currentTime >= rangeStart && currentTime <= rangeEnd && (
          <span className="pointer-events-none absolute inset-y-0 z-30 w-px bg-cyan-300 shadow-[0_0_8px_rgba(103,232,249,.9)]" style={{ left: `${positionPct(currentTime)}%` }} aria-hidden="true" />
        )}
      </div>
    </div>
  );
}

export default function GuidedTimingReview({
  windows = [],
  segments = [],
  waveform = null,
  waveformLoading = false,
  duration = 0,
  audioAvailable = true,
  audioLoading = false,
  currentTime = 0,
  isPlaying = false,
  playingWindowId = null,
  confirmedIds = new Set(),
  reviewRequired = false,
  onConfirm,
  onPlayWindow,
  onStopPlayback,
  onRetryAudio,
  onSeek,
  onMove,
  onOpenAdvanced,
}) {
  const { t } = useI18n();
  const [activeWindowId, setActiveWindowId] = useState(null);
  const [selectedSegmentId, setSelectedSegmentId] = useState(null);
  const activeWindowSignatureRef = useRef(null);
  const pending = useMemo(() => windows.filter((window) => !confirmedIds.has(window.id)), [confirmedIds, windows]);

  useEffect(() => {
    if (!windows.length) {
      setActiveWindowId(null);
      return;
    }
    if (!activeWindowId || !windows.some((window) => window.id === activeWindowId)) {
      setActiveWindowId((pending[0] || windows[0]).id);
    }
  }, [activeWindowId, pending, windows]);

  const activeWindow = windows.find((window) => window.id === activeWindowId) || pending[0] || windows[0] || null;
  const activeWindowSignature = activeWindow
    ? `${activeWindow.id}:${activeWindow.start}:${activeWindow.end}`
    : null;
  useEffect(() => {
    const previous = activeWindowSignatureRef.current;
    if (previous && previous !== activeWindowSignature) onStopPlayback?.();
    activeWindowSignatureRef.current = activeWindowSignature;
  }, [activeWindowSignature, onStopPlayback]);
  const overlappingSegments = useMemo(() => {
    if (!activeWindow) return [];
    return segments.filter((segment) => Number(segment.end) >= activeWindow.start && Number(segment.start) <= activeWindow.end);
  }, [activeWindow, segments]);

  useEffect(() => {
    if (!overlappingSegments.length) {
      setSelectedSegmentId(null);
      return;
    }
    if (!overlappingSegments.some((segment) => segment._id === selectedSegmentId)) {
      setSelectedSegmentId(overlappingSegments[0]._id);
    }
  }, [overlappingSegments, selectedSegmentId]);

  const selectedSegment = overlappingSegments.find((segment) => segment._id === selectedSegmentId) || overlappingSegments[0] || null;
  const waveformDuration = Number(waveform?.duration);
  const effectiveDuration = Number(duration) > 0
    ? Number(duration)
    : (Number.isFinite(waveformDuration) && waveformDuration > 0 ? waveformDuration : 0);
  const activeWindowPlayable = Boolean(activeWindow) && (
    effectiveDuration <= 0
    || (activeWindow.start < effectiveDuration && activeWindow.end > 0)
  );
  const rangeCeiling = effectiveDuration > 0
    ? effectiveDuration
    : Math.max(activeWindow?.end + CONTEXT_S || 1, 1);
  const desiredRangeStart = activeWindow ? Math.max(0, activeWindow.start - CONTEXT_S) : 0;
  const rangeStart = Math.min(desiredRangeStart, Math.max(0, rangeCeiling - Math.min(0.5, rangeCeiling)));
  const desiredRangeEnd = activeWindow ? activeWindow.end + CONTEXT_S : rangeCeiling;
  const rangeEnd = Math.max(rangeStart + Math.min(0.5, rangeCeiling - rangeStart), Math.min(rangeCeiling, desiredRangeEnd));
  const activeIndex = activeWindow ? windows.findIndex((window) => window.id === activeWindow.id) : -1;

  const nextWindow = useCallback((current, skipConfirmed = true) => {
    if (!windows.length) return null;
    const startIndex = Math.max(0, windows.findIndex((window) => window.id === current?.id));
    for (let offset = 1; offset <= windows.length; offset += 1) {
      const candidate = windows[(startIndex + offset) % windows.length];
      if (!skipConfirmed || !confirmedIds.has(candidate.id)) return candidate;
    }
    return null;
  }, [confirmedIds, windows]);

  const confirmAndContinue = () => {
    if (!activeWindow || !audioAvailable || !activeWindowPlayable) return;
    const next = nextWindow(activeWindow);
    onStopPlayback?.();
    onConfirm?.(activeWindow);
    if (next && next.id !== activeWindow.id) setActiveWindowId(next.id);
  };

  const nudge = (delta) => {
    if (!selectedSegment) return;
    const bounds = segmentShiftBounds(segments, selectedSegment, effectiveDuration);
    if (bounds.blocked) return;
    const safeDelta = clamp(delta, bounds.min, bounds.max);
    if (Math.abs(safeDelta) < 0.0001) return;
    onMove?.(
      selectedSegment._id,
      selectedSegment.start + safeDelta,
      selectedSegment.end + safeDelta,
      { operation: "guided_nudge" },
    );
  };
  const selectedMovementBounds = selectedSegment
    ? segmentShiftBounds(segments, selectedSegment, effectiveDuration)
    : { min: 0, max: 0, blocked: false };
  const selectedMovementBlocked = selectedMovementBounds.blocked;
  const openAdvanced = () => {
    onStopPlayback?.();
    onOpenAdvanced?.();
  };

  if (!windows.length) {
    return (
      <section className="rounded-2xl bg-emerald-400/[0.045] px-5 py-8 text-center ring-1 ring-emerald-300/15" data-testid="guided-timing-empty">
        <span className="mx-auto grid h-11 w-11 place-items-center rounded-2xl bg-emerald-400/10 text-emerald-200" aria-hidden="true">
          <svg className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24"><path d="m5 12 4 4L19 6" strokeLinecap="round" strokeLinejoin="round" /></svg>
        </span>
        <h3 className="mt-4 text-sm font-semibold text-white">{t("timing_review.no_issues_title") || "No encontramos partes dudosas"}</h3>
        <p className="mx-auto mt-1 max-w-lg text-xs leading-relaxed text-ink-secondary">{t("timing_review.no_issues_summary") || "La sincronización parece consistente. Si querés hacer un ajuste manual, abrí la timeline avanzada."}</p>
        <button type="button" onClick={openAdvanced} className="mt-4 rounded-xl bg-white/[0.06] px-4 py-2 text-xs font-semibold text-white ring-1 ring-white/[0.1] hover:bg-white/[0.1] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-light">
          {t("timing_review.open_advanced") || "Abrir timeline avanzada"}
        </button>
      </section>
    );
  }

  if (pending.length === 0) {
    return (
      <section className="rounded-2xl bg-emerald-400/[0.055] px-5 py-8 text-center ring-1 ring-emerald-300/20" data-testid="guided-timing-complete" role="status">
        <span className="mx-auto grid h-11 w-11 place-items-center rounded-full bg-emerald-400/15 text-emerald-200" aria-hidden="true">
          <svg className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="2.2" viewBox="0 0 24 24"><path d="m5 12 4 4L19 6" strokeLinecap="round" strokeLinejoin="round" /></svg>
        </span>
        <h3 className="mt-4 text-sm font-semibold text-white">{t("timing_review.complete_title") || "Sincronización revisada"}</h3>
        <p className="mx-auto mt-1 max-w-lg text-xs text-ink-secondary">{t("timing_review.complete_summary") || "Confirmaste todos los tramos señalados. Podés continuar o abrir la timeline para ajustes adicionales."}</p>
        <button type="button" onClick={openAdvanced} className="mt-4 text-xs font-semibold text-brand-light hover:text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-light">
          {t("timing_review.open_advanced") || "Abrir timeline avanzada"}
        </button>
      </section>
    );
  }

  return (
    <section className="space-y-4" data-testid="guided-timing-review" aria-labelledby="guided-timing-title">
      <div className="flex flex-wrap items-start justify-between gap-3 rounded-2xl bg-brand/[0.065] px-4 py-3 ring-1 ring-brand/20">
        <div className="min-w-0">
          <p id="guided-timing-title" className="text-sm font-semibold text-white">
            {((windows.length === 1
              ? (t("timing_review.title_one") || "Encontramos 1 parte que conviene revisar")
              : (t("timing_review.title") || "Encontramos {count} partes que conviene revisar"))
              .replace("{count}", windows.length))}
          </p>
          <p className="mt-1 text-xs leading-relaxed text-ink-secondary">{t("timing_review.summary") || "Escuchá cada fragmento y confirmá o ajustá el momento en que aparece la frase."}</p>
        </div>
        <span className="shrink-0 rounded-full bg-black/20 px-2.5 py-1 text-[10px] font-semibold tabular-nums text-brand-light ring-1 ring-brand/20" aria-live="polite">
          {(t("timing_review.progress") || "{done} de {total} revisadas")
            .replace("{done}", windows.length - pending.length)
            .replace("{total}", windows.length)}
        </span>
      </div>

      <div className="rounded-2xl bg-surface-2/35 p-4 ring-1 ring-white/[0.08] shadow-xl shadow-black/15">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
          <div>
            <div className="flex items-center gap-2">
              <span className="rounded-md bg-amber-300/10 px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.1em] text-amber-200 ring-1 ring-amber-200/20">
                {(t("timing_review.issue_number") || "Parte {current} de {total}")
                  .replace("{current}", activeIndex + 1)
                  .replace("{total}", windows.length)}
              </span>
              {reviewRequired && <span className="text-[10px] font-medium text-amber-200/80">{t("timing_review.required") || "Revisión necesaria"}</span>}
            </div>
            <p className="mt-2 text-xs font-medium text-white">{reasonLabel(activeWindow, t)}</p>
          </div>
          <button
            type="button"
            onClick={() => onPlayWindow?.(activeWindow)}
            disabled={!audioAvailable || !activeWindowPlayable}
            className={`inline-flex h-9 items-center gap-2 rounded-xl px-3 text-xs font-semibold ring-1 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-light disabled:cursor-not-allowed disabled:opacity-45 ${playingWindowId === activeWindow.id && isPlaying ? "bg-brand text-white ring-brand-light/50" : "bg-white/[0.06] text-white ring-white/[0.1] hover:bg-white/[0.1]"}`}
            aria-label={!audioAvailable
              ? (audioLoading ? (t("timing_review.audio_loading") || "Cargando audio") : (t("timing_review.audio_unavailable") || "Audio no disponible"))
              : (playingWindowId === activeWindow.id && isPlaying ? (t("timing_review.stop_loop") || "Detener repetición") : (t("timing_review.play_loop") || "Reproducir este tramo en loop"))}
          >
            {playingWindowId === activeWindow.id && isPlaying ? (
              <svg className="h-3.5 w-3.5" fill="currentColor" viewBox="0 0 24 24" aria-hidden="true"><path d="M7 6h10v12H7z" /></svg>
            ) : (
              <svg className="h-3.5 w-3.5" fill="currentColor" viewBox="0 0 24 24" aria-hidden="true"><path d="m8 5 11 7-11 7z" /></svg>
            )}
            {playingWindowId === activeWindow.id && isPlaying ? (t("timing_review.stop") || "Detener") : (t("timing_review.listen") || "Escuchar en loop")}
          </button>
        </div>

        {(!audioAvailable || !activeWindowPlayable) && (
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2 rounded-xl bg-amber-300/[0.05] px-3 py-2 text-[11px] text-amber-100/80 ring-1 ring-amber-200/10" role="status">
            <span>{!activeWindowPlayable
              ? (t("timing_review.window_outside_audio") || "Este tramo quedó fuera de la duración del audio. Abrí la timeline para revisarlo.")
              : (audioLoading ? (t("timing_review.audio_loading") || "Cargando audio…") : (t("timing_review.audio_unavailable") || "El audio no está disponible para escuchar este tramo."))}</span>
            {!audioLoading && !audioAvailable && onRetryAudio && (
              <button type="button" onClick={onRetryAudio} className="rounded-lg px-2.5 py-1.5 font-semibold text-amber-100 ring-1 ring-amber-200/20 hover:bg-amber-200/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-amber-100">
                {t("timing_review.retry_audio") || "Reintentar audio"}
              </button>
            )}
          </div>
        )}

        <FocusedWaveform
          waveform={waveform}
          waveformLoading={waveformLoading}
          duration={effectiveDuration}
          rangeStart={rangeStart}
          rangeEnd={rangeEnd}
          segments={overlappingSegments}
          constraintSegments={segments}
          selectedId={selectedSegment?._id}
          currentTime={currentTime}
          onSeek={onSeek}
          onSelect={setSelectedSegmentId}
          onMove={onMove}
        />

        {selectedSegment ? (
          <div className="mt-3 rounded-xl bg-black/20 px-3 py-3 ring-1 ring-white/[0.07]">
            <div className="flex min-w-0 items-center gap-3">
              <span className="grid h-8 w-8 shrink-0 place-items-center rounded-lg bg-brand/15 text-brand-light" aria-hidden="true">
                <svg className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.8" viewBox="0 0 24 24"><path d="M4 8h16M7 5 4 8l3 3m10-6 3 3-3 3M4 16h16" strokeLinecap="round" strokeLinejoin="round" /></svg>
              </span>
              <div className="min-w-0 flex-1">
                <p className="truncate text-xs font-semibold text-white">{selectedSegment.text || t("timing_review.empty_phrase") || "Sin texto"}</p>
                <p className="mt-0.5 text-[10px] text-ink-tertiary">{t("timing_review.drag_hint") || "Arrastrá la frase sobre el audio o ajustala de a 0,1 segundos"}</p>
              </div>
              <span className="shrink-0 font-mono text-[10px] tabular-nums text-brand-light">{formatTime(selectedSegment.start, true)}</span>
            </div>
            {selectedMovementBlocked && (
              <p className="mt-2 text-[10px] leading-relaxed text-amber-100/75">{t("timing_review.overlap_blocked") || "Esta frase ya se superpone con otra. Usá la timeline avanzada para resolverlas juntas."}</p>
            )}
            <div className="mt-3 grid grid-cols-2 gap-2">
              <button type="button" disabled={selectedMovementBlocked || selectedMovementBounds.min >= 0} onClick={() => nudge(-NUDGE_S)} className="rounded-lg bg-white/[0.045] px-3 py-2 text-[11px] font-medium text-ink-secondary ring-1 ring-white/[0.08] hover:bg-white/[0.08] hover:text-white disabled:cursor-not-allowed disabled:opacity-40" aria-label={t("timing_review.earlier") || "Mover 0,1 segundos antes"}>
                ← {t("timing_review.earlier_short") || "0,1 s antes"}
              </button>
              <button type="button" disabled={selectedMovementBlocked || selectedMovementBounds.max <= 0} onClick={() => nudge(NUDGE_S)} className="rounded-lg bg-white/[0.045] px-3 py-2 text-[11px] font-medium text-ink-secondary ring-1 ring-white/[0.08] hover:bg-white/[0.08] hover:text-white disabled:cursor-not-allowed disabled:opacity-40" aria-label={t("timing_review.later") || "Mover 0,1 segundos después"}>
                {t("timing_review.later_short") || "0,1 s después"} →
              </button>
            </div>
          </div>
        ) : (
          <p className="mt-3 rounded-xl bg-amber-300/[0.05] px-3 py-2 text-[11px] text-amber-100/75 ring-1 ring-amber-200/10">{t("timing_review.no_phrase") || "No encontramos una frase dentro de este tramo. Escuchalo y revisalo en la timeline avanzada si necesitás agregar una."}</p>
        )}

        <div className="mt-4 flex flex-wrap items-center justify-between gap-2 border-t border-white/[0.07] pt-4">
          {pending.length > 1 ? <button type="button" onClick={() => {
            onStopPlayback?.();
            const next = nextWindow(activeWindow);
            if (next) setActiveWindowId(next.id);
          }} className="px-2 py-2 text-xs font-medium text-ink-tertiary hover:text-white">
            {t("timing_review.skip") || "Revisar después"}
          </button> : <span />}
          <div className="flex w-full flex-col gap-2 sm:w-auto sm:flex-row sm:items-center">
            <button type="button" onClick={openAdvanced} className="rounded-xl px-3 py-2 text-xs font-medium text-ink-secondary ring-1 ring-white/[0.08] hover:bg-white/[0.05] hover:text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-light">
              {t("timing_review.open_advanced") || "Timeline avanzada"}
            </button>
            <button type="button" onClick={confirmAndContinue} disabled={!audioAvailable || !activeWindowPlayable} className="inline-flex items-center justify-center gap-2 rounded-xl bg-emerald-700 px-4 py-2 text-xs font-semibold text-white shadow-lg shadow-emerald-950/20 hover:bg-emerald-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-emerald-200 disabled:cursor-not-allowed disabled:opacity-45">
              <svg className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth="2.5" viewBox="0 0 24 24" aria-hidden="true"><path d="m5 12 4 4L19 6" strokeLinecap="round" strokeLinejoin="round" /></svg>
              {t("timing_review.confirm_next") || "Confirmar y seguir"}
            </button>
          </div>
        </div>
      </div>
    </section>
  );
}
