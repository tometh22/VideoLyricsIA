// Estado y acciones de la pantalla dedicada de pedidos de cambio de UMG.
//
// Vive separado de useOperacion para que abrir "Cambios UMG" no arranque
// también los pollings de health y pipeline que esa pantalla no muestra.
import { useCallback, useEffect, useRef, useState } from "react";

import { useAdmin } from "../../AdminContext";
import { API, fetchJson } from "../../adminApi";

const ACTIVE_RENDER_STATUSES = new Set([
  "queued", "processing", "rendering", "editing", "transcribed_pending",
]);

function publicationHasPendingWork(publication) {
  return ACTIVE_RENDER_STATUSES.has(publication?.job_status)
    || (publication?.prores_pending?.length || 0) > 0;
}

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
  let hash = 2166136261;
  for (const char of signature) {
    hash ^= char.charCodeAt(0);
    hash = Math.imul(hash, 16777619);
  }
  return `change-request-${proposalId}-${baseRevision}-${(hash >>> 0).toString(16)}`;
}

export default function useChangeRequests({ initialPendingCount = 0 } = {}) {
  const { flashError } = useAdmin();
  const [changeRequests, setChangeRequests] = useState([]);
  const [crStatusFilter, setCrStatusFilter] = useState("pending");
  const [crPendingCount, setCrPendingCount] = useState(initialPendingCount);
  const [crResolvedCount, setCrResolvedCount] = useState(0);
  const [crLoading, setCrLoading] = useState(true);
  const [crResolvingId, setCrResolvingId] = useState(null);
  const [crProposalEnabled, setCrProposalEnabled] = useState(false);
  const [crProposalApplyEnabled, setCrProposalApplyEnabled] = useState(false);
  const [crProposalBusyId, setCrProposalBusyId] = useState(null);
  const [crProposalDetails, setCrProposalDetails] = useState({});
  const [crPublishingId, setCrPublishingId] = useState(null);
  const [crPublishNotice, setCrPublishNotice] = useState(null);

  const crStatusRef = useRef(crStatusFilter);
  const activeRenderIdsRef = useRef(new Set());
  crStatusRef.current = crStatusFilter;

  const loadChangeRequests = useCallback(async ({ silent = false } = {}) => {
    if (!silent) setCrLoading(true);
    try {
      const data = await fetchJson(
        `${API}/admin/change-requests?status=${crStatusRef.current}&limit=200`,
      );
      const items = data.items || [];
      const nextActive = new Set(items
        .filter((item) => publicationHasPendingWork(item.publication))
        .map((item) => item.id));
      const completed = [...activeRenderIdsRef.current].filter((id) => (
        !nextActive.has(id)
        && items.some((item) => item.id === id
          && ["done", "pending_review"].includes(item.publication?.job_status))
      ));
      activeRenderIdsRef.current = nextActive;
      setChangeRequests(items);
      if (completed.length) {
        setCrPublishNotice(current => {
          // Completion of a different request must not hide this request's
          // actionable error or preparation acknowledgement.
          if (current?.requestId != null && !completed.includes(current.requestId)) return current;
          return {
            requestId: current?.requestId ?? completed[0],
            tone: "ok",
            text: "Terminó la preparación de los archivos. Revisá el video: esto no confirma que el pedido esté corregido ni publica en el portal.",
          };
        });
      }
      setCrPendingCount(data.pending_count || 0);
      setCrResolvedCount(data.resolved_count || 0);
      setCrProposalEnabled(data.proposal_enabled === true);
      setCrProposalApplyEnabled(data.proposal_apply_enabled === true);
    } catch (err) {
      flashError(`No pude cargar los cambios: ${err.message || err}`);
    } finally {
      if (!silent) setCrLoading(false);
    }
  }, [flashError]);

  const storeProposal = useCallback((requestId, proposal) => {
    setCrProposalDetails((current) => ({ ...current, [requestId]: proposal }));
  }, []);

  const generateChangeRequestProposal = useCallback(async (requestId) => {
    setCrProposalBusyId(requestId);
    setCrPublishNotice(null);
    try {
      const data = await fetchJson(`${API}/admin/change-requests/${requestId}/proposals`, {
        method: "POST",
      });
      storeProposal(requestId, data.proposal);
      await loadChangeRequests();
      return data.proposal;
    } catch (err) {
      setCrPublishNotice({ requestId, tone: "error", text: `No pude analizar el pedido: ${err.message || err}` });
      flashError(`No pude analizar el pedido: ${err.message || err}`);
      return null;
    } finally {
      setCrProposalBusyId(null);
    }
  }, [flashError, loadChangeRequests, storeProposal]);

  const loadChangeRequestProposal = useCallback(async (requestId) => {
    setCrProposalBusyId(requestId);
    setCrPublishNotice(null);
    try {
      const data = await fetchJson(
        `${API}/admin/change-requests/${requestId}/proposals/current`,
      );
      storeProposal(requestId, data.proposal);
      return data.proposal;
    } catch (err) {
      setCrPublishNotice({ requestId, tone: "error", text: `No pude cargar la propuesta: ${err.message || err}` });
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
    setCrPublishNotice(null);
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
      setCrPublishNotice({ requestId, tone: "error", text: `No pude aplicar la propuesta: ${err.message || err}` });
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

  const regenerateBackgroundFromProposal = useCallback(async (
    requestId, proposalId, operationId, jobId, prompt, backgroundMode,
  ) => {
    setCrProposalBusyId(requestId);
    setCrPublishNotice(null);
    try {
      const nonce = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`;
      const data = await fetchJson(`${API}/edit/${jobId}`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": `cr-background-${proposalId}-${operationId}-${nonce}`,
        },
        body: JSON.stringify({
          edit_type: "background",
          background_hint: prompt.trim(),
          background_mode: backgroundMode === "imagen" ? "imagen" : "veo",
          bg_verbatim: true,
          force_content_validation: true,
        }),
      });
      setCrPublishNotice({
        tone: "wait",
        text: "La regeneración empezó. El portal conserva el corte anterior: esperá a que termine, abrí el video nuevo y publicalo sólo si quedó bien.",
      });
      await loadChangeRequests({ silent: true });
      return data;
    } catch (err) {
      flashError(`No pude regenerar el fondo: ${err.message || err}`);
      return null;
    } finally {
      setCrProposalBusyId(null);
    }
  }, [flashError, loadChangeRequests]);

  useEffect(() => {
    loadChangeRequests();
  }, [crStatusFilter, loadChangeRequests]);

  useEffect(() => {
    const iv = setInterval(() => {
      if (typeof document !== "undefined" && document.hidden) return;
      fetchJson(`${API}/admin/change-requests?status=pending&limit=1`)
        .then((data) => setCrPendingCount(data.pending_count || 0))
        .catch(() => {});
    }, 30000);
    return () => clearInterval(iv);
  }, []);

  const hasActiveWork = changeRequests.some((item) => (
    publicationHasPendingWork(item.publication)
  ));
  useEffect(() => {
    if (!hasActiveWork) return undefined;
    const iv = setInterval(() => {
      if (typeof document !== "undefined" && document.hidden) return;
      loadChangeRequests({ silent: true });
    }, 5000);
    return () => clearInterval(iv);
  }, [hasActiveWork, loadChangeRequests]);

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
          requestId: crId,
          tone: "wait",
          text: data.stale?.length
            ? "Se está actualizando el archivo profesional (.mov) con la corrección. Esperá un minuto; después vas a poder publicar."
            : "Se está preparando el archivo profesional (.mov). Esperá un minuto; después vas a poder publicar.",
        });
      } else if (data.content_changed) {
        setCrPublishNotice({
          requestId: crId,
          tone: "ok",
          text: `Publicada la versión ${data.revision}. El cliente la ve como pendiente de aprobar${
            data.resolved_change_requests?.length
              ? ` y se cerraron ${data.resolved_change_requests.length} pedido(s)`
              : ""
          }.`,
        });
      } else {
        setCrPublishNotice({
          requestId: crId,
          tone: "ok",
          text: "Reenviado. El render es el mismo que ya estaba publicado, así que la versión y la aprobación no cambian.",
        });
      }
      await loadChangeRequests();
    } catch (err) {
      setCrPublishNotice({ requestId: crId, tone: "error",
        text: `No se publicó la actualización: ${err.message || err}` });
      flashError(`No pude publicar la actualización: ${err.message || err}`);
    } finally {
      setCrPublishingId(null);
    }
  }, [flashError, loadChangeRequests]);

  const prepareProRes = useCallback(async (jobId, crId) => {
    setCrPublishingId(crId);
    setCrPublishNotice({ requestId: crId, tone: "wait", text: "Solicitando actualización del archivo profesional…" });
    try {
      // Reuse the exact saved format, never guess or replace it with defaults.
      // This action must NOT call the publication endpoint: pending_review
      // renders can prepare a master, but cannot publish without approval.
      const job = await fetchJson(`${API}/status/${jobId}`);
      const spec = job.umg_spec;
      if (!spec?.frame_size || spec.fps == null || spec.prores_profile == null) {
        throw new Error("Falta el formato del master. Volvé a cargar el pedido y elegí el formato.");
      }
      const data = await fetchJson(`${API}/enable-prores/${jobId}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ umg_frame_size: spec.frame_size,
          umg_fps: String(spec.fps), umg_prores_profile: String(spec.prores_profile) }),
      });
      if (data.ok !== true || !data.enqueued?.length) {
        throw new Error("El servidor no confirmó la actualización. Podés reintentar.");
      }
      setCrPublishNotice({ requestId: crId, tone: "wait",
        text: "Actualización del .mov encolada. Esta pantalla comprobará cuándo esté listo. No se publicó ni se aprobó el video." });
      await loadChangeRequests({ silent: true });
    } catch (err) {
      setCrPublishNotice({ requestId: crId, tone: "error",
        text: `No se pudo actualizar el archivo profesional: ${err.message || err}` });
    } finally {
      setCrPublishingId(null);
    }
  }, [loadChangeRequests]);

  const handleProResConfigured = useCallback(async (requestId) => {
    setCrPublishNotice({
      requestId,
      tone: "wait",
      text: "Formato guardado. Se está generando el .mov del último render; esto no aplica cambios de letra pendientes ni publica en el portal.",
    });
    await loadChangeRequests({ silent: true });
  }, [loadChangeRequests]);

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
    regenerateBackgroundFromProposal,
    resolveChangeRequest,
    reopenChangeRequest,
    crPublishingId,
    crPublishNotice,
    setCrPublishNotice,
    publishDeliveryUpdate,
    prepareProRes,
    handleProResConfigured,
  };
}
