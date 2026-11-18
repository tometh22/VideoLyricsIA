import { useCallback, useEffect, useRef, useState } from "react";

const sameRows = (left, right) => Array.isArray(left) && Array.isArray(right)
  && left.length === right.length && left.every((row, index) => row.text === right[index].text
    && Number(row.start) === Number(right[index].start) && Number(row.end) === Number(right[index].end));
const stamp = value => `${Number(value).toFixed(2)} s`;
const stageLabel = {
  queued: "En espera", processing: "Analizando la canción", downloading: "Preparando el audio",
  recognizing: "Reconociendo la letra", aligning: "Sincronizando la letra", publishing: "Preparando la propuesta",
  complete: "Análisis completo", partial: "Análisis parcial", failed: "El análisis no terminó",
  stale: "La canción cambió: hace falta un nuevo análisis", worker_unavailable: "El procesador local se desconectó",
};

export default function LocalGenlyModelCandidate({ jobId, revision, segments, request, enabled,
  onSeek, onProposalReady }) {
  const [available, setAvailable] = useState(false);
  const [state, setState] = useState(null);
  const [candidate, setCandidate] = useState(null);
  const [error, setError] = useState(null);
  const [starting, setStarting] = useState(false);
  const callbackRef = useRef(onProposalReady);
  callbackRef.current = onProposalReady;
  const lastCompleted = useRef(null);

  const refresh = useCallback(async (cancelled = () => false) => {
    if (!enabled || !request || !jobId) return;
    try {
      const response = await request(`/editor/${jobId}/genly-model/status`);
      if (cancelled()) return;
      if (response.status === 404) { setAvailable(false); return; }
      if (!response.ok) throw new Error("No pudimos consultar el procesador de Genly.");
      const body = await response.json();
      if (cancelled()) return;
      setAvailable(true);
      setState(body);
      setError(null);
      const candidateResponse = await request(`/editor/${jobId}/genly-model/candidate`);
      if (cancelled()) return;
      if (!candidateResponse.ok) throw new Error("No pudimos recuperar la propuesta de Genly.");
      const result = await candidateResponse.json();
      if (cancelled()) return;
      setCandidate(result.candidate || null);
      const identity = result.candidate?.candidate_sha256;
      if (identity && identity !== lastCompleted.current && result.candidate.source?.segments_revision === revision) {
        lastCompleted.current = identity;
        callbackRef.current?.(result.quality_proposal || null);
      }
    } catch (failure) {
      if (!cancelled()) setError(failure.message || "No pudimos consultar el modelo.");
    }
  }, [enabled, request, jobId, revision]);

  useEffect(() => {
    let stopped = false;
    setCandidate(null);
    setState(null);
    setAvailable(false);
    lastCompleted.current = null;
    refresh(() => stopped);
    return () => { stopped = true; };
  }, [refresh]);

  const running = ["queued", "processing", "worker_unavailable"].includes(state?.request?.status);
  useEffect(() => {
    if (!available || !running) return undefined;
    let stopped = false;
    const timer = setInterval(() => refresh(() => stopped), 5000);
    return () => { stopped = true; clearInterval(timer); };
  }, [available, running, refresh]);

  const start = async () => {
    setStarting(true);
    setError(null);
    try {
      const response = await request(`/editor/${jobId}/genly-model/request`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ base_revision: revision }),
      });
      if (!response.ok) throw new Error(response.status === 409
        ? "La canción cambió o ya está aprobada. Actualizá el editor antes de analizar."
        : "No pudimos iniciar el análisis.");
      setState(await response.json());
      await refresh();
    } catch (failure) { setError(failure.message); }
    finally { setStarting(false); }
  };

  if (!enabled || !available) return null;
  const currentCandidate = candidate && Number.isInteger(revision)
    && candidate.source?.segments_revision === revision && sameRows(candidate.baseline, segments)
    ? candidate : null;
  const status = state?.request?.status;
  const label = stageLabel[status === "processing" ? state.request.stage : status];
  return <section aria-label="Modelo Genly local" className="mb-4 rounded-xl border border-cyan-400/20 p-4">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <h3 className="font-semibold text-cyan-200">Modelo Genly · piloto</h3>
      <div className="flex gap-3">
        <button type="button" className="text-sm underline disabled:opacity-50" onClick={start}
          disabled={starting || running || !state?.worker_available || !Number.isInteger(revision)}>
          {starting ? "Solicitando…" : "Analizar canción completa"}
        </button>
        <button type="button" className="text-sm underline" onClick={() => refresh()}>Actualizar estado</button>
      </div>
    </div>
    <p className="mt-2 text-sm text-gray-300">Propone letra y sincronización para escuchar y revisar. Los cambios se incorporan desde las sugerencias del editor y requieren tu aceptación.</p>
    {!state?.worker_available && <p role="status" className="mt-2 text-sm text-amber-200">El procesador local está desconectado. El análisis estará disponible cuando vuelva a conectarse.</p>}
    {label && <p role="status" className="mt-2 text-sm">{label}</p>}
    {error && <p role="alert" className="mt-2 text-sm text-amber-200">{error}</p>}
    {candidate && !currentCandidate && <p className="mt-2 text-sm text-amber-200">La propuesta corresponde a otra versión o hay cambios sin guardar. Conservamos tus ediciones.</p>}
    {currentCandidate && <>
      <p className="mt-2 text-sm">{currentCandidate.status === "complete" ? "Se recorrió el audio completo." : "Quedan fragmentos pendientes. Podés revisar las propuestas de las líneas que cuentan con evidencia acústica."}
        {` ${currentCandidate.changes.length} líneas con cambios propuestos.`} Requiere revisión humana.</p>
      {currentCandidate.changes.length > 0 && currentCandidate.adoption_status !== "matching_existing_proposal"
        && <p className="text-sm text-amber-200">La candidata está disponible para comparar. Las sugerencias actuales se conservan; todavía no hay una acción de incorporación asociada a esta candidata.</p>}
      <details className="mt-3">
        <summary>Ver letra y tiempos de toda la candidata ({currentCandidate.segments.length} líneas)</summary>
        <ol className="mt-2 space-y-2">{currentCandidate.segments.map((row, index) => <li key={row.id || index} className="rounded bg-white/5 p-2">
          <button type="button" className="mr-2 text-cyan-300" onClick={() => onSeek?.(Math.max(0, row.start - 1))}
            aria-label={`Escuchar candidata línea ${index + 1}`}>▶ {index + 1} · {stamp(row.start)}–{stamp(row.end)}</button>
          <span>{row.text}</span>
          {!sameRows([row], [currentCandidate.baseline[index]]) && <p className="text-xs text-gray-400">
            Actual: {stamp(currentCandidate.baseline[index].start)}–{stamp(currentCandidate.baseline[index].end)} · {currentCandidate.baseline[index].text}
          </p>}
        </li>)}</ol>
      </details>
      <p className="mt-2 text-xs text-gray-400">Versión del modelo: {currentCandidate.model_id}</p>
    </>}
  </section>;
}
