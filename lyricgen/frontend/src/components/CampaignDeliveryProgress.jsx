import { useEffect, useRef, useState } from "react";

const portals = { argentina: ["Argentina", "https://umg.genly.pro"], chile: ["Chile", "https://umgchile.genly.pro"] };
const errors = { stale_approval: "La aprobación o el video cambió. Revisá y aprobá la versión actual.", deliverables_not_ready: "Faltan archivos de entrega. Revisá el detalle del video.", delivery_qc_blocked: "Hay controles pendientes para este corte. Abrí el detalle del video.", preparing_prores: "Se está preparando el archivo profesional. Reintentá este envío cuando termine.", delivery_manifest_unavailable: "No se pudieron verificar los archivos. Revisá el video y reintentá.", delivery_publish_failed: "No se pudo registrar la publicación. Reintentá el envío.", portal_contract_unavailable: "El portal no está disponible para este envío." };

export default function CampaignDeliveryProgress({ operationId, request, onSelectFailed, onSettled }) {
  const [operation, setOperation] = useState(null);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [resuming, setResuming] = useState(false);
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
      } catch (e) {
        if (!controller.signal.aborted) {
          setError(e.message);
          timer = setTimeout(poll, 8000);
        }
      }
    };
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [operationId, request, refresh]);
  const failed = operation?.items?.filter(item => item.status === "failed") || [];
  const portal = portals[operation?.destination_portal];
  const resume = async () => {
    setResuming(true);
    try {
      await request(`/batch/delivery-operations/${encodeURIComponent(operationId)}/resume`, { method: "POST" });
      setError("");
      setRefresh(value => value + 1);
    } catch (e) { setError(e.message); }
    finally { setResuming(false); }
  };
  return <section aria-label="Seguimiento del envío" className="space-y-3 rounded-xl bg-brand/10 p-4 ring-1 ring-brand/30">
    <h3 className="font-semibold">Envío al portal{portal ? ` · ${portal[0]}` : ""}</h3>
    {!operation && !error && <p role="status">Consultando el envío…</p>}
    {error && <p role="alert">No pudimos actualizar el envío: {error} <button className="underline" onClick={() => setRefresh(n => n + 1)}>Reintentar consulta</button></p>}
    {operation && <>
      <p role="status">{operation.sent_count || 0} de {operation.total_count} publicados · {failed.length} con error{operation.status === "completed" ? " · Envío completado" : ["partial", "failed"].includes(operation.status) ? " · Requiere atención" : operation.status === "waiting_worker" ? " · Esperando ejecutor" : operation.status === "queued" ? " · En cola" : " · En curso"}</p>
      {operation.updated_at && <p className="text-xs text-ink-secondary">Última actualización: {new Date(operation.updated_at).toLocaleString()}</p>}
      {operation.status === "waiting_worker" && <button type="button" className="rounded-lg bg-brand px-3 py-2 text-sm font-semibold text-white disabled:opacity-50" disabled={resuming} onClick={resume}>{resuming ? "Reintentando…" : "Reintentar inicio del envío"}</button>}
      <p className="text-xs text-ink-secondary">Podés salir y volver a este enlace para consultar el avance.</p>
      {failed.length > 0 && <><ul className="max-h-40 overflow-auto text-sm">{failed.map(item => <li key={item.job_id}>{item.job_id}: {errors[item.error_code] || "No se pudo enviar. Revisá los archivos y volvé a intentarlo."}</li>)}</ul><button className="text-sm underline" onClick={() => onSelectFailed(failed.map(item => item.job_id))}>Seleccionar sólo los fallidos</button></>}
      {portal && <a href={portal[1]} target="_blank" rel="noreferrer" className="inline-block text-sm text-brand-light underline">Abrir portal {portal[0]}</a>}
    </>}
  </section>;
}
