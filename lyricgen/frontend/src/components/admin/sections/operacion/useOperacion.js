// Hook dueño de TODA la data de la sección Operación.
//
// Acá viven los tres flujos de triaje del operador:
//   - health + stuck-jobs (refresh 15 s, pausado si la pestaña está oculta)
//   - jobs en vivo (refresh 5 s cuando auto-refresh está on)
//   - change requests de UMG (sin auto-refresh, recarga manual / al cambiar filtro)
//
// Todo lo de red pasa por fetchJson (un solo lugar con auth + res.ok). Las
// mutaciones reportan error vía useAdmin().flashError, igual que el admin viejo.
import { useCallback, useEffect, useRef, useState } from "react";

import { API, fetchJson } from "../../adminApi";

async function changeRequestIdempotencyKey(proposalId, baseRevision, operationIds) {
  const signature = `${proposalId}:${baseRevision}:${[...operationIds].sort().join(",")}`;
  if (globalThis.crypto?.subtle && typeof TextEncoder !== "undefined") {
    const bytes = await globalThis.crypto.subtle.digest(
      "SHA-256", new TextEncoder().encode(signature),
    );
    const hex = [...new Uint8Array(bytes)]
      .map((value) => value.toString(16).padStart(2, "0")).join("");
    return `change-request-${hex}`;
  }
  // Deterministic fallback for older admin browsers. The proposal UUID and
  // revision remain in the input, so a reload still produces the same key.
  let hash = 2166136261;
  for (const char of signature) {
    hash ^= char.charCodeAt(0);
    hash = Math.imul(hash, 16777619);
  }
  return `change-request-${proposalId}-${baseRevision}-${(hash >>> 0).toString(16)}`;
}
import { useAdmin } from "../../AdminContext";

const STUCK_THRESHOLD_MIN = 100;

export default function useOperacion() {
  const { flashError } = useAdmin();

  // --- Health + stuck jobs (15 s) ------------------------------------------
  const [health, setHealth] = useState(null);
  const [stuckJobs, setStuckJobs] = useState({ count: 0, jobs: [] });
  const [reaperRunning, setReaperRunning] = useState(false);
  const [reaperResult, setReaperResult] = useState(null);

  const loadHealth = useCallback(async () => {
    try {
      // /health es público — NO mandar auth headers.
      setHealth(await fetchJson(`${API}/health`));
    } catch {
      setHealth({ status: "error", _fetch_failed: true });
    }
  }, []);

  const loadStuckJobs = useCallback(async () => {
    try {
      setStuckJobs(await fetchJson(`${API}/admin/stuck-jobs?threshold_min=${STUCK_THRESHOLD_MIN}`));
    } catch {
      // Best-effort: si falla dejamos el último conteo conocido.
    }
  }, []);

  const runReaperNow = useCallback(async () => {
    if (reaperRunning) return;
    setReaperRunning(true);
    setReaperResult(null);
    try {
      const data = await fetchJson(
        `${API}/admin/runbook/reaper-now?threshold_min=${STUCK_THRESHOLD_MIN}`,
        { method: "POST" },
      );
      setReaperResult(data);
      // Refrescamos el conteo enseguida para que el banner refleje la realidad.
      loadStuckJobs();
    } catch (err) {
      setReaperResult({ error: String(err.message || err) });
    } finally {
      setReaperRunning(false);
    }
  }, [reaperRunning, loadStuckJobs]);

  useEffect(() => {
    loadHealth();
    loadStuckJobs();
    const iv = setInterval(() => {
      if (typeof document !== "undefined" && document.hidden) return;
      loadHealth();
      loadStuckJobs();
    }, 15000);
    return () => clearInterval(iv);
  }, [loadHealth, loadStuckJobs]);

  // --- Jobs en vivo (5 s) ---------------------------------------------------
  const [jobs, setJobs] = useState([]);
  const [jobsTotal, setJobsTotal] = useState(0);
  const [jobsStatusFilter, setJobsStatusFilter] = useState("");
  const [jobsTenantFilter, setJobsTenantFilter] = useState("");
  const [jobsAutoRefresh, setJobsAutoRefresh] = useState(true);
  const [jobsLoading, setJobsLoading] = useState(true);

  // Refs para que el intervalo lea los filtros vigentes sin re-crearse.
  const jobsStatusRef = useRef(jobsStatusFilter);
  const jobsTenantRef = useRef(jobsTenantFilter);
  jobsStatusRef.current = jobsStatusFilter;
  jobsTenantRef.current = jobsTenantFilter;

  const loadJobs = useCallback(async () => {
    try {
      const tenant = jobsTenantRef.current
        ? `&tenant_id=${encodeURIComponent(jobsTenantRef.current)}` : "";
      const status = jobsStatusRef.current
        ? `&status=${encodeURIComponent(jobsStatusRef.current)}` : "";
      const data = await fetchJson(`${API}/admin/jobs?limit=200${tenant}${status}`);
      setJobs(data.jobs || []);
      setJobsTotal(data.total || 0);
    } catch {
      // Best-effort: el auto-refresh reintentará en 5 s.
    } finally {
      setJobsLoading(false);
    }
  }, []);

  // Carga inmediata al montar y cada vez que cambian los filtros.
  useEffect(() => {
    setJobsLoading(true);
    loadJobs();
  }, [jobsStatusFilter, jobsTenantFilter, loadJobs]);

  // Auto-refresh cada 5 s, pausado si la pestaña está oculta.
  useEffect(() => {
    if (!jobsAutoRefresh) return;
    const iv = setInterval(() => {
      if (typeof document !== "undefined" && document.hidden) return;
      loadJobs();
    }, 5000);
    return () => clearInterval(iv);
  }, [jobsAutoRefresh, loadJobs]);

  // --- Change requests de UMG ----------------------------------------------
  const [changeRequests, setChangeRequests] = useState([]);
  const [crStatusFilter, setCrStatusFilter] = useState("pending");
  const [crPendingCount, setCrPendingCount] = useState(0);
  const [crResolvedCount, setCrResolvedCount] = useState(0);
  const [crLoading, setCrLoading] = useState(true);
  const [crResolvingId, setCrResolvingId] = useState(null);
  const [crProposalEnabled, setCrProposalEnabled] = useState(false);
  const [crProposalApplyEnabled, setCrProposalApplyEnabled] = useState(false);
  const [crProposalBusyId, setCrProposalBusyId] = useState(null);
  const [crProposalDetails, setCrProposalDetails] = useState({});

  const crStatusRef = useRef(crStatusFilter);
  crStatusRef.current = crStatusFilter;

  const loadChangeRequests = useCallback(async () => {
    setCrLoading(true);
    try {
      const data = await fetchJson(
        `${API}/admin/change-requests?status=${crStatusRef.current}&limit=200`,
      );
      setChangeRequests(data.items || []);
      setCrPendingCount(data.pending_count || 0);
      setCrResolvedCount(data.resolved_count || 0);
      setCrProposalEnabled(data.proposal_enabled === true);
      setCrProposalApplyEnabled(data.proposal_apply_enabled === true);
    } catch (err) {
      flashError(`No pude cargar los cambios: ${err.message || err}`);
    } finally {
      setCrLoading(false);
    }
  }, [flashError]);

  const storeProposal = useCallback((requestId, proposal) => {
    setCrProposalDetails((current) => ({ ...current, [requestId]: proposal }));
  }, []);

  const generateChangeRequestProposal = useCallback(async (requestId) => {
    setCrProposalBusyId(requestId);
    try {
      const data = await fetchJson(`${API}/admin/change-requests/${requestId}/proposals`, {
        method: "POST",
      });
      storeProposal(requestId, data.proposal);
      await loadChangeRequests();
      return data.proposal;
    } catch (err) {
      flashError(`No pude analizar el pedido: ${err.message || err}`);
      return null;
    } finally {
      setCrProposalBusyId(null);
    }
  }, [flashError, loadChangeRequests, storeProposal]);

  const loadChangeRequestProposal = useCallback(async (requestId) => {
    setCrProposalBusyId(requestId);
    try {
      const data = await fetchJson(
        `${API}/admin/change-requests/${requestId}/proposals/current`,
      );
      storeProposal(requestId, data.proposal);
      return data.proposal;
    } catch (err) {
      flashError(`No pude cargar la propuesta: ${err.message || err}`);
      return null;
    } finally {
      setCrProposalBusyId(null);
    }
  }, [flashError, storeProposal]);

  const adjustChangeRequestProposal = useCallback(async (
    requestId, proposalId, operationId, requestedText, baseRevision,
  ) => {
    setCrProposalBusyId(requestId);
    try {
      const data = await fetchJson(
        `${API}/admin/change-requests/${requestId}/proposals/${proposalId}`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            operation_id: operationId,
            requested_text: requestedText,
            base_revision: baseRevision,
          }),
        },
      );
      storeProposal(requestId, data.proposal);
      return data.proposal;
    } catch (err) {
      flashError(`No pude ajustar la propuesta: ${err.message || err}`);
      return null;
    } finally {
      setCrProposalBusyId(null);
    }
  }, [flashError, storeProposal]);

  const applyChangeRequestProposal = useCallback(async (
    requestId, proposalId, operationIds, baseRevision,
  ) => {
    setCrProposalBusyId(requestId);
    try {
      const idempotencyKey = await changeRequestIdempotencyKey(
        proposalId, baseRevision, operationIds,
      );
      const data = await fetchJson(
        `${API}/admin/change-requests/${requestId}/proposals/${proposalId}/apply`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            base_revision: baseRevision,
            operation_ids: operationIds,
            idempotency_key: idempotencyKey,
          }),
        },
      );
      storeProposal(requestId, { ...data.proposal, editor_url: data.editor_url });
      setCrPublishNotice({
        tone: "ok",
        text: "La corrección quedó guardada. Abrí el editor para revisar y re-renderizar; el pedido seguirá pendiente hasta publicar.",
      });
      await loadChangeRequests();
      return data;
    } catch (err) {
      flashError(`No pude aplicar la propuesta: ${err.message || err}`);
      return null;
    } finally {
      setCrProposalBusyId(null);
    }
  }, [flashError, loadChangeRequests, storeProposal]);

  const dismissChangeRequestProposal = useCallback(async (requestId, proposalId) => {
    setCrProposalBusyId(requestId);
    try {
      const data = await fetchJson(
        `${API}/admin/change-requests/${requestId}/proposals/${proposalId}/dismiss`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ reason: "manual_workflow" }),
        },
      );
      storeProposal(requestId, data.proposal);
      await loadChangeRequests();
      return data.proposal;
    } catch (err) {
      flashError(`No pude descartar la propuesta: ${err.message || err}`);
      return null;
    } finally {
      setCrProposalBusyId(null);
    }
  }, [flashError, loadChangeRequests, storeProposal]);

  useEffect(() => {
    loadChangeRequests();
  }, [crStatusFilter, loadChangeRequests]);

  // Polling liviano (30 s) sólo del contador de pendientes — así el badge
  // en el tab/KPI se actualiza sin que el operador tenga que entrar a la
  // sección. La lista completa sigue siendo carga manual / al cambiar filtro.
  useEffect(() => {
    const iv = setInterval(() => {
      if (typeof document !== "undefined" && document.hidden) return;
      fetchJson(`${API}/admin/change-requests?status=pending&limit=1`)
        .then((data) => setCrPendingCount(data.pending_count || 0))
        .catch(() => {});
    }, 30000);
    return () => clearInterval(iv);
  }, []);

  const resolveChangeRequest = useCallback(async (id, note) => {
    setCrResolvingId(id);
    try {
      await fetchJson(`${API}/admin/change-requests/${id}/resolve`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ resolution_note: (note || "").trim() }),
      });
      await loadChangeRequests();
    } catch (err) {
      flashError(`No pude marcar como resuelto: ${err.message || err}`);
    } finally {
      setCrResolvingId(null);
    }
  }, [flashError, loadChangeRequests]);

  // Publicar la corrección en el portal. Es la acción que realmente cierra
  // un pedido de cambios: el backend detecta que el render cambió, sube la
  // versión, baja la aprobación vieja (el cliente vuelve a ver "Aprobar") y
  // marca los pedidos pendientes de esa entrega como resueltos.
  //
  // El 202 no es un error: el master de broadcast se re-transcodifica
  // asincrónicamente y publicar en esa ventana entregaría el MP4 nuevo con
  // el ProRes viejo. El backend lo encola y pide reintentar.
  const [crPublishingId, setCrPublishingId] = useState(null);
  const [crPublishNotice, setCrPublishNotice] = useState(null);

  const publishDeliveryUpdate = useCallback(async (jobId, portalId, crId) => {
    setCrPublishingId(crId ?? jobId);
    setCrPublishNotice(null);
    try {
      const data = await fetchJson(`${API}/admin/deliveries/from-job/${jobId}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ portal_id: portalId || "argentina" }),
      });
      if (data.ok === false && data.status === "preparing_prores") {
        setCrPublishNotice({
          tone: "wait",
          text: data.stale?.length
            ? "El master ProRes todavía es el corte anterior. Se está regenerando con la corrección: reintentá en un minuto."
            : "Falta preparar el master ProRes. Ya se encoló: reintentá en un minuto.",
        });
      } else if (data.content_changed) {
        setCrPublishNotice({
          tone: "ok",
          text: `Publicada la versión ${data.revision}. El cliente la ve como pendiente de aprobar${
            data.resolved_change_requests?.length
              ? ` y se cerraron ${data.resolved_change_requests.length} pedido(s)`
              : ""
          }.`,
        });
      } else {
        setCrPublishNotice({
          tone: "ok",
          text: "Reenviado. El render es el mismo que ya estaba publicado, así que la versión y la aprobación no cambian.",
        });
      }
      await loadChangeRequests();
    } catch (err) {
      flashError(`No pude publicar la actualización: ${err.message || err}`);
    } finally {
      setCrPublishingId(null);
    }
  }, [flashError, loadChangeRequests]);

  const reopenChangeRequest = useCallback(async (id) => {
    setCrResolvingId(id);
    try {
      await fetchJson(`${API}/admin/change-requests/${id}/reopen`, { method: "POST" });
      await loadChangeRequests();
    } catch (err) {
      flashError(`No pude reabrir: ${err.message || err}`);
    } finally {
      setCrResolvingId(null);
    }
  }, [flashError, loadChangeRequests]);

  return {
    // health + reaper
    health,
    stuckJobs,
    reaperRunning,
    reaperResult,
    setReaperResult,
    runReaperNow,
    // jobs
    jobs,
    jobsTotal,
    jobsLoading,
    jobsStatusFilter,
    setJobsStatusFilter,
    jobsTenantFilter,
    setJobsTenantFilter,
    jobsAutoRefresh,
    setJobsAutoRefresh,
    // change requests
    changeRequests,
    crStatusFilter,
    setCrStatusFilter,
    crPendingCount,
    crResolvedCount,
    crLoading,
    crResolvingId,
    crProposalEnabled,
    crProposalApplyEnabled,
    crProposalBusyId,
    crProposalDetails,
    generateChangeRequestProposal,
    loadChangeRequestProposal,
    adjustChangeRequestProposal,
    applyChangeRequestProposal,
    dismissChangeRequestProposal,
    resolveChangeRequest,
    reopenChangeRequest,
    crPublishingId,
    crPublishNotice,
    setCrPublishNotice,
    publishDeliveryUpdate,
  };
}
