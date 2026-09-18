export const PORTAL_LABELS = {
  argentina: "UMG Argentina",
  chile: "UMG Chile",
};

export const WORKFLOW_STEPS = [
  { id: "analyze", label: "Interpretar" },
  { id: "apply", label: "Aplicar" },
  { id: "render", label: "Renderizar" },
  { id: "review", label: "Revisar" },
  { id: "publish", label: "Publicar" },
];

const BUSY_JOB_STATUSES = new Set([
  "queued", "processing", "rendering", "editing", "transcribed_pending",
]);

const PROPOSAL_REVIEW_STATUSES = new Set(["ready", "partial", "needs_input"]);
const PROPOSAL_APPLIED_STATUSES = new Set(["applied", "partially_applied"]);

export function proposalForRequest(item, loadedProposal) {
  return loadedProposal || item?.proposal || null;
}

export function requestWorkflow(item, loadedProposal, proposalEnabled = true) {
  if (!item) {
    return {
      key: "empty", activeStep: 0, label: "Sin pedido seleccionado",
      detail: "Elegí un pedido de la cola.", tone: "idle",
    };
  }
  if (item.resolved_at) {
    return {
      key: "resolved", activeStep: WORKFLOW_STEPS.length,
      label: "Pedido resuelto", detail: "La corrección ya fue cerrada.", tone: "done",
    };
  }

  const publication = item.publication || {};
  if (BUSY_JOB_STATUSES.has(publication.job_status)) {
    return {
      key: "rendering", activeStep: 2, label: "Generando corte nuevo",
      detail: "El portal conserva la versión anterior hasta que revises y publiques.",
      tone: "busy",
    };
  }
  if (publication.render_matches_editor === false && publication.can_render) {
    const proposal = proposalForRequest(item, loadedProposal);
    if (["applied", "partially_applied"].includes(proposal?.status) || !proposal) {
      return { key: proposal ? "render" : "analyze", activeStep: proposal ? 2 : 0,
        label: proposal ? "Revisar y confirmar el render" : "Pendiente de interpretar",
        detail: "Aplicá la propuesta o editá a mano. Después revisá la letra y confirmá el render.", tone: "action" };
    }
  }
  if (publication.render_matches_editor && publication.needs_publish && !(publication.prores_pending || []).length) {
    return { key: "publish", activeStep: 3, label: "Corte listo · revisar y publicar",
      detail: "Reproducí el video y confirmá Publicar actualización para actualizar el portal.", tone: "attention" };
  }
  // A missing/old master says nothing about whether this client's request was
  // interpreted. Do not paint those stages complete or hide the analyze action.
  if (proposalEnabled && !proposalForRequest(item, loadedProposal)) {
    return {
      key: "analyze", activeStep: 0, label: "Pendiente de interpretar",
      detail: "Analizá el pedido primero. El estado del archivo profesional no confirma estas correcciones.",
      tone: "action",
    };
  }
  if ((publication.prores_pending || []).length) {
    return {
      key: "publish", activeStep: 4, label: "Archivo profesional pendiente",
      detail: publication.prores_configured === false
        ? "Falta elegir el formato del master antes de publicar."
        : "Actualizá el master profesional para completar la publicación.",
      tone: "attention",
    };
  }
  if (publication.needs_publish) {
    return {
      key: "publish", activeStep: 4, label: "Listo para publicar",
      detail: "Revisá el corte nuevo y publicalo cuando esté correcto.", tone: "attention",
    };
  }
  if (publication.render_matches_editor) {
    return { key: "review", activeStep: 3, label: "Corte generado",
      detail: "El portal ya tiene este corte. Si el pedido está atendido, marcalo como resuelto.", tone: "done" };
  }

  const proposal = proposalForRequest(item, loadedProposal);
  const proposalStatus = proposal?.status;
  if (PROPOSAL_APPLIED_STATUSES.has(proposalStatus)) {
    return {
      key: "render", activeStep: 2, label: "Letra guardada · falta renderizar",
      detail: "Verificá la letra guardada y generá el corte. El video todavía puede ser el anterior.", tone: "action",
    };
  }
  if (PROPOSAL_REVIEW_STATUSES.has(proposalStatus)) {
    return {
      key: "apply", activeStep: 1,
      label: proposalStatus === "needs_input" ? "Requiere revisión manual" : "Propuesta lista",
      detail: "Compará el antes y después y elegí qué cambios aplicar.",
      tone: proposalStatus === "needs_input" ? "attention" : "action",
    };
  }
  if (proposalStatus === "stale" || proposalStatus === "dismissed") {
    return {
      key: "analyze", activeStep: 0, label: "Hay que recalcular",
      detail: "El pedido o la letra cambió desde el último análisis.", tone: "attention",
    };
  }
  if (proposal) {
    return {
      key: "apply", activeStep: 1, label: "Análisis disponible",
      detail: "Abrí la propuesta para revisar los cambios detectados.", tone: "action",
    };
  }
  if (proposalEnabled) {
    return {
      key: "analyze", activeStep: 0, label: "Pendiente de interpretar",
      detail: "Convertí el comentario del cliente en cambios revisables.", tone: "action",
    };
  }
  return {
    key: "manual", activeStep: 1, label: "Revisión manual",
    detail: "Abrí el editor para atender este pedido.", tone: "attention",
  };
}

export function requestKind(item, loadedProposal) {
  const proposal = proposalForRequest(item, loadedProposal);
  const operations = proposal?.operations || [];
  if (operations.some((operation) => operation.visual_action === "regenerate_background"
    || operation.kind === "background_review")) return "Fondo";
  if (operations.some((operation) => operation.kind === "timing_review")) return "Timing";
  if (operations.some((operation) => operation.kind === "structure_review"
    || operation.kind === "merge_phrase")) return "Estructura";
  if (operations.some((operation) => operation.applicable)) return "Letra";
  if (proposal?.visual_action_count) return "Fondo";

  const comment = String(item?.comment || "").toLowerCase();
  if (/fondo|background|escena|imagen|video ia/.test(comment)) return "Fondo";
  if (/timing|tiempo|entra|sale|segundo|sincron/.test(comment)) return "Timing";
  if (/línea|linea|frase completa|pantalla/.test(comment)) return "Estructura";
  return "Letra";
}

export function requestSearchText(item) {
  const delivery = item?.delivery || {};
  return [
    delivery.artist, delivery.song, delivery.label, delivery.job_id,
    delivery.portal_id, delivery.tenant, delivery.owner_email,
    delivery.owner_username, item?.comment,
  ].filter(Boolean).join(" ").toLocaleLowerCase("es");
}

export function workflowMatchesFilter(workflow, filter) {
  if (!filter || filter === "all") return true;
  if (filter === "action") {
    return ["analyze", "apply", "manual", "render"].includes(workflow.key);
  }
  if (filter === "review") return workflow.key === "publish";
  return workflow.key === filter;
}

export function workflowCounts(items, proposals, proposalEnabled) {
  const counts = { all: items.length, action: 0, rendering: 0, review: 0 };
  items.forEach((item) => {
    const workflow = requestWorkflow(item, proposals?.[item.id], proposalEnabled);
    if (["analyze", "apply", "manual", "render"].includes(workflow.key)) counts.action += 1;
    if (workflow.key === "rendering") counts.rendering += 1;
    if (workflow.key === "publish") counts.review += 1;
  });
  return counts;
}

export function isBusyJobStatus(status) {
  return BUSY_JOB_STATUSES.has(status);
}
