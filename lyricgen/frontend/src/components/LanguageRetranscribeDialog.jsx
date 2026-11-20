import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  RETRANSCRIBE_LANGUAGES,
  isRetranscriptionDone,
  knownRetranscribeFailure,
  languageLabel,
  retranscribeFailureMessage,
} from "../lib/languageRetranscription";

export function LanguageSelect({ value, onChange, disabled = false, id, className = "" }) {
  return (
    <select
      id={id}
      data-testid="retranscribe-language-select"
      value={value}
      disabled={disabled}
      onChange={(event) => onChange(event.target.value)}
      aria-label="Idioma de la canción"
      className={`rounded-lg bg-surface-2 px-2 py-1 text-xs text-white ring-1 ring-white/15 ${className}`}
    >
      {RETRANSCRIBE_LANGUAGES.map((item) => (
        <option key={item.code} value={item.code}>{item.label}</option>
      ))}
    </select>
  );
}

// Confirm → save → POST /jobs/{id}/retranscribe-language → poll until the new
// transcription is installed, then reload the editor. The previous version
// always stays in the history; a failure leaves it on screen.
export default function LanguageRetranscribeDialog({
  open,
  initialLanguage = "es",
  initialPhase = "confirm",
  onClose,
  onBeforeStart,
  onStart,
  onPoll,
  onApplied,
  pollMs = 3000,
}) {
  const [language, setLanguage] = useState(initialLanguage);
  const [phase, setPhase] = useState(initialPhase);
  const [error, setError] = useState("");
  const [progress, setProgress] = useState(null);
  const aliveRef = useRef(true);
  const timerRef = useRef(null);

  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, []);

  useEffect(() => {
    if (!open) return;
    setLanguage(initialLanguage);
    setPhase(initialPhase);
    setError("");
    if (initialPhase === "running") schedulePoll(0);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const schedulePoll = (delay) => {
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(poll, delay);
  };

  const poll = async () => {
    timerRef.current = null;
    if (!aliveRef.current) return;
    const result = await onPoll();
    if (!aliveRef.current) return;
    if (!result?.ok) {
      // Transient network/server error: keep waiting, the job is durable.
      schedulePoll(pollMs);
      return;
    }
    if (result.language) setLanguage(result.language);
    setProgress(Number.isFinite(Number(result.progress)) ? Number(result.progress) : null);
    if (!isRetranscriptionDone(result.status)) {
      schedulePoll(pollMs);
      return;
    }
    if (result.status === "applied") {
      setPhase("done");
      onApplied?.(result);
      return;
    }
    setPhase("error");
    const generic = "No pudimos volver a transcribir la canción. La versión anterior sigue en el editor y en el historial.";
    setError(knownRetranscribeFailure(result.error)
      || (result.error ? `${generic} Detalle: ${result.error}` : generic));
  };

  const confirm = async () => {
    if (phase !== "confirm") return;
    setPhase("starting");
    setError("");
    try {
      const saved = await onBeforeStart();
      if (!saved?.ok || !Number.isInteger(saved.revision)) {
        throw new Error("No pudimos guardar la versión actual. Tus cambios siguen en pantalla; reintentá.");
      }
      const started = await onStart({ language, baseRevision: saved.revision });
      if (!started?.ok) {
        throw new Error(started?.reason === "network"
          ? "Sin conexión con el servidor. Reintentá en unos segundos."
          : (started?.message || retranscribeFailureMessage(started?.reason)));
      }
      if (!aliveRef.current) return;
      setPhase("running");
      schedulePoll(pollMs);
    } catch (exc) {
      if (!aliveRef.current) return;
      setPhase("confirm");
      setError(exc?.message || "No pudimos iniciar la nueva transcripción. Reintentá.");
    }
  };

  if (!open) return null;
  const label = languageLabel(language);
  const busy = phase === "starting" || phase === "running" || phase === "done";
  return createPortal(
    <div className="fixed inset-0 z-[120] flex items-center justify-center bg-black/70 p-4">
      <section role="dialog" aria-modal="true" aria-labelledby="retranscribe-title"
        data-testid="retranscribe-dialog"
        className="w-full max-w-lg rounded-2xl bg-surface-1 p-6 shadow-2xl ring-1 ring-white/15">
        <h2 id="retranscribe-title" className="text-lg font-semibold text-white">
          Volver a transcribir en otro idioma
        </h2>
        {(phase === "confirm" || phase === "starting") && (
          <>
            <label htmlFor="retranscribe-dialog-language" className="mt-4 block text-xs text-ink-secondary">
              Idioma de la canción
            </label>
            <LanguageSelect id="retranscribe-dialog-language" className="mt-1" value={language}
              onChange={setLanguage} disabled={phase === "starting"} />
            <p className="mt-3 text-sm text-ink-secondary">
              {`La versión actual queda guardada en el historial. Genly vuelve a escuchar la canción en ${label} y la letra nueva vuelve a revisión.`}
            </p>
          </>
        )}
        {(phase === "running" || phase === "done") && (
          <div role="status" aria-live="polite" className="mt-4 flex items-start gap-3">
            <span className="mt-0.5 h-5 w-5 shrink-0 animate-spin rounded-full border-2 border-sky-200/25 border-t-sky-200" aria-hidden="true" />
            <p className="text-sm text-ink-secondary">
              {phase === "done"
                ? "Listo. Cargando la letra nueva…"
                : `Genly está escuchando la canción en ${label}. Puede tardar unos minutos; no cierres esta pestaña.`}
              {phase === "running" && progress != null && progress > 1 && (
                <span className="mt-1 block text-xs text-ink-tertiary">{`Progreso: ${progress}%`}</span>
              )}
            </p>
          </div>
        )}
        {error && <p role="alert" className="mt-3 text-sm text-red-300">{error}</p>}
        <div className="mt-5 flex justify-end gap-3">
          {!busy && (
            <button type="button" onClick={onClose} className="rounded-lg px-3 py-2 text-sm text-white">
              {phase === "error" ? "Cerrar" : "Cancelar"}
            </button>
          )}
          {(phase === "confirm" || phase === "starting") && (
            <button type="button" data-testid="retranscribe-confirm" disabled={phase === "starting"}
              onClick={confirm}
              className="rounded-lg bg-brand px-4 py-2 text-sm font-medium text-white disabled:opacity-50">
              {phase === "starting" ? "Iniciando…" : `Volver a transcribir en ${label}`}
            </button>
          )}
        </div>
      </section>
    </div>,
    document.body,
  );
}
