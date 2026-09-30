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
  if (loadedProposal && item?.proposal?.content_hash
    && loadedProposal.content_hash !== item.proposal.content_hash) return item.proposal;
  return loadedProposal || item?.proposal || null;
}

export function requestWorkflow(item, loadedProposal, proposalEnabled = true) {
  if (!item) {
    return {
      key: "empty", activeStep: 0, label: "Sin pedido seleccionado",
      detail: "Elegí un pedido de la cola.", tone: "idle",
    };
  }
  if (item.workflow && Array.isArray(item.workflow.allowed_actions)) {
    if (!loadedProposal?.content_hash || loadedProposal.content_hash === item.proposal?.content_hash) {
      return item.workflow;
    }
    return { key: "refresh", activeStep: 0, label: "Comparación desactualizada",
      detail: "La propuesta cambió. Actualizá el estado y revisá la nueva comparación antes de continuar.",
      tone: "attention", allowed_actions: ["refresh"] };
  }
  if (item.resolved_at) {
    const published = item.resolution_source === "publication";
    return {
      key: "resolved", activeStep: published ? WORKFLOW_STEPS.length : -1,
      label: published ? "Resuelto al publicar" : "Cerrado manualmente",
      detail: published ? "El pedido se cerró al registrar su publicación."
        : "Este cierre no confirma que se haya publicado un video nuevo.", tone: "done",
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
      detail: "Revisá la publicación registrada y el pedido. Cerrarlo manualmente requiere indicar el motivo.", tone: "attention" };
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
  if (proposalStatus === "interpreting") {
    return {
      key: "apply", activeStep: 1, label: "Interpretando el pedido…",
      detail: "Ubicando cada cambio en la letra. En un minuto queda para revisar.", tone: "busy",
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
  if (filter === "review") return ["publish", "review"].includes(workflow.key);
  return workflow.key === filter;
}

export function workflowCounts(items, proposals, proposalEnabled) {
  const counts = { all: items.length, action: 0, rendering: 0, review: 0 };
  items.forEach((item) => {
    const workflow = requestWorkflow(item, proposals?.[item.id], proposalEnabled);
    if (["analyze", "apply", "manual", "render"].includes(workflow.key)) counts.action += 1;
    if (workflow.key === "rendering") counts.rendering += 1;
    if (["publish", "review"].includes(workflow.key)) counts.review += 1;
  });
  return counts;
}

export function isBusyJobStatus(status) {
  return BUSY_JOB_STATUSES.has(status);
}

// ---------------------------------------------------------------------------
// Vista de 3 pasos para el operador.
//
// El servidor sigue proyectando 5 etapas (Interpretar, Aplicar, Renderizar,
// Revisar, Publicar). Acá sólo se traducen a lo que el operador hace de
// verdad: corregir, generar el video nuevo y publicar. La interpretación con
// IA es una ayuda opcional dentro de "Corregir", no un paso. Nada de esto
// cambia el contrato `workflow` del servidor ni sus `allowed_actions`.
// ---------------------------------------------------------------------------

export const CORRECTION_STEPS = [
  { key: "correct", label: "Corregir" },
  { key: "render", label: "Generar el video nuevo" },
  { key: "publish", label: "Publicar" },
];

export const PRIMARY_LABELS = {
  edit: "Corregir en el editor",
  review_proposal: "Revisar los cambios sugeridos",
  render: "Generar el video corregido",
  rendering: "Generando el video nuevo…",
  publish: "Publicar en el portal y dar por resuelto",
  publishing: "Publicando…",
  prepare_master: "Preparar el archivo profesional",
  prepare_master_setup: "Elegir formato y preparar el archivo profesional",
  preparing_master: "Preparando el archivo…",
  reopen: "Reabrir pedido",
  reopening: "Reabriendo…",
  see_error: "Ver el error",
  close: "Dar por resuelto",
  refresh: "Actualizar estado",
  none: "Sin acción disponible",
};

export const SECONDARY_LABELS = {
  suggest: "Pedir sugerencia a la IA",
  suggesting: "Analizando…",
  edit: "Editar letra a mano",
  close: "Cerrar sin publicar",
};

/** Un `action` del servidor está permitido; sin lista (datos viejos) todo lo está. */
export function workflowAllows(workflow, action) {
  return !Array.isArray(workflow?.allowed_actions) || workflow.allowed_actions.includes(action);
}

// Posición en los 3 pasos: 0..2 = paso activo, 3 = todo hecho, -1 = nada hecho.
function stepPosition(workflow) {
  switch (workflow?.key) {
    case "resolved":
      // El servidor manda activeStep >= 3 si se resolvió al publicar. La
      // bandeja de campaña no manda activeStep: su "resolved" siempre publicó.
      return workflow.activeStep == null || workflow.activeStep >= 3 ? 3 : -1;
    case "closed": return -1;
    case "review": return 3;
    case "publish": return 2;
    case "render": case "rendering": case "blocked": return 1;
    default: return 0;
  }
}

/** Los 3 pasos con su estado. Una clave desconocida cae en "Corregir". */
export function correctionStepList(workflow) {
  const position = stepPosition(workflow);
  return CORRECTION_STEPS.map((step, index) => ({
    ...step,
    state: position === -1 ? "todo" : index < position ? "done" : index === position ? "active" : "todo",
  }));
}

export const STEP_STATE_LABELS = { done: "hecho", active: "en curso", todo: "pendiente" };

/**
 * UNA sola acción principal, con el nombre de lo que hace. Devuelve siempre un
 * objeto {key, label, disabled}; el panel le agrega el href o el handler.
 *
 * El orden replica el de las reglas anteriores (revisión bloqueante, reabrir,
 * refrescar, error, master, publicar, render en curso, propuesta, render,
 * cierre): sólo cambia el nombre, y "Analizar pedido" pasa a ser una ayuda
 * secundaria en vez de la acción principal.
 */
export function correctionPrimary(workflow, ctx = {}) {
  const {
    isResolved = false, hasJob = true, publication = null, status = {},
    effectiveProposal = null, loadedProposal = null, summaryProposal = null,
    proposalEnabled = false, qcReview = null, busy = {},
  } = ctx;
  const allows = (action) => workflowAllows(workflow, action);
  const server = Array.isArray(workflow?.allowed_actions);
  const proresPending = Boolean(publication?.prores_pending?.length);
  const pick = (key, label = PRIMARY_LABELS[key], disabled = false) => ({ key, label, disabled });
  const edit = () => (hasJob ? pick("edit") : pick("none", PRIMARY_LABELS.none, true));
  const master = () => pick("prepare_master",
    busy.publishing ? PRIMARY_LABELS.preparing_master
      : status.needsProResSetup ? PRIMARY_LABELS.prepare_master_setup : PRIMARY_LABELS.prepare_master,
    Boolean(busy.publishing));
  const publish = () => pick("publish", busy.publishing ? PRIMARY_LABELS.publishing : PRIMARY_LABELS.publish,
    Boolean(busy.publishing));
  const reopen = () => pick("reopen", busy.resolving ? PRIMARY_LABELS.reopening : PRIMARY_LABELS.reopen,
    Boolean(busy.resolving));
  const reviewProposal = () => pick("review_proposal", undefined, Boolean(busy.proposal));
  const close = () => pick("close", undefined, Boolean(busy.resolving || busy.noNote));

  if (qcReview) return pick("qc_review", qcReview.label);

  if (server) {
    if (isResolved && allows("reopen")) return reopen();
    if (["refresh", "unknown"].includes(workflow.key) && allows("refresh")) return pick("refresh");
    if (workflow.key === "blocked" && allows("edit") && hasJob) return pick("see_error");
    if (workflow.key === "analyze" && allows("analyze")) return edit();
    if (allows("prepare_master") && proresPending) return master();
    if (allows("publish") && status.canPublish) return publish();
    if (workflow.key === "rendering") return pick("rendering", undefined, true);
    if (allows("review_proposal") && ["apply", "analyze"].includes(workflow.key)) return reviewProposal();
    if (allows("review_render")) return pick("render", undefined, Boolean(busy.proposal || busy.publishing));
    if (allows("resolve")) return close();
    if (allows("refresh")) return pick("refresh");
    if (allows("edit") && hasJob) return pick("edit");
    return pick("none", PRIMARY_LABELS.none, true);
  }

  // Datos sin proyección del servidor: se infiere de la publicación y la propuesta.
  if (isResolved) return reopen();
  if (workflow?.key === "analyze" && !effectiveProposal) return edit();
  if (status.canPublish) return proresPending ? master() : publish();
  if (workflow?.key === "rendering") return pick("rendering", undefined, true);
  if (publication?.render_matches_editor && !publication?.needs_publish) return close();
  if (proposalEnabled && !effectiveProposal) return edit();
  if (PROPOSAL_APPLIED_STATUSES.has(effectiveProposal?.status) && hasJob) {
    return pick("render", undefined, Boolean(busy.proposal));
  }
  if (proposalEnabled && summaryProposal && !loadedProposal) return reviewProposal();
  if (PROPOSAL_REVIEW_STATUSES.has(loadedProposal?.status)) return pick("review_proposal");
  return edit();
}

/** Enlaces chicos bajo el botón principal. Nunca repiten lo que ya hace el principal. */
export function correctionSecondary(workflow, primary, ctx = {}) {
  const {
    isResolved = false, hasJob = true, effectiveProposal = null, proposalEnabled = false, busy = {},
  } = ctx;
  if (isResolved) return [];
  const links = [];
  const canSuggest = proposalEnabled && workflowAllows(workflow, "analyze")
    && ["analyze", "manual", "render"].includes(workflow?.key)
    && (!effectiveProposal || workflow.key === "analyze");
  if (canSuggest) {
    links.push({ key: "suggest", label: busy.proposal ? SECONDARY_LABELS.suggesting : SECONDARY_LABELS.suggest,
      disabled: Boolean(busy.proposal) });
  }
  if (hasJob && !["edit", "see_error"].includes(primary.key)) {
    links.push({ key: "edit", label: SECONDARY_LABELS.edit });
  }
  if (primary.key !== "close") links.push({ key: "close", label: SECONDARY_LABELS.close });
  return links;
}

/**
 * Una frase en lenguaje llano para el paso actual. Sin jerga interna: nunca
 * "preflight", "QC" ni "fingerprint".
 */
export function correctionSentence(workflow, ctx = {}) {
  const { qcReview = null, publication = null, proposalEnabled = false } = ctx;
  const pending = Number(workflow?.pending_manual) || 0;
  const manual = pending > 0
    ? ` Hay ${pending} ${pending === 1 ? "indicación" : "indicaciones"} que hay que comprobar a mano en el video.`
    : "";
  if (qcReview) {
    return "La publicación está en pausa: falta revisar el video antes de publicar.";
  }
  switch (workflow?.key) {
    case "empty": return "Elegí un pedido de la cola.";
    case "analyze":
      return proposalEnabled
        ? `Corregí lo que pidió el cliente en el editor. Si querés, la IA te sugiere los cambios primero.${manual}`
        : `Corregí lo que pidió el cliente en el editor.${manual}`;
    case "manual": return `Corregí lo que pidió el cliente en el editor.${manual}`;
    case "apply":
      return `La IA sugirió cambios. Revisalos y aplicá los que correspondan.${manual}`;
    case "render":
      return `La corrección está guardada, pero el video todavía es el anterior. Generá el video corregido.${manual}`;
    case "rendering":
      return "Estamos generando el video nuevo. El portal sigue mostrando el anterior hasta que lo publiques.";
    case "blocked":
      return "No se pudo generar el video nuevo. Abrí el error para ver el motivo y no vuelvas a aplicar los cambios guardados.";
    case "publish":
      return publication?.prores_pending?.length
        ? `El video nuevo está listo. Falta preparar el archivo profesional antes de publicar.${manual}`
        : `El video nuevo está listo. Miralo y publicalo: el cliente lo ve en el portal y el pedido queda resuelto.${manual}`;
    case "review":
      return "Ya hay una versión publicada. Revisá que lo pedido esté en el video y cerrá el pedido con una nota.";
    case "resolved": case "closed":
      return stepPosition(workflow) === 3
        ? "Pedido resuelto: el video nuevo ya está en el portal."
        : "Pedido cerrado sin publicar un video nuevo.";
    case "refresh":
      return "La propuesta cambió. Actualizá el estado y revisá los cambios antes de seguir.";
    default:
      return "Todavía no podemos confirmar en qué punto está este pedido. Actualizá el estado.";
  }
}

/** Todo junto: pasos, acción principal, enlaces secundarios y la frase del paso. */
export function correctionSteps(workflow, ctx = {}) {
  const primary = correctionPrimary(workflow, ctx);
  return {
    steps: correctionStepList(workflow),
    primary,
    secondary: correctionSecondary(workflow, primary, ctx),
    sentence: correctionSentence(workflow, ctx),
  };
}

// Mensajes de error al publicar, con qué hacer. Nunca un callejón sin salida:
// lo desconocido igual nombra el estado HTTP o el código.
export function describePublishError(error) {
  const detail = error?.detail;
  const code = String(
    (detail && typeof detail === "object" ? detail.code : detail) || error?.code || "",
  ).trim();
  const status = error?.status;
  const has = (...needles) => needles.some((needle) => code.includes(needle));
  if (status === 402 || has("quota", "insufficient_credit", "payment_required")) {
    return "Falta crédito en la cuenta del video. Cargá crédito y volvé a publicar.";
  }
  if (has("language_review_unresolved")) {
    return "La letra no coincide con el idioma de la referencia. Abrí el editor y confirmá el idioma.";
  }
  if (has("lyric_review_pending")) {
    return "Quedan líneas de la letra sin revisar. Abrí el editor, revisalas y volvé a publicar.";
  }
  if (has("change_request_job_mismatch")) {
    return "Este pedido pertenece a otro video. Actualizá la pantalla y abrí el pedido de nuevo.";
  }
  if (has("fresh_preflight_required", "stale_preflight", "delivery_qc_report_stale", "preflight_stale")) {
    return "El video cambió desde la última revisión. Volvé a revisarlo y después publicá.";
  }
  if (has("publication_context_has_pending_writes")) {
    return "El video todavía se está guardando. Esperá un minuto, actualizá y reintentá.";
  }
  if (has("publication_storage_unavailable")) {
    return "No se pudieron copiar los archivos al portal. El portal conserva la versión anterior; reintentá en un minuto.";
  }
  if (has("prores_required", "prores_stale_without_spec")) {
    return "Falta preparar el archivo profesional antes de publicar. Usá “Preparar el archivo profesional”.";
  }
  if (status === 403) return "Tu usuario no tiene permiso para publicar. Pedile a un administrador que lo haga.";
  if (status === 404) return "No encontramos el video o el pedido. Actualizá la pantalla.";
  if (status === 409) return "El video cambió mientras se publicaba. Actualizá y reintentá.";
  const reason = typeof detail === "string" && detail.includes(" ") ? detail : (error?.message || code);
  const tag = [status ? `HTTP ${status}` : "", code && !code.includes(" ") ? code : ""].filter(Boolean).join(" · ");
  return `No se pudo publicar${tag ? ` (${tag})` : ""}${reason && reason !== code ? `: ${reason}` : ""}. Actualizá la pantalla y reintentá; si se repite, avisá al equipo con el número del pedido.`;
}
