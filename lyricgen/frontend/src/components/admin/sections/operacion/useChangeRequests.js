// Estado y acciones de la pantalla dedicada de pedidos de cambio de UMG.
//
// Vive separado de useOperacion para que abrir "Cambios UMG" no arranque
// también los pollings de health y pipeline que esa pantalla no muestra.
import { useCallback, useEffect, useRef, useState } from "react";

import { useAdmin } from "../../AdminContext";
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
