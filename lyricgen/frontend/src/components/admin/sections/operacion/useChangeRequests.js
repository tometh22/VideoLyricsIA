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

async function changeRequestIdempotencyKey(proposalId, baseRevision, operationIds, contentHash = "") {
  const signature = JSON.stringify([proposalId, baseRevision, [...operationIds].sort(), contentHash]);
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

function mutationOutcomeUnknown(error) {
  // No response, proxy/server failure, or request timeout can occur after a
  // successful commit. A received validation/permission/conflict response is
  // different; only the server can confirm the operation's actual outcome.
  return !error?.status || error.status >= 500 || [408, 502, 504].includes(error.status);
}

function proposalMatchesSummary(proposal, item) {
  if (!proposal) return false;
  const summary = item.proposal;
  if (Object.prototype.hasOwnProperty.call(item, "proposal") && !summary) return false;
  if (summary && (summary.id !== proposal.id || summary.status !== proposal.status
    || (summary.content_hash && summary.content_hash !== proposal.content_hash))) return false;
  const revision = item.publication?.editor_revision;
  return revision == null || proposal.lyrics_context?.revision == null
    || revision === proposal.lyrics_context.revision;
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
  const [crRenderReview, setCrRenderReview] = useState(null);
  const renderReviewRef = useRef(crRenderReview);
  renderReviewRef.current = crRenderReview;
  const renderLockRef = useRef(false);
  const listGenerationRef = useRef(0);
  const proposalGenerationRef = useRef(new Map());
  const proposalFactsRef = useRef(new Map());
  const mutationLocksRef = useRef(new Set());
  const reviewGenerationRef = useRef(0);
  const proposalWorkRef = useRef(new Map());
  const proposalDetailsRef = useRef(crProposalDetails);
  proposalDetailsRef.current = crProposalDetails;
  const requestItemsRef = useRef(changeRequests);
  requestItemsRef.current = changeRequests;
  const beginProposalWork = useCallback((requestId) => {
    const token = Symbol();
    proposalWorkRef.current.set(token, requestId);
    setCrProposalBusyId(requestId);
    return token;
  }, []);
  const endProposalWork = useCallback((token) => {
    proposalWorkRef.current.delete(token);
    const pending = [...proposalWorkRef.current.values()];
    setCrProposalBusyId(pending.length ? pending[pending.length - 1] : null);
  }, []);

  const crStatusRef = useRef(crStatusFilter);
  const activeRenderIdsRef = useRef(new Set());
  crStatusRef.current = crStatusFilter;

  const loadChangeRequests = useCallback(async ({ silent = false } = {}) => {
    const generation = ++listGenerationRef.current;
    const requestedFilter = crStatusRef.current;
    if (!silent) setCrLoading(true);
    try {
      const data = await fetchJson(
        `${API}/admin/change-requests?status=${requestedFilter}&limit=200`,
      );
      if (generation !== listGenerationRef.current || requestedFilter !== crStatusRef.current) return;
      const items = data.items || [];
      for (const item of items) {
        const id = String(item.id);
        const facts = JSON.stringify([item.proposal?.id, item.proposal?.status,
          item.proposal?.content_hash, item.publication?.editor_revision]);
        const previous = proposalFactsRef.current.get(id);
        if (previous != null && previous !== facts) {
          proposalGenerationRef.current.set(id, (proposalGenerationRef.current.get(id) || 0) + 1);
        }
        proposalFactsRef.current.set(id, facts);
      }
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
      setCrProposalDetails(current => Object.fromEntries(Object.entries(current).filter(([id, proposal]) => {
        const item = items.find(row => String(row.id) === id);
        return !item || proposalMatchesSummary(proposal, item);
      })));
      if (completed.length) {
        setCrPublishNotice(current => {
          // Completion of a different request must not hide this request's
          // actionable error or preparation acknowledgement.
          if (current?.outcomeUnknown || (current?.requestId != null && !completed.includes(current.requestId))) return current;
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
      if (generation === listGenerationRef.current && requestedFilter === crStatusRef.current) {
        flashError(`No pude cargar los cambios: ${err.message || err}`);
      }
    } finally {
      if (generation === listGenerationRef.current && requestedFilter === crStatusRef.current) setCrLoading(false);
    }
  }, [flashError]);

  const startProposalRequest = useCallback((requestId) => {
    const id = String(requestId);
    const generation = (proposalGenerationRef.current.get(id) || 0) + 1;
    proposalGenerationRef.current.set(id, generation);
    return generation;
  }, []);

  const storeProposal = useCallback((requestId, proposal, generation) => {
    if (generation != null && proposalGenerationRef.current.get(String(requestId)) !== generation) return;
    setCrProposalDetails((current) => ({ ...current, [requestId]: proposal }));
  }, []);

  const generateChangeRequestProposal = useCallback(async (requestId) => {
    const generation = startProposalRequest(requestId);
    const work = beginProposalWork(requestId);
    setCrPublishNotice(null);
    try {
      const data = await fetchJson(`${API}/admin/change-requests/${requestId}/proposals`, {
        method: "POST",
      });
      storeProposal(requestId, data.proposal, generation);
      await loadChangeRequests();
      return data.proposal;
    } catch (err) {
      setCrPublishNotice({ requestId, tone: "error", text: `No pude analizar el pedido: ${err.message || err}` });
      flashError(`No pude analizar el pedido: ${err.message || err}`);
      return null;
    } finally {
      endProposalWork(work);
    }
  }, [flashError, loadChangeRequests, storeProposal, startProposalRequest, beginProposalWork, endProposalWork]);

  const loadChangeRequestProposal = useCallback(async (requestId) => {
    const generation = startProposalRequest(requestId);
    const work = beginProposalWork(requestId);
    setCrPublishNotice(null);
    try {
      const data = await fetchJson(
        `${API}/admin/change-requests/${requestId}/proposals/current`,
      );
      storeProposal(requestId, data.proposal, generation);
      await loadChangeRequests({ silent: true });
      return data.proposal;
    } catch (err) {
      setCrPublishNotice({ requestId, tone: "error", text: `No pude cargar la propuesta: ${err.message || err}` });
      flashError(`No pude cargar la propuesta: ${err.message || err}`);
      return null;
    } finally {
      endProposalWork(work);
    }
  }, [flashError, storeProposal, startProposalRequest, loadChangeRequests, beginProposalWork, endProposalWork]);

  const adjustChangeRequestProposal = useCallback(async (
    requestId, proposalId, operationId, requestedText, baseRevision, expectedProposalHash,
  ) => {
    if (!expectedProposalHash) {
      setCrPublishNotice({ requestId, tone: "error", text: "Actualizá la propuesta antes de guardar ajustes; falta su versión verificada." });
      return null;
    }
    const generation = startProposalRequest(requestId);
    const work = beginProposalWork(requestId);
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
            expected_proposal_hash: expectedProposalHash,
          }),
        },
      );
      storeProposal(requestId, data.proposal, generation);
      await loadChangeRequests({ silent: true });
      return data.proposal;
    } catch (err) {
      if (err.status === 409) {
        await loadChangeRequestProposal(requestId);
        setCrPublishNotice({ requestId, tone: "error", text: "La propuesta cambió. Recargamos la comparación; revisala antes de volver a guardar o aplicar." });
      } else {
        setCrPublishNotice({ requestId, outcomeUnknown: mutationOutcomeUnknown(err), tone: mutationOutcomeUnknown(err) ? "wait" : "error",
          text: mutationOutcomeUnknown(err) ? "No pudimos confirmar si se guardó el ajuste. Actualizá la comparación antes de reintentar."
            : `No pude ajustar la propuesta: ${err.message || err}` });
      }
      return null;
    } finally {
      endProposalWork(work);
    }
  }, [storeProposal, startProposalRequest, loadChangeRequestProposal, loadChangeRequests, beginProposalWork, endProposalWork]);

  const applyChangeRequestProposal = useCallback(async (
    requestId, proposalId, operationIds, baseRevision, expectedProposalHash,
  ) => {
    if (!expectedProposalHash) {
      setCrPublishNotice({ requestId, tone: "error", text: "Actualizá la propuesta antes de aplicar; falta su versión verificada." });
      return null;
    }
    const lock = `apply:${requestId}`;
    if (mutationLocksRef.current.has(lock)) return null;
    mutationLocksRef.current.add(lock);
    const generation = startProposalRequest(requestId);
    const work = beginProposalWork(requestId);
    setCrPublishNotice(null);
    try {
      const idempotencyKey = await changeRequestIdempotencyKey(
        proposalId, baseRevision, operationIds, expectedProposalHash,
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
            expected_proposal_hash: expectedProposalHash,
          }),
        },
      );
      if (data.ok !== true || !Number.isInteger(data.revision) || data.proposal?.id !== proposalId
        || (typeof data.applied !== "boolean" && data.idempotent !== true)) {
        throw new Error("El servidor no confirmó una revisión guardada verificable.");
      }
      const replay = data.idempotent === true || data.applied === false;
      const latest = replay ? await loadChangeRequestProposal(requestId) : null;
      if (!replay) storeProposal(requestId, { ...data.proposal, editor_url: data.editor_url }, generation);
      setCrPublishNotice({
        requestId,
        tone: replay && !latest ? "wait" : "ok",
        text: replay
          ? `La aplicación ya se registró en la revisión ${data.revision}. ${latest ? "Consultamos la comparación actual" : "No pudimos consultar la comparación actual"}: este recibo histórico no confirma que siga igual después de otras ediciones.`
          : "La corrección quedó guardada. Revisá la letra y confirmá el render desde acá o desde el editor; todavía no se publicó.",
      });
      await loadChangeRequests();
      return data;
    } catch (err) {
      if (err.status === 409) {
        await loadChangeRequestProposal(requestId);
        setCrPublishNotice({ requestId, tone: "error", text: "La letra o la propuesta cambió. Recargamos la comparación; revisala antes de volver a aplicar." });
      } else {
        setCrPublishNotice({ requestId, outcomeUnknown: mutationOutcomeUnknown(err), tone: mutationOutcomeUnknown(err) ? "wait" : "error", text: mutationOutcomeUnknown(err)
          ? "No pudimos confirmar la aplicación. Actualizá la comparación antes de reintentar; el cambio podría haberse guardado."
          : `No pude aplicar la propuesta: ${err.message || err}` });
        await loadChangeRequests({ silent: true });
      }
      return null;
    } finally {
      mutationLocksRef.current.delete(lock);
      endProposalWork(work);
    }
  }, [loadChangeRequests, storeProposal, startProposalRequest, loadChangeRequestProposal, beginProposalWork, endProposalWork]);

  const dismissChangeRequestProposal = useCallback(async (requestId, proposalId) => {
    const generation = startProposalRequest(requestId);
    const work = beginProposalWork(requestId);
    try {
      const data = await fetchJson(
        `${API}/admin/change-requests/${requestId}/proposals/${proposalId}/dismiss`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ reason: "manual_workflow" }),
        },
      );
      storeProposal(requestId, data.proposal, generation);
      await loadChangeRequests();
      return data.proposal;
    } catch (err) {
      flashError(`No pude descartar la propuesta: ${err.message || err}`);
      return null;
    } finally {
      endProposalWork(work);
    }
  }, [flashError, loadChangeRequests, storeProposal, startProposalRequest, beginProposalWork, endProposalWork]);

  const regenerateBackgroundFromProposal = useCallback(async (
    requestId, proposalId, operationId, jobId, prompt, backgroundMode, expectedProposalHash,
  ) => {
    const proposal = proposalDetailsRef.current[requestId];
    const operation = proposal?.operations?.find(row => row.id === operationId);
    const item = requestItemsRef.current.find(row => row.id === requestId);
    if (!expectedProposalHash || proposal?.content_hash !== expectedProposalHash
      || proposal?.id !== proposalId || proposal?.job_id !== jobId
      || !["ready", "partial"].includes(proposal?.status)
      || operation?.regeneration_supported !== true
      || proposal?.lyrics_context?.matches_base !== true
      || !Number.isInteger(proposal?.lyrics_context?.revision)
      || proposal.lyrics_context.revision !== proposal.base_revision
      || (item?.workflow?.allowed_actions && !item.workflow.allowed_actions.includes("review_proposal"))) {
      setCrPublishNotice({ requestId, tone: "error", text: "Actualizá y revisá la propuesta de fondo antes de regenerar; falta una comparación vigente y aplicable." });
      return null;
    }
    const lock = `background:${requestId}`;
    if (mutationLocksRef.current.has(lock)) return null;
    mutationLocksRef.current.add(lock);
    const work = beginProposalWork(requestId);
    setCrPublishNotice(null);
    try {
      // The same reviewed proposal + parameters is the same paid intention,
      // including after reload or in another tab. A retry must not buy a new
      // generation merely because its first response was lost.
      const key = await changeRequestIdempotencyKey(`background-${proposalId}`, operationId,
        [JSON.stringify([jobId, prompt.trim(), backgroundMode === "imagen" ? "imagen" : "veo"])], expectedProposalHash);
      const data = await fetchJson(`${API}/edit/${jobId}`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Idempotency-Key": key,
        },
        body: JSON.stringify({
          edit_type: "background",
          background_hint: prompt.trim(),
          background_mode: backgroundMode === "imagen" ? "imagen" : "veo",
          bg_verbatim: true,
          force_content_validation: true,
          expected_proposal_hash: expectedProposalHash,
          change_request_id: requestId,
          change_request_proposal_id: proposalId,
          change_request_operation_id: operationId,
          editor_revision: proposal.lyrics_context.revision,
        }),
      });
      if (data.ok !== true && !["editing", "queued", "processing", "rendering"].includes(data.status)) {
        throw new Error("El servidor no confirmó una operación de fondo verificable.");
      }
      setCrPublishNotice({
        requestId,
        tone: "wait",
        text: data.deduplicated
          ? "Este pedido de fondo ya fue recibido. Consultá su resultado antes de solicitar otra opción; todavía no se publicó."
          : "La regeneración empezó. El portal conserva el corte anterior: esperá a que termine, abrí el video nuevo y publicalo sólo si quedó bien.",
      });
      await loadChangeRequests({ silent: true });
      return data;
    } catch (err) {
      setCrPublishNotice({ requestId, outcomeUnknown: mutationOutcomeUnknown(err), tone: mutationOutcomeUnknown(err) ? "wait" : "error",
        text: mutationOutcomeUnknown(err)
          ? "No pudimos confirmar la regeneración. Consultá el estado; reintentar este mismo pedido conserva la intención y no solicita otra opción."
          : `No pude regenerar el fondo: ${err.message || err}` });
      await loadChangeRequests({ silent: true });
      return null;
    } finally {
      mutationLocksRef.current.delete(lock);
      endProposalWork(work);
    }
  }, [loadChangeRequests, beginProposalWork, endProposalWork]);

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
    const reason = (note || "").trim();
    if (!reason) {
      setCrPublishNotice({ requestId: id, tone: "error", text: "Escribí el motivo para cerrar el pedido sin publicar otro video." });
      return;
    }
    const lock = `resolve:${id}`;
    if (mutationLocksRef.current.has(lock)) return;
    mutationLocksRef.current.add(lock);
    setCrResolvingId(id);
    try {
      const data = await fetchJson(`${API}/admin/change-requests/${id}/resolve`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ resolution_note: reason }),
      });
      if (data.ok !== true || (data.already_resolved !== true
        && (typeof data.resolved_at !== "string" || !Number.isFinite(Date.parse(data.resolved_at))))) {
        throw new Error("El servidor no confirmó el cierre del pedido.");
      }
      setCrPublishNotice({ requestId: id, tone: "ok", text: data.already_resolved
        ? "El pedido ya figura cerrado en el registro. Este resultado no confirma una nueva publicación ni la revisión del contenido."
        : "Pedido cerrado manualmente en el registro. No se generó ni publicó otro video; el cierre no acredita la revisión del contenido." });
      await loadChangeRequests();
    } catch (err) {
      setCrPublishNotice({ requestId: id, outcomeUnknown: mutationOutcomeUnknown(err), tone: mutationOutcomeUnknown(err) ? "wait" : "error",
        text: mutationOutcomeUnknown(err) ? "No pudimos confirmar el cierre. Actualizando el estado del pedido; no publiques otra versión para reintentar."
          : `No pude marcar como resuelto: ${err.message || err}` });
      await loadChangeRequests({ silent: true });
    } finally {
      mutationLocksRef.current.delete(lock);
      setCrResolvingId(current => current === id ? null : current);
    }
  }, [loadChangeRequests]);

  const reviewForRender = useCallback(async (requestId) => {
    const generation = ++reviewGenerationRef.current;
    const work = beginProposalWork(requestId);
    try {
      const review = await fetchJson(`${API}/admin/change-requests/${requestId}/review`);
      if (generation === reviewGenerationRef.current) setCrRenderReview(review);
    } catch (err) {
      setCrPublishNotice({ requestId, tone: "error", text: `No pude abrir la revisión: ${err.message || err}` });
    } finally { endProposalWork(work); }
  }, [beginProposalWork, endProposalWork]);

  const closeRenderReview = useCallback((value) => {
    reviewGenerationRef.current += 1;
    setCrRenderReview(value);
  }, []);

  const confirmRender = useCallback(async () => {
    if (!crRenderReview || renderLockRef.current) return;
    const requestId = crRenderReview.change_request_id;
    renderLockRef.current = true;
    const work = beginProposalWork(requestId);
    try {
      const data = await fetchJson(`${API}/admin/change-requests/${requestId}/render`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ editor_revision: crRenderReview.editor_revision }),
      });
      if (!["done", "pending_review", "editing", "queued", "processing", "rendering"].includes(data.status)) {
        throw new Error("El servidor no confirmó el estado del render.");
      }
      if (renderReviewRef.current === crRenderReview) setCrRenderReview(null);
      setCrPublishNotice({ requestId, tone: "ok", text: ["done", "pending_review"].includes(data.status)
        ? "Esta revisión ya tiene un corte generado. Revisalo antes de publicar."
        : "Letra aprobada. Generando el corte nuevo; todavía no se publicó en el portal." });
      await loadChangeRequests();
    } catch (err) {
      if (renderReviewRef.current === crRenderReview) setCrRenderReview(null);
      setCrPublishNotice({ requestId, outcomeUnknown: mutationOutcomeUnknown(err), tone: mutationOutcomeUnknown(err) ? "wait" : "error", text: mutationOutcomeUnknown(err)
        ? "No pudimos confirmar el inicio del render. Actualizando el estado; no generes otro corte hasta comprobarlo."
        : `El servidor no aceptó el render: ${err.message || err}. Volvé a revisar la letra guardada.` });
      await loadChangeRequests({ silent: true });
    } finally { renderLockRef.current = false; endProposalWork(work); }
  }, [crRenderReview, loadChangeRequests, beginProposalWork, endProposalWork]);

  const publishDeliveryUpdate = useCallback(async (jobId, portalId, crId, publication) => {
    if (!portalId || !publication?.render_fingerprint || !Number.isInteger(publication.editor_revision)) {
      setCrPublishNotice({ requestId: crId, tone: "error", text: "Falta verificar el destino o la revisión del corte. Actualizá el pedido antes de publicar." });
      return;
    }
    const lock = `publish:${crId ?? jobId}`;
    if (mutationLocksRef.current.has(lock)) return;
    mutationLocksRef.current.add(lock);
    setCrPublishingId(crId ?? jobId);
    setCrPublishNotice(null);
    try {
      const data = await fetchJson(`${API}/admin/deliveries/from-job/${jobId}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ portal_id: portalId,
          ...(publication ? { change_request_id: crId,
            reviewed_render_fingerprint: publication.render_fingerprint,
            reviewed_editor_revision: publication.editor_revision } : {}),
        }),
      });
      if (data.ok === false && data.status === "preparing_prores") {
        setCrPublishNotice({
          requestId: crId,
          tone: "wait",
          text: data.stale?.length
            ? "Se está actualizando el archivo profesional (.mov) con la corrección. Esperá un minuto; después vas a poder publicar."
            : "Se está preparando el archivo profesional (.mov). Esperá un minuto; después vas a poder publicar.",
        });
      } else if (data.ok !== true || typeof data.content_changed !== "boolean"
        || !Number.isInteger(data.revision) || data.revision < 1
        || (data.portal_id && data.portal_id !== portalId)
        || (data.job_id && data.job_id !== jobId)) {
        throw new Error("El servidor no confirmó la identidad de la publicación.");
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
      setCrPublishNotice({ requestId: crId, outcomeUnknown: mutationOutcomeUnknown(err), tone: mutationOutcomeUnknown(err) ? "wait" : "error",
        text: mutationOutcomeUnknown(err)
          ? "No pudimos confirmar el resultado de la publicación. Actualizando el estado: podría haberse publicado. Verificá la versión antes de reintentar."
          : `El servidor no aceptó la publicación: ${err.message || err}` });
      await loadChangeRequests({ silent: true });
    } finally {
      mutationLocksRef.current.delete(lock);
      setCrPublishingId(current => current === (crId ?? jobId) ? null : current);
    }
  }, [loadChangeRequests]);

  const prepareProRes = useCallback(async (jobId, crId) => {
    const lock = `publish:${crId ?? jobId}`;
    if (mutationLocksRef.current.has(lock)) return;
    mutationLocksRef.current.add(lock);
    setCrPublishingId(crId);
    setCrPublishNotice({ requestId: crId, tone: "wait", text: "Solicitando actualización del archivo profesional…" });
    let submitted = false;
    let acknowledged = false;
    try {
      // Reuse the exact saved format, never guess or replace it with defaults.
      // This action must NOT call the publication endpoint: pending_review
      // renders can prepare a master, but cannot publish without approval.
      const job = await fetchJson(`${API}/status/${jobId}`);
      const spec = job.umg_spec;
      if (!spec?.frame_size || spec.fps == null || spec.prores_profile == null) {
        throw new Error("Falta el formato del master. Volvé a cargar el pedido y elegí el formato.");
      }
      submitted = true;
      const data = await fetchJson(`${API}/enable-prores/${jobId}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ umg_frame_size: spec.frame_size,
          umg_fps: String(spec.fps), umg_prores_profile: String(spec.prores_profile) }),
      });
      if (data.ok !== true || !Array.isArray(data.enqueued)) {
        throw new Error("El servidor no confirmó el resultado de la actualización.");
      }
      acknowledged = true;
      if (!data.enqueued.length) {
        throw new Error("El servidor no confirmó la actualización. Podés reintentar.");
      }
      setCrPublishNotice({ requestId: crId, tone: "wait",
        text: "Actualización del .mov encolada. Esta pantalla comprobará cuándo esté listo. No se publicó ni se aprobó el video." });
      await loadChangeRequests({ silent: true });
    } catch (err) {
      const unknown = submitted && !acknowledged && mutationOutcomeUnknown(err);
      setCrPublishNotice({ requestId: crId, outcomeUnknown: unknown, tone: unknown ? "wait" : "error",
        text: unknown ? "No pudimos confirmar la preparación del archivo profesional. Actualizando el estado antes de reintentar."
          : `No se pudo actualizar el archivo profesional: ${err.message || err}` });
      if (unknown) await loadChangeRequests({ silent: true });
    } finally {
      mutationLocksRef.current.delete(lock);
      setCrPublishingId(current => current === crId ? null : current);
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
      setCrPublishNotice({ requestId: id, outcomeUnknown: mutationOutcomeUnknown(err), tone: mutationOutcomeUnknown(err) ? "wait" : "error",
        text: mutationOutcomeUnknown(err) ? "No pudimos confirmar la reapertura. Actualizando el estado del pedido."
          : `No pude reabrir: ${err.message || err}` });
      await loadChangeRequests({ silent: true });
    } finally {
      setCrResolvingId(null);
    }
  }, [loadChangeRequests]);

  return {
    refreshChangeRequests: loadChangeRequests,
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
    reviewForRender, confirmRender, crRenderReview, setCrRenderReview: closeRenderReview,
    prepareProRes,
    handleProResConfigured,
  };
}
