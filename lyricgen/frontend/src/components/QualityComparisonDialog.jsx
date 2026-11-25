import { useEffect, useId, useRef, useState } from "react";
import NativeLyricPreview from "./NativeLyricPreview";

const button = "rounded-lg border border-white/20 px-3 py-2 text-sm font-medium text-gray-100 hover:border-cyan-300/50 disabled:opacity-40";

export default function QualityComparisonDialog({
  comparison, jobId, audioUrl, backgroundUrl, settings, request,
  onClose, onDecision, onEdit, onApply, applying,
}) {
  const title = useId();
  const dialog = useRef(null);
  const audio = useRef(null);
  const firstHeard = useRef(null);
  const [eventId] = useState(() => globalThis.crypto?.randomUUID?.()
    || `ab-${Date.now()}-${Math.random().toString(36).slice(2)}`);
  const [variantA] = useState(() => Math.random() < 0.5 ? "current" : "proposed");
  const identity = variant => variant === "A" ? variantA : (variantA === "current" ? "proposed" : "current");
  const [variant, setVariant] = useState("A");
  const [heard, setHeard] = useState({});
  const [currentTime, setCurrentTime] = useState(comparison.start);
  const [previewState, setPreviewState] = useState("loading");
  const [playing, setPlaying] = useState(false);
  const [audioReady, setAudioReady] = useState(false);
  const [audioError, setAudioError] = useState(false);
  const [vote, setVote] = useState(null);
  const [saving, setSaving] = useState(false);
  const [failedVote, setFailedVote] = useState(null);
  const end = audioReady ? Math.min(comparison.end, audio.current?.duration ?? comparison.end) : comparison.end;

  useEffect(() => {
    const element = dialog.current;
    const previousFocus = document.activeElement;
    element.showModal();
    return () => {
      if (element.open) element.close();
      previousFocus?.focus();
    };
  }, []);

  useEffect(() => {
    const element = audio.current;
    return () => element.pause();
  }, []);

  useEffect(() => {
    if (!playing || previewState !== "ready") return;
    let frame;
    const element = audio.current;
    const update = () => {
      const time = element.currentTime;
      setCurrentTime(Math.min(time, end));
      if (time >= end || element.ended) {
        element.pause();
        setHeard(previous => ({ ...previous, [variant]: true }));
      } else {
        frame = requestAnimationFrame(update);
      }
    };
    frame = requestAnimationFrame(update);
    return () => cancelAnimationFrame(frame);
  }, [end, playing, previewState, variant]);

  function choose(next) {
    if (next === variant) return;
    audio.current.pause();
    setPlaying(false);
    setCurrentTime(comparison.start);
    setPreviewState("loading");
    setVariant(next);
  }

  function changePreviewState(state) {
    setPreviewState(state);
    if (state !== "ready") audio.current?.pause();
  }

  async function play() {
    const element = audio.current;
    setAudioError(false);
    try {
      element.currentTime = comparison.start;
      setCurrentTime(comparison.start);
      await element.play();
    } catch (error) {
      if (error.name !== "AbortError") setAudioError(true);
    }
  }

  async function decide(selection) {
    setSaving(true);
    setFailedVote(null);
    const choice = selection === "neither" ? selection : identity(selection);
    try {
      const accepted = await onDecision({
        event_id: eventId, choice, variant_a: variantA, first_heard: identity(firstHeard.current),
        position_ms: Math.round(comparison.start * 1000),
        duration_ms: Math.round((end - comparison.start) * 1000),
      });
      if (accepted) setVote(choice);
      else setFailedVote(selection);
    } catch {
      setFailedVote(selection);
    } finally {
      setSaving(false);
    }
  }

  return (
    <dialog ref={dialog} aria-labelledby={title}
      onCancel={event => { event.preventDefault(); onClose(); }}
      onKeyDown={event => event.stopPropagation()}
      className="max-h-[90dvh] w-[calc(100%_-_2rem)] max-w-2xl overflow-y-auto rounded-2xl border border-white/15 bg-slate-950 p-4 text-white backdrop:bg-black/80 sm:p-6">
      <div className="mb-4 flex items-center justify-between gap-3">
        <h2 id={title} className="text-lg font-semibold">Comparar A/B</h2>
        <button type="button" className={button} onClick={onClose}>Cerrar</button>
      </div>
      <audio ref={audio} preload="auto" src={audioUrl}
        onLoadStart={() => { setAudioReady(false); setAudioError(false); setPlaying(false); }}
        onLoadedMetadata={() => {
          const duration = audio.current.duration;
          setAudioReady(Number.isFinite(duration) && duration >= comparison.end - 0.05 && duration > comparison.start);
          setAudioError(!Number.isFinite(duration) || duration < comparison.end - 0.05 || duration <= comparison.start);
        }}
        onPlaying={() => { firstHeard.current ??= variant; setPlaying(true); }}
        onPause={() => setPlaying(false)}
        onEnded={() => {
          if (previewState === "ready" && audio.current.currentTime >= end) {
            setHeard(previous => ({ ...previous, [variant]: true }));
          }
          setPlaying(false);
        }}
        onError={() => { setPlaying(false); setAudioReady(false); setAudioError(true); }}
      />
      <div className="mb-3 flex flex-wrap items-center gap-2">
        {["A", "B"].map(option => (
          <button key={option} type="button" className={`${button} aria-pressed:border-cyan-300 aria-pressed:bg-cyan-950`} aria-pressed={option === variant}
            onClick={() => choose(option)}>Versión {option}{heard[option] ? " ✓" : ""}</button>
        ))}
        <button type="button" className={button} disabled={!audioReady || previewState !== "ready"}
          onClick={playing ? () => audio.current.pause() : play}>
          {playing ? "Pausar" : `Reproducir ${variant}`}
        </button>
      </div>
      <NativeLyricPreview key={variant} segments={comparison[identity(variant)]} settings={settings}
        jobId={jobId} request={request} backgroundUrl={backgroundUrl} currentTime={currentTime}
        isPlaying={playing} onStateChange={changePreviewState} />
      {onEdit && <button type="button" className={`mt-4 ${button}`} onClick={onEdit}>Corregir en el editor</button>}
      {audioError && <p role="alert" className="mt-3 text-sm text-amber-200">No se pudo reproducir el tramo del audio original.</p>}
      {vote ? (
        <div className="mt-4 space-y-3">
          <p role="status" className="text-sm text-gray-200">
            Decisión guardada. {variantA === "current" ? "A es el borrador actual. B es la propuesta." : "A es la propuesta. B es el borrador actual."}
          </p>
          {vote === "proposed" && onApply && <button type="button"
            className="rounded-lg bg-cyan-400 px-3 py-2 text-sm font-semibold text-slate-950 disabled:opacity-50"
            disabled={applying} onClick={onApply}>{applying ? "Aplicando…" : "Aplicar al borrador"}</button>}
        </div>
      ) : failedVote ? (
        <div className="mt-4 space-y-2">
          <p role="alert" className="text-sm text-amber-200">No se confirmó el guardado de la decisión.</p>
          <button type="button" className={button} onClick={() => decide(failedVote)}>Reintentar guardado</button>
        </div>
      ) : (
        <div className="mt-4 space-y-2">
          <p className="text-sm text-gray-300">Reproducí A y B para elegir. Si encontrás errores de letra o tiempos en ambas, podés ir directo al editor.</p>
          <div className="flex flex-wrap gap-2">
            {[["A", "Prefiero A"], ["B", "Prefiero B"], ["neither", "Ambas necesitan edición"]].map(([selection, label]) => (
              <button key={selection} type="button" className={button}
                disabled={!heard.A || !heard.B || playing || saving} onClick={() => decide(selection)}>{label}</button>
            ))}
          </div>
        </div>
      )}
    </dialog>
  );
}
