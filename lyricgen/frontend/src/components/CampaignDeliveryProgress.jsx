import { useEffect, useRef, useState } from "react";

const portals = { argentina: ["Argentina", "https://umg.genly.pro"], chile: ["Chile", "https://umgchile.genly.pro"] };
const errors = {
  stale_approval: "La aprobación o el video cambió. Revisá y aprobá la versión actual.",
  deliverables_not_ready: "Faltan archivos de entrega. Revisá el detalle del video.",
  portal_contract_unavailable: "El portal no está disponible para este envío.",
  ambiguous_replacement: "Hay varios pedidos del cliente vinculados a esta canción. Elegí la entrega a corregir desde Cambios antes de publicar.",
  unexpected_error: "Ocurrió un error inesperado al publicar. Se puede reintentar.",
};
const RETRY_CODES = {
  operation_in_progress: "El envío todavía se está procesando. Esperá a que termine o a que aparezca como detenido.",
  nothing_to_retry: "No queda nada para reintentar en este envío.",
};
const RETRY_OUTCOME = {
  queued: { tone: "ok", text: "Reintento en marcha." },
  already_running: { tone: "ok", text: "El envío ya se está procesando; esperá a que termine." },
  unavailable: { tone: "error", text: "No pudimos poner el envío en cola. Probá de nuevo en un minuto; si sigue igual, avisá al equipo." },
};

const ITEM_STATUS = { sent: "Enviado", published: "Enviado", completed: "Enviado", failed: "Con error", pending: "En cola", queued: "En cola", sending: "Enviando", processing: "Enviando" };

export default function CampaignDeliveryProgress({ operationId, request, onSelectFailed, onSettled, describe }) {
  const [operation, setOperation] = useState(null);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [retrying, setRetrying] = useState(false);
  const [retryNotice, setRetryNotice] = useState(null);
  const settledRef = useRef(null);
  const onSettledRef = useRef(onSettled);
  onSettledRef.current = onSettled;
  useEffect(() => {
    const controller = new AbortController();
    let timer;
    setOperation(null); setError("");
    const poll = async () => {
      try {
        const result = await request(`/batch/delivery-operations/${encodeURIComponent(operationId)}`, { signal: controller.signal });
        if (controller.signal.aborted) return;
        setOperation(result); setError("");
        if (["completed", "partial", "failed"].includes(result.status)) {
          const signature = `${operationId}:${result.status}:${result.sent_count}:${result.failed_count}`;
          if (settledRef.current !== signature) {
            settledRef.current = signature;
            onSettledRef.current?.();
          }
        }
        if (!["completed", "partial", "failed"].includes(result.status)) timer = setTimeout(poll, 4000);
      } catch (e) { if (!controller.signal.aborted) setError(e.message); }
    };
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [operationId, request, refresh]);
  const failed = operation?.items?.filter(item => item.status === "failed") || [];
  const retryable = failed.filter(item => item.retryable !== false);
  // A send that is still running normally is not retryable; the server would
  // answer 409 operation_in_progress. Offer it only once it stopped or stalled.
  const settled = ["partial", "failed"].includes(operation?.status);
  const canRetry = Boolean(operation) && operation.status !== "completed" && (operation.stalled || (settled && retryable.length > 0));
  const portal = portals[operation?.destination_portal];
  const retry = async () => {
    if (retrying) return;
    setRetrying(true); setRetryNotice(null);
    try {
      const result = await request(`/batch/delivery-operations/${encodeURIComponent(operationId)}/retry`, { method: "POST", json: {} });
      setRetryNotice(RETRY_OUTCOME[result.outcome] || RETRY_OUTCOME.queued);
      setRefresh(n => n + 1);
    } catch (e) {
      setRetryNotice({ tone: "error", text: RETRY_CODES[e.code] || e.message || "No se pudo reintentar." });
    } finally {
      setRetrying(false);
    }
  };
  return <section aria-label="Seguimiento del envío" className="space-y-3 rounded-xl bg-brand/10 p-4 ring-1 ring-brand/30">
    <h3 className="font-semibold">Envío al portal{portal ? ` · ${portal[0]}` : ""}</h3>
    {!operation && !error && <p role="status">Consultando el envío…</p>}
    {error && <p role="alert">No pudimos actualizar el envío: {error} <button className="underline" onClick={() => setRefresh(n => n + 1)}>Reintentar consulta</button></p>}
    {operation && <>
      <p role="status">{operation.sent_count || 0} de {operation.total_count} enviados · {failed.length} con error{operation.status === "completed" ? " · Envío completado" : ["partial", "failed"].includes(operation.status) ? " · Requiere atención" : operation.stalled ? " · Detenido" : " · En curso"}</p>
      {operation.stalled && <p role="alert" className="text-sm text-amber-200">Este envío parece detenido: hace más de 10 minutos que no avanza. Podés reintentarlo; no se duplica nada de lo ya enviado.</p>}
      <p className="text-xs text-ink-secondary">Este resumen es de este envío, no de toda la campaña. Podés salir y volver a este enlace para consultar el avance.</p>
      {failed.length > 0 && <><ul className="max-h-40 overflow-auto text-sm">{failed.map(item => <li key={item.job_id}>{describe?.(item.job_id) || item.job_id}: {errors[item.error_code] || "No se pudo enviar. Revisá los archivos y volvé a intentarlo."}{item.error_detail && !errors[item.error_code] ? ` (${item.error_detail})` : ""}</li>)}</ul><button className="text-sm underline" onClick={() => onSelectFailed(failed.map(item => item.job_id))}>Seleccionar sólo los fallidos</button></>}
      {canRetry && <button className="rounded-lg bg-brand px-3 py-1.5 text-sm font-medium text-white disabled:opacity-60" disabled={retrying} onClick={retry}>{retrying ? "Reintentando…" : operation.stalled && !retryable.length ? "Reintentar envío" : `Reintentar ${retryable.length} ${retryable.length === 1 ? "fallido" : "fallidos"}`}</button>}
      {retryNotice && <p role={retryNotice.tone === "error" ? "alert" : "status"} className="text-sm">{retryNotice.text}</p>}
      {operation.items?.length > 0 && <details className="text-sm"><summary className="cursor-pointer text-ink-secondary">Canciones de este envío ({operation.items.length})</summary>
        <ul className="mt-2 max-h-48 space-y-1 overflow-auto">{operation.items.map(item => <li key={item.job_id} className="flex justify-between gap-3"><span className="truncate">{describe?.(item.job_id) || item.job_id}</span><span className="shrink-0 text-xs text-ink-secondary">{ITEM_STATUS[item.status] || item.status}</span></li>)}</ul>
      </details>}
      {portal && <a href={portal[1]} target="_blank" rel="noreferrer" className="inline-block text-sm text-brand-light underline">Abrir portal {portal[0]}</a>}
    </>}
  </section>;
}
