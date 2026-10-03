// Pedidos de cambio de UMG (delivery_change_requests).
//
// El operador filtra pending/resolved/all (chips con badge), ve el contexto
// del delivery (artista, canción, label, frame_size, tenant, owner), un
// preview del video clickeable, el comentario, y resuelve / reabre.
//
// El operador ve el ciclo completo en la misma tarjeta. Cada publicación
// congela un manifiesto de archivos; verificar y publicar son pasos distintos.
// Los tres pasos viven acá:
//
//   1. Editar letra    → abre el editor de esa canción.
//   2. Verificar       → confirma todas las instrucciones sobre el corte final.
//      Publicar        → actualiza el manifiesto y cierra solo los pedidos
//                        verificados para ese corte.
//   3. Marcar resuelto → sigue estando, para lo que se contesta sin
//                        re-renderizar (una aclaración, un pedido que se
//                        descarta).
import { useEffect, useState } from "react";

import { fmtDate, fmtAgo } from "../../adminApi";
import FilterBar from "../../primitives/FilterBar";
import EmptyState from "../../primitives/EmptyState";
import TableSkeleton from "../../primitives/TableSkeleton";
import EnableProResModal from "../../../EnableProResModal";

const PORTAL_LABELS = { argentina: "UMG Argentina", chile: "UMG Chile" };
const PHASE_LABELS = {
  blocked: "Bloqueados", rendering: "Generando video",
  proposal_ready: "Propuestas listas", needs_edit: "Pendientes de edición",
  changes_saved: "Cambios guardados",
  needs_review: "Pendientes de revisión", preparing_files: "Preparando archivos",
  needs_verification: "Pendientes de verificación",
  ready_to_publish: "Listos para publicar", resolved: "Resueltos",
};

// Estados en los que el job está re-renderizando: publicar ahora no tiene
// sentido porque los archivos se están por reemplazar.
const BUSY_JOB_STATUSES = new Set([
  "queued", "processing", "rendering", "editing", "transcribed_pending",
]);

/**
 * Traduce el bloque `publication` del backend a UNA frase y un tono.
 *
 * El orden importa: es el orden en que los problemas bloquean al operador.
 * Primero lo que impide publicar (render en curso, master desfasado),
 * después lo que falta hacer (publicar), y recién al final los estados de
 * reposo (esperando al cliente / aprobado).
 */
export function publicationStatus(publication, workflow = null) {
  if (!publication) {
    return {
      tone: "idle",
      title: "Sin publicación activa en el portal",
      detail: "Esta entrega no está publicada o fue dada de baja.",
      canPublish: false,
    };
  }
  const revision = publication.revision || 1;
  const prores = publication.prores_pending || [];

  if (BUSY_JOB_STATUSES.has(publication.job_status)) {
    return {
      tone: "busy",
      title: "Re-renderizando",
      detail:
        "Mientras tanto el portal sigue entregando la versión anterior. " +
        "Cuando termine, publicá la actualización desde acá.",
      canPublish: false,
    };
  }
  if (workflow?.next_action === "review_qc") {
    return {
      tone: "wait",
      title: "Controles pendientes del video",
      detail: workflow.blockers?.[0]?.message || "Revisá los controles del corte actual.",
      canPublish: false,
      needsInternalReview: true,
    };
  }
  if (workflow?.next_action === "render_changes") {
    return {
      tone: "wait", title: "Cambios guardados; falta generar el video",
      detail: "La propuesta modificó la letra del editor. Generá un corte nuevo para revisarlo.",
      canPublish: false,
    };
  }
  if (publication.internal_approval_current === false && publication.needs_publish) {
    return {
      tone: "wait",
      title: "Falta aprobar el video final",
      detail: "Revisá el corte actualizado y completá la aprobación interna antes de publicarlo.",
      canPublish: false,
      needsInternalReview: true,
    };
  }
  if (prores.length) {
    if (publication.prores_configured === false) {
      return {
        tone: "wait",
        title: "Hay que elegir el formato del archivo profesional",
        detail:
          "Esta entrega vieja perdió la configuración de resolución, cuadros por segundo y perfil. " +
          "Elegilos una vez para regenerar el .mov con la corrección.",
        canPublish: true,
        publishLabel: "Elegir formato y actualizar .mov",
        needsProResSetup: true,
      };
    }
    return {
      tone: "wait",
      title: "Falta actualizar el archivo profesional (.mov)",
      detail:
        "El video de arriba ya tiene la corrección, pero el archivo de máxima " +
        "calidad que descarga Universal todavía es la versión anterior. " +
        "Actualizalo primero; cuando termine aparecerá Publicar actualización.",
      canPublish: true,
      publishLabel: "Actualizar archivo profesional",
    };
  }
  if (publication.needs_publish) {
    return {
      tone: "warn",
      title: "El render nuevo está listo para revisar",
      detail:
        "Abrí el video de esta tarjeta y comprobá el cambio. El portal sigue " +
        "entregando el corte anterior hasta que publiques la actualización.",
      canPublish: true,
      publishLabel: "Publicar actualización",
    };
  }
  if (publication.awaiting_review) {
    return {
      tone: "ok",
      title: `Versión ${revision} publicada · esperando al cliente`,
      detail: "El cliente todavía no aprobó esta versión en el portal.",
      canPublish: false,
    };
  }
  if (publication.approved_at) {
    return {
      tone: "ok",
      title: `Versión ${revision} aprobada por ${publication.approved_by_label || "el cliente"}`,
      detail: `Aprobada el ${fmtDate(publication.approved_at)}.`,
      canPublish: false,
    };
  }
  return {
    tone: "ok",
    title: `Versión ${revision} publicada`,
    detail: "El portal está entregando este mismo corte.",
    canPublish: false,
  };
}

const TONE_STYLES = {
  warn: "bg-amber-500/10 ring-amber-400/30 text-amber-100",
  wait: "bg-amber-500/10 ring-amber-400/25 text-amber-100",
  busy: "bg-brand/10 ring-brand/25 text-brand-light",
  ok: "bg-emerald-500/10 ring-emerald-400/20 text-emerald-100",
  idle: "bg-surface-2/40 ring-white/[0.06] text-gray-300",
};

export default function ChangeRequestsPanel({
  changeRequests,
  crStatusFilter,
  setCrStatusFilter,
  crPendingCount,
  crResolvedCount,
  crLoading,
  crResolvingId,
  crVerifyingId,
  resolveChangeRequest,
  verifyChangeRequest,
  reopenChangeRequest,
  crPublishingId,
  crPublishNotice,
  dismissPublishNotice,
  publishDeliveryUpdate,
  proposalEnabled = false,
  proposalApplyEnabled = false,
  proposalBusyId = null,
  proposalDetails = {},
  generateProposal = () => {},
  loadProposal = () => {},
  adjustProposal = () => {},
  applyProposal = () => {},
  dismissProposal = () => {},
  regenerateBackground = () => {},
  onProResConfigured = () => {},
}) {
  // Draft local del input de "respuesta" por CR. Clave = id del CR.
  const [drafts, setDrafts] = useState({});
  const [proResSetup, setProResSetup] = useState(null);
  const [portalFilter, setPortalFilter] = useState(() =>
    new URLSearchParams(window.location.search).get("change_portal") || "all");
  const [phaseFilter, setPhaseFilter] = useState(() =>
    new URLSearchParams(window.location.search).get("change_stage") || "all");
  const [search, setSearch] = useState(() =>
    new URLSearchParams(window.location.search).get("change_search") || "");
  const [selectedId, setSelectedId] = useState(() =>
    new URLSearchParams(window.location.search).get("change_request_id"));

  const normalizedSearch = search.trim().toLocaleLowerCase();
  const visibleRequests = changeRequests.filter((item) => {
    const delivery = item.delivery || {};
    if (portalFilter !== "all" && delivery.portal_id !== portalFilter) return false;
    if (phaseFilter !== "all" && item.workflow?.phase !== phaseFilter) return false;
    if (!normalizedSearch) return true;
    return [delivery.artist, delivery.song, item.comment]
      .some((value) => String(value || "").toLocaleLowerCase().includes(normalizedSearch));
  });
  const selectedRequest = visibleRequests.find((item) => String(item.id) === String(selectedId))
    || visibleRequests[0];

  useEffect(() => {
    const onHistory = () => {
      const params = new URLSearchParams(window.location.search);
      setSelectedId(params.get("change_request_id"));
      setPortalFilter(params.get("change_portal") || "all");
      setPhaseFilter(params.get("change_stage") || "all");
      setSearch(params.get("change_search") || "");
    };
    window.addEventListener("popstate", onHistory);
    return () => window.removeEventListener("popstate", onHistory);
  }, []);

  const rememberSelection = (id) => {
    setSelectedId(String(id));
    const url = new URL(window.location.href);
    url.searchParams.set("change_request_id", String(id));
    window.history.replaceState(window.history.state, "", url);
  };

  useEffect(() => {
    const url = new URL(window.location.href);
    if (portalFilter === "all") url.searchParams.delete("change_portal");
    else url.searchParams.set("change_portal", portalFilter);
    if (phaseFilter === "all") url.searchParams.delete("change_stage");
    else url.searchParams.set("change_stage", phaseFilter);
    if (search.trim()) url.searchParams.set("change_search", search.trim());
    else url.searchParams.delete("change_search");
    window.history.replaceState(window.history.state, "", url);
  }, [portalFilter, phaseFilter, search]);

  useEffect(() => {
    if (!selectedId || crLoading) return undefined;
    const frame = requestAnimationFrame(() => {
      document.getElementById(`change-request-${selectedId}`)?.scrollIntoView?.({
        behavior: "smooth", block: "center",
      });
    });
    return () => cancelAnimationFrame(frame);
  }, [selectedId, crLoading]);
  const setDraft = (id, val) => setDrafts((d) => ({ ...d, [id]: val }));

  const filterOptions = [
    { id: "pending", label: "Pendientes", badge: crPendingCount },
    { id: "resolved", label: "Resueltos", badge: crResolvedCount },
    { id: "all", label: "Todos" },
  ];

  return (
    <div className="space-y-4">
      <FilterBar>
        <FilterBar.Chips
          value={crStatusFilter}
          onChange={setCrStatusFilter}
          options={filterOptions}
          label="Estado"
        />
      </FilterBar>

      <div className="flex flex-wrap items-end gap-3">
        <label className="text-label text-gray-300">
          Portal
          <select aria-label="Filtrar por portal" value={portalFilter}
            onChange={(event) => setPortalFilter(event.target.value)}
            className="mt-1 block rounded-button bg-surface-2 px-3 py-2 text-white ring-1 ring-white/10">
            <option value="all">Todos los portales</option>
            <option value="argentina">UMG Argentina</option>
            <option value="chile">UMG Chile</option>
          </select>
        </label>
        <label className="text-label text-gray-300 grow max-w-md">
          Buscar pedido
          <input type="search" value={search} onChange={(event) => setSearch(event.target.value)}
            placeholder="Artista, canción o texto del pedido"
            className="mt-1 block w-full rounded-button bg-surface-2 px-3 py-2 text-white ring-1 ring-white/10 focus:outline-none focus:ring-brand/50" />
        </label>
        <label className="text-label text-gray-300">
          Etapa
          <select aria-label="Filtrar por etapa" value={phaseFilter}
            onChange={(event) => setPhaseFilter(event.target.value)}
            className="mt-1 block rounded-button bg-surface-2 px-3 py-2 text-white ring-1 ring-white/10">
            <option value="all">Todas las etapas</option>
            {Object.entries(PHASE_LABELS).map(([phase, label]) => (
              <option key={phase} value={phase}>{label}</option>
            ))}
          </select>
        </label>
      </div>

      {crPublishNotice && (
        <div
          role="status"
          className={`rounded-card p-3 text-caption ring-1 flex items-start justify-between gap-3 ${
            TONE_STYLES[crPublishNotice.tone === "ok" ? "ok" : "wait"]
          }`}
        >
          <div className="space-y-2">
            <p>{crPublishNotice.text}</p>
            {crPublishNotice.actionHref && (
              <a
                href={crPublishNotice.actionHref}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex min-h-9 items-center rounded-button bg-white/10 px-3 py-1.5 font-semibold text-white hover:bg-white/15"
              >
                {crPublishNotice.actionLabel || "Revisar controles"}
              </a>
            )}
          </div>
          <button
            onClick={dismissPublishNotice}
            className="text-label opacity-70 hover:opacity-100 shrink-0"
          >
            Cerrar
          </button>
        </div>
      )}

      {crLoading && changeRequests.length === 0 ? (
        <div className="glass rounded-card p-2">
          <TableSkeleton rows={3} cols={3} />
        </div>
      ) : visibleRequests.length === 0 ? (
        <EmptyState
          title={
            crStatusFilter === "pending"
              ? "Sin pedidos pendientes"
              : crStatusFilter === "resolved"
                ? "Sin pedidos resueltos"
                : "Sin pedidos de cambio"
          }
          message={
            crStatusFilter === "pending"
              ? "No hay pedidos de cambio pendientes."
              : "Todavía no hay pedidos en esta categoría."
          }
        />
      ) : (
        <div className="grid min-w-0 gap-4 lg:grid-cols-[minmax(240px,300px)_minmax(0,1fr)]">
          <nav aria-label="Cola de pedidos" className="min-w-0 lg:sticky lg:top-4 lg:self-start lg:max-h-[calc(100vh-8rem)] lg:overflow-y-auto rounded-card bg-surface-2/30 ring-1 ring-white/10 p-2">
            <p className="px-2 py-2 text-label font-semibold uppercase tracking-wide text-gray-400">
              {visibleRequests.length} {visibleRequests.length === 1 ? "pedido" : "pedidos"}
            </p>
            <div className="space-y-1">
              {visibleRequests.map((item) => (
                <button key={item.id} type="button" onClick={() => rememberSelection(item.id)}
                  aria-current={selectedRequest?.id === item.id ? "true" : undefined}
                  className={`w-full rounded-button px-3 py-3 text-left transition-colors ${
                    selectedRequest?.id === item.id
                      ? "bg-brand/20 text-white ring-1 ring-brand/40"
                      : "text-gray-300 hover:bg-white/[0.06]"
                  }`}>
                  <span className="block truncate text-caption font-semibold">{item.delivery?.artist || "Sin artista"} · {item.delivery?.song || "Sin canción"}</span>
                  <span className="mt-1 block text-label text-gray-400">
                    {PORTAL_LABELS[item.delivery?.portal_id] || item.delivery?.portal_id || "Portal sin asignar"}
                    {" · "}{item.resolved_at ? "Resuelto" : "Pendiente"}
                  </span>
                  {item.workflow?.next_action_label && !item.resolved_at && (
                    <span className="mt-1 block truncate text-label text-brand-light">
                      {item.workflow.next_action_label}
                    </span>
                  )}
                  <span className="mt-1 block truncate text-label text-gray-500">{item.comment}</span>
                </button>
              ))}
            </div>
          </nav>
          {selectedRequest && [selectedRequest].map((item) => (
            <ChangeRequestCard
              key={item.id}
              item={item}
              draft={drafts[item.id] || ""}
              onDraftChange={(v) => setDraft(item.id, v)}
              resolving={crResolvingId === item.id}
              verifying={crVerifyingId === item.id}
              publishing={crPublishingId === item.id}
              onResolve={() => resolveChangeRequest(item.id, drafts[item.id])}
              onVerify={(instructionIds, fingerprint) =>
                verifyChangeRequest(item.id, instructionIds, fingerprint)}
              onReopen={() => reopenChangeRequest(item.id)}
              onPublish={() => {
                const status = publicationStatus(item.publication);
                if (status.needsProResSetup) {
                  setProResSetup({
                    jobId: item.delivery?.job_id,
                    requestId: item.id,
                    frameSize: item.delivery?.frame_size,
                  });
                  return;
                }
                publishDeliveryUpdate(
                  item.delivery?.job_id,
                  item.delivery?.portal_id,
                  item.id,
                );
              }}
              proposalEnabled={proposalEnabled}
              proposalApplyEnabled={proposalApplyEnabled}
              proposalBusy={proposalBusyId === item.id}
              proposal={proposalDetails[item.id] || null}
              onGenerateProposal={() => generateProposal(item.id)}
              onLoadProposal={() => loadProposal(item.id)}
              onAdjustProposal={(proposalId, operationId, requestedText, baseRevision) =>
                adjustProposal(
                  item.id, proposalId, operationId, requestedText, baseRevision,
                )
              }
              onApplyProposal={(proposalId, operationIds, baseRevision) =>
                applyProposal(item.id, proposalId, operationIds, baseRevision)
              }
              onDismissProposal={(proposalId) => dismissProposal(item.id, proposalId)}
              onRegenerateBackground={(proposalId, operationId, prompt, backgroundMode) =>
                regenerateBackground(
                  item.id, proposalId, operationId, item.delivery?.job_id,
                  prompt, backgroundMode,
                )
              }
            />
          ))}
        </div>
      )}

      {proResSetup && (
        <EnableProResModal
          jobId={proResSetup.jobId}
          initialFrameSize={proResSetup.frameSize}
          title="Configurar y actualizar el archivo profesional"
          description="Elegí el formato que requiere Universal. Vamos a regenerar el .mov con el video corregido; todavía no se publicará en el portal."
          submitLabel="Guardar formato y actualizar .mov"
          onClose={() => setProResSetup(null)}
          onSuccess={(data) => {
            const setup = proResSetup;
            setProResSetup(null);
            onProResConfigured(setup.requestId, data);
          }}
        />
      )}
    </div>
  );
}

function ChangeRequestCard({
  item, draft, onDraftChange, resolving, verifying, publishing,
  onResolve, onReopen, onPublish, onVerify,
  proposalEnabled, proposalApplyEnabled, proposalBusy, proposal,
  onGenerateProposal, onLoadProposal, onAdjustProposal, onApplyProposal,
  onDismissProposal, onRegenerateBackground,
}) {
  const d = item.delivery || {};
  const isResolved = !!item.resolved_at;
  const status = publicationStatus(item.publication, item.workflow);
  const workflowActions = item.workflow?.allowed_actions;
  const instructions = item.instructions || [];
  const canVerifyPublishedCut = item.requested_revision != null
    && (item.publication?.revision || 0) > item.requested_revision;
  const canVerify = workflowActions
    ? workflowActions.includes("verify")
    : item.publication?.job_status === "done"
      && item.publication?.internal_approval_current !== false
      && (item.publication?.needs_publish || canVerifyPublishedCut);
  const canFinishPublication = workflowActions
    ? ["publish", "prepare_prores", "configure_prores"].some((action) => workflowActions.includes(action))
    : status.canPublish || (canVerifyPublishedCut && item.verification?.current);
  const [checkedInstructions, setCheckedInstructions] = useState([]);
  const [confirmedWholeComment, setConfirmedWholeComment] = useState(false);
  useEffect(() => {
    setCheckedInstructions([]);
    setConfirmedWholeComment(false);
  }, [item.verification?.render_fingerprint]);
  // Un pedido resuelto AL PUBLICAR no necesita que nadie confirme nada: la
  // corrección ya está en el portal. Uno cerrado a mano sí se explica.
  const closedByPublication = item.resolution_source === "publication";

  return (
    <div
      id={`change-request-${item.id}`}
      className={`glass rounded-card p-5 border-l-4 ${
        isResolved ? "border-emerald-500/60 opacity-75" : "border-amber-400"
      }`}
    >
      {/* Contexto del delivery */}
      <div className="flex items-start justify-between gap-3 mb-3 flex-wrap">
        <div className="min-w-0">
          <p className="text-section uppercase tracking-wider text-brand-light font-bold mb-0.5">
            {d.artist || "(sin artista)"}
          </p>
          <h3 className="text-ui font-bold leading-snug text-white">
            {d.song || "(canción eliminada)"}
          </h3>
          <div className="flex items-center gap-2 mt-1 flex-wrap text-label text-gray-500">
            {d.label && <span>{d.label}</span>}
            {d.portal_id && (<><span>·</span><span>{PORTAL_LABELS[d.portal_id] || d.portal_id}</span></>)}
            {d.frame_size && (<><span>·</span><span className="text-brand-light">{d.frame_size}</span></>)}
            {d.job_id && (<><span>·</span><span className="font-mono">job {d.job_id}</span></>)}
            {d.tenant && (<><span>·</span><span>{d.tenant}</span></>)}
            {(d.owner_email || d.owner_username) && (
              <>
                <span>·</span>
                <span title="Usuario que generó el video">
                  por <span className="text-gray-300">{d.owner_email || d.owner_username}</span>
                </span>
              </>
            )}
            {d.removed_at && <span className="text-red-300">· entrega eliminada</span>}
          </div>
        </div>
        <span
          className={`text-section font-bold uppercase px-2 py-1 rounded-button shrink-0 ${
            isResolved ? "bg-emerald-500/15 text-emerald-300" : "bg-amber-500/15 text-amber-300"
          }`}
        >
          {isResolved ? "Resuelto" : "Pendiente"}
        </span>
      </div>

      {/* Estado real de lo que el cliente puede descargar ahora mismo. */}
      <div className={`rounded-button ring-1 p-3 mb-3 ${TONE_STYLES[status.tone]}`}>
        <p className="text-caption font-semibold">{status.title}</p>
        <p className="text-label opacity-80 mt-0.5 leading-relaxed">{status.detail}</p>
        {item.publication?.stale_since && (
          <p className="text-label opacity-70 mt-1">
            Cambios en curso desde {fmtAgo(item.publication.stale_since)}.
          </p>
        )}
      </div>
      {!isResolved && item.workflow && (
        <div className="mb-3 rounded-button bg-brand/[0.08] px-3 py-2 ring-1 ring-brand/20 text-label text-gray-200">
          <span className="font-semibold text-white">Próximo paso: </span>
          {item.workflow.next_action_label}
          {item.workflow.blockers?.filter((blocker) => blocker.message !== status.detail).map((blocker) => (
            <p key={blocker.code} className="mt-1 text-amber-200">{blocker.message}</p>
          ))}
        </div>
      )}


      {/* Preview: thumbnail clickeable que abre el video en pestaña nueva. */}
      {d.thumbnail_url && (
        <a
          href={d.video_url || d.thumbnail_url}
          target="_blank"
          rel="noopener noreferrer"
          className="block relative mb-3 rounded-button overflow-hidden ring-1 ring-white/[0.06] group"
          title={d.video_url ? "Abrir video en pestaña nueva" : "Abrir imagen"}
        >
          <img
            src={d.thumbnail_url}
            alt="Preview del video"
            loading="lazy"
            className="w-full max-h-[220px] object-contain bg-black/40"
          />
          {d.video_url && (
            <span className="absolute inset-0 flex items-center justify-center">
              <span className="w-12 h-12 rounded-full bg-black/50 ring-1 ring-white/30 flex items-center justify-center group-hover:bg-black/70 transition-colors duration-brand">
                <svg className="w-5 h-5 text-white ml-0.5" viewBox="0 0 24 24" fill="currentColor">
                  <path d="M8 5v14l11-7z" />
                </svg>
              </span>
            </span>
          )}
        </a>
      )}

      {/* Comentario del pedido */}
      <div className="rounded-button bg-surface-2/40 ring-1 ring-white/[0.04] p-3 text-caption leading-relaxed whitespace-pre-wrap font-mono text-gray-200">
        {item.comment}
      </div>

      <p className="text-label text-gray-500 mt-2">
        UMG envió este pedido el {fmtDate(item.submitted_at)}
        {item.requested_revision ? ` · Sobre la versión ${item.requested_revision}` : ""}
      </p>

      {!isResolved && (
        <section className="mt-3 rounded-button bg-surface-2/40 ring-1 ring-white/10 p-3" aria-label="Verificar pedido">
          <h4 className="text-caption font-semibold text-white">Comprobar el pedido en el video final</h4>
          {item.verification?.current ? (
            <p className="mt-2 text-label text-emerald-300">Verificado para este corte. Confirmá la publicación para cerrar el pedido.</p>
          ) : (
            <>
              {item.verification?.stale && <p className="mt-2 text-label text-amber-200">La verificación anterior corresponde a otro corte. Revisá esta versión.</p>}
              <div className="mt-3 space-y-2">
                {instructions.map((instruction) => (
                  <label key={instruction.id} className="flex items-start gap-2 text-label text-gray-200">
                    <input type="checkbox" className="mt-0.5 accent-brand" checked={checkedInstructions.includes(instruction.id)}
                      onChange={(event) => setCheckedInstructions((current) => event.target.checked
                        ? [...current, instruction.id] : current.filter((id) => id !== instruction.id))} />
                    <span>{instruction.timecode_seconds != null ? `${Math.floor(instruction.timecode_seconds / 60)}:${String(Math.floor(instruction.timecode_seconds % 60)).padStart(2, "0")} · ` : ""}{instruction.source_excerpt}</span>
                  </label>
                ))}
                <label className="flex items-start gap-2 border-t border-white/10 pt-2 text-label font-medium text-white">
                  <input type="checkbox" className="mt-0.5 accent-brand" checked={confirmedWholeComment}
                    onChange={(event) => setConfirmedWholeComment(event.target.checked)} />
                  <span>Revisé el comentario completo de UMG y comprobé todos los cambios en el video final.</span>
                </label>
              </div>
              <button type="button" className="mt-3 rounded-button bg-brand px-3 py-2 text-caption font-semibold text-white disabled:opacity-40"
                disabled={verifying || !canVerify || !confirmedWholeComment || checkedInstructions.length !== instructions.length || !item.verification?.render_fingerprint}
                onClick={() => onVerify(checkedInstructions, item.verification.render_fingerprint)}>
                {verifying ? "Guardando verificación…" : "Confirmar pedido verificado"}
              </button>
              {!canVerify && <p className="mt-2 text-label text-gray-400">Esperá al render nuevo y a su aprobación interna para verificar el pedido.</p>}
            </>
          )}
        </section>
      )}

      {!isResolved && proposalEnabled && (
        <ChangeRequestProposal
          summary={item.proposal}
          proposal={proposal}
          requestComment={item.comment}
          busy={proposalBusy}
          applyEnabled={proposalApplyEnabled}
          jobId={d.job_id}
          onGenerate={onGenerateProposal}
          onLoad={onLoadProposal}
          onAdjust={onAdjustProposal}
          onApply={onApplyProposal}
          onDismiss={onDismissProposal}
          onRegenerateBackground={onRegenerateBackground}
        />
      )}

      {/* Resolución */}
      {isResolved ? (
        <div className="mt-3 pt-3 border-t border-white/[0.06] flex items-start justify-between gap-3 flex-wrap">
          <div className="text-label text-gray-400 min-w-0">
            <span className="text-emerald-300 font-medium">
              {closedByPublication
                ? `Resuelto al publicar la versión ${item.resolved_by_revision}`
                : "Resuelto"}
            </span>
            {!closedByPublication && item.resolved_by && <> por <b>{item.resolved_by}</b></>}
            {" "}el {fmtDate(item.resolved_at)}
            {item.resolution_note && (
              <p className="mt-1 text-gray-300 whitespace-pre-wrap">
                <span className="text-gray-500">Respuesta: </span>
                {item.resolution_note}
              </p>
            )}
          </div>
          <button
            onClick={onReopen}
            disabled={resolving}
            className="text-label text-amber-300 hover:text-amber-200 disabled:opacity-50"
          >
            Reabrir
          </button>
        </div>
      ) : (
        <div className="mt-3 pt-3 border-t border-white/[0.06] space-y-3">
          {/* Los dos pasos que realmente atienden el pedido. */}
          <div className="flex flex-wrap items-center gap-2">
            {item.workflow?.next_action === "render_changes" && d.job_id && (
              <a href={`/videos/${d.job_id}/edit-lyrics?change_request_id=${item.id}`}
                className="bg-brand/20 text-brand-light text-caption font-medium px-3 py-1.5 rounded-button ring-1 ring-brand/30">
                Generar video actualizado
              </a>
            )}
            {(status.needsInternalReview || item.workflow?.next_action === "review_qc") && d.job_id && (
              <a href={`/videos/${d.job_id}?qc_focus=manual&return_to=${encodeURIComponent(`${window.location.pathname}${window.location.search}`)}`}
                className="bg-amber-500/20 text-amber-100 text-caption font-medium px-3 py-1.5 rounded-button ring-1 ring-amber-400/30">
                Revisar y aprobar video
              </a>
            )}
            {d.job_id && (
              <a
                href={`/videos/${d.job_id}/edit-lyrics`}
                target="_blank"
                rel="noopener noreferrer"
                className="bg-white/[0.07] hover:bg-white/[0.12] text-white text-caption font-medium px-3 py-1.5 rounded-button transition-colors duration-brand"
              >
                Editar letra
              </a>
            )}
            {d.job_id && (
              <button
                onClick={onPublish}
                disabled={publishing || !canFinishPublication}
                title={
                  canFinishPublication
                    ? "Publica este corte en el portal; solo cierra los pedidos verificados"
                    : "No hay nada nuevo para publicar en este momento"
                }
                className="bg-brand hover:bg-brand-light text-white text-caption font-medium px-3 py-1.5 rounded-button disabled:opacity-40 disabled:cursor-not-allowed transition-colors duration-brand"
              >
                {publishing
                  ? "Publicando…"
                  : canVerifyPublishedCut && !status.canPublish ? "Confirmar versión publicada" : status.publishLabel || "Publicar actualización"}
              </button>
            )}
          </div>

          {/* Cierre manual, para lo que se contesta sin re-renderizar. */}
          <div className="space-y-2">
            <input
              type="text"
              placeholder="Motivo del cierre manual (obligatorio)"
              value={draft}
              onChange={(e) => onDraftChange(e.target.value)}
              maxLength={2000}
              className="bg-surface-3/40 ring-1 ring-white/[0.06] focus:ring-brand/40 focus:outline-none rounded-button px-3 py-2 text-caption text-white placeholder:text-gray-600 w-full"
            />
            <div className="flex justify-end">
              <button
                onClick={onResolve}
                disabled={resolving || !draft.trim()}
                className="bg-white/[0.07] hover:bg-white/[0.12] text-white text-caption font-medium px-3 py-1.5 rounded-button disabled:opacity-50 transition-colors duration-brand"
              >
                {resolving ? "Guardando…" : "Marcar resuelto sin publicar"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

const PROPOSAL_LABELS = {
  ready: "Lista para revisar",
  partial: "Propuesta parcial",
  needs_input: "Necesita intervención",
  applied: "Aplicada",
  partially_applied: "Aplicada parcialmente",
  stale: "Desactualizada",
  dismissed: "Descartada",
};

const MANUAL_LABELS = {
  timing_review: "Revisar timing en el editor",
  structure_review: "Revisar estructura de líneas",
  background_review: "Cambio de fondo manual",
  audio_review: "Verificar identidad del audio",
  manual_review: "Interpretación manual requerida",
};

function samePreviewSegment(left, right) {
  if (!left || !right) return false;
  if (left._id != null && right._id != null) {
    return String(left._id) === String(right._id);
  }
  return (
    Math.abs(Number(left.start || 0) - Number(right.start || 0)) < 0.000001
    && Math.abs(Number(left.end || 0) - Number(right.end || 0)) < 0.000001
    && String(left.text || "") === String(right.text || "")
  );
}

export function buildLyricsPreview(
  segments = [], operations = [], selected = [], textDrafts = {},
) {
  const selectedIds = new Set(selected.map(String));
  const replacements = operations.filter((operation) => (
    operation?.applicable
    && operation.status === "pending"
    && selectedIds.has(String(operation.id))
    && operation.current_segments?.length >= 1
    && operation.proposed_segments?.length === 1
  ));
  const rows = [];
  for (let index = 0; index < segments.length; index += 1) {
    const segment = segments[index];
    const operation = replacements.find((candidate) => {
      const currentRows = candidate.current_segments || [];
      if (!samePreviewSegment(segment, currentRows[0])) return false;
      return currentRows.every((row, offset) => (
        samePreviewSegment(segments[index + offset], row)
      ));
    });
    const currentRows = operation?.current_segments || [segment];
    const currentText = String(segment?.text || "");
    const resultText = operation
      ? String(
        textDrafts[operation.id]
        ?? operation.proposed_segments?.[0]?.text
        ?? currentRows.map((row) => row?.text || "").join(" "),
      )
      : currentText;
    rows.push({
      key: segment?._id != null
        ? `segment-${segment._id}`
        : `segment-${index}-${segment?.start}-${segment?.end}`,
      start: Number(segment?.start || 0),
      currentText: operation
        ? currentRows.map((row) => String(row?.text || "")).join(" / ")
        : currentText,
      resultText,
      changed: Boolean(operation) && (
        currentRows.length > 1 || resultText !== currentText
      ),
      operationId: operation?.id || null,
    });
    if (operation) index += currentRows.length - 1;
  }
  return rows;
}

function previewTimestamp(value) {
  const seconds = Math.max(0, Math.round(Number(value) || 0));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

function LyricsProposalPreview({
  requestComment, lyricsContext, operations, selected, textDrafts,
}) {
  if (!lyricsContext?.segments?.length) {
    return (
      <div className="rounded-button bg-amber-500/10 ring-1 ring-amber-400/20 p-3">
        <p className="text-caption text-amber-100 font-medium">
          No pudimos cargar la letra completa para esta propuesta.
        </p>
        <p className="text-label text-amber-100/70 mt-1">
          Recalculá antes de aplicar para revisar el resultado con contexto.
        </p>
      </div>
    );
  }
  if (lyricsContext.matches_base === false) {
    return (
      <div className="rounded-button bg-amber-500/10 ring-1 ring-amber-400/20 p-3">
        <p className="text-caption text-amber-100 font-medium">
          La letra cambió después de generar esta propuesta.
        </p>
        <p className="text-label text-amber-100/70 mt-1">
          Recalculá para comparar el pedido con la revisión actual.
        </p>
      </div>
    );
  }

  const rows = buildLyricsPreview(
    lyricsContext.segments, operations, selected, textDrafts,
  );
  const changedCount = rows.filter((row) => row.changed).length;
  return (
    <div
      aria-label="Vista previa de la letra resultante"
      className="rounded-button bg-black/20 ring-1 ring-white/[0.08] overflow-hidden"
    >
      <div className="p-3 border-b border-white/[0.08] space-y-2">
        <div className="flex items-start justify-between gap-3 flex-wrap">
          <div>
            <p className="text-caption font-semibold text-white">
              Así quedaría la letra completa
            </p>
            <p className="text-label text-gray-400">
              Vista previa solamente · todavía no modifica el editor
            </p>
          </div>
          <span className="text-label text-emerald-200 bg-emerald-500/10 ring-1 ring-emerald-400/20 px-2 py-1 rounded-button">
            {changedCount} cambio(s) seleccionado(s)
          </span>
        </div>
        <div className="rounded-button bg-surface-2/50 p-2 ring-1 ring-white/[0.05]">
          <p className="text-label uppercase tracking-wider text-gray-500 mb-1">
            Pedido original
          </p>
          <p className="text-label text-gray-200 whitespace-pre-wrap font-mono leading-relaxed">
            {requestComment}
          </p>
        </div>
      </div>
      <div className="max-h-96 overflow-y-auto divide-y divide-white/[0.04]">
        {rows.map((row) => (
          <div
            key={row.key}
            data-testid={`lyrics-preview-${row.key}`}
            className={`grid grid-cols-[3rem_minmax(0,1fr)] gap-2 px-3 py-2 ${
              row.changed ? "bg-emerald-500/[0.08]" : ""
            }`}
          >
            <span className="text-label font-mono text-gray-600 pt-0.5">
              {previewTimestamp(row.start)}
            </span>
            <div className="min-w-0">
              {row.changed && (
                <p className="text-label text-gray-500 line-through break-words">
                  {row.currentText}
                </p>
              )}
              <p className={`text-caption break-words ${
                row.changed ? "text-emerald-200 font-medium" : "text-gray-300"
              }`}>
                {row.resultText}
              </p>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function ChangeRequestProposal({
  summary, proposal, requestComment, busy, applyEnabled, jobId,
  onGenerate, onLoad, onAdjust, onApply, onDismiss, onRegenerateBackground,
}) {
  const operations = proposal?.operations || [];
  const applicable = operations.filter(
    (operation) => operation.applicable && operation.status === "pending",
  );
  const [selected, setSelected] = useState([]);
  const [textDrafts, setTextDrafts] = useState({});
  const [backgroundDrafts, setBackgroundDrafts] = useState({});

  useEffect(() => {
    setSelected(applicable.map((operation) => operation.id));
    setTextDrafts(Object.fromEntries(applicable.map((operation) => [
      operation.id,
      operation.proposed_segments?.[0]?.text || "",
    ])));
    setBackgroundDrafts(Object.fromEntries(operations
      .filter((operation) => operation.visual_action === "regenerate_background")
      .map((operation) => [operation.id, operation.suggested_prompt || ""])));
  }, [proposal?.id, proposal?.updated_at]); // eslint-disable-line react-hooks/exhaustive-deps

  const hasUnsavedDrafts = applicable.some((operation) => (
    operation.current_segments?.length === 1
    && operation.proposed_segments?.length === 1
    && (textDrafts[operation.id] ?? operation.proposed_segments?.[0]?.text ?? "")
      !== (operation.proposed_segments?.[0]?.text ?? "")
  ));

  const effective = proposal || summary;
  const status = effective?.status;
  const editorUrl = proposal?.editor_url
    || (jobId ? `/videos/${jobId}/edit-lyrics` : null);

  if (!effective) {
    return (
      <div className="mt-3 rounded-button bg-brand/5 ring-1 ring-brand/20 p-3">
        <p className="text-label text-gray-300 mb-2">
          El asistente puede convertir timestamps y reemplazos explícitos en un diff revisable.
        </p>
        <button
          type="button"
          onClick={onGenerate}
          disabled={busy}
          className="bg-brand hover:bg-brand-light text-white text-caption font-medium px-3 py-1.5 rounded-button disabled:opacity-50"
        >
          {busy ? "Analizando…" : "Analizar pedido"}
        </button>
      </div>
    );
  }

  if (!proposal) {
    return (
      <div className="mt-3 rounded-button bg-brand/5 ring-1 ring-brand/20 p-3 flex items-center justify-between gap-3">
        <div>
          <p className="text-caption font-semibold text-brand-light">
            {PROPOSAL_LABELS[status] || status}
          </p>
          <p className="text-label text-gray-400">
            {summary.visual_action_count
              ? `${summary.visual_action_count} fondo(s) listo(s) para regenerar`
              : `${summary.applicable_count || 0} cambio(s) aplicable(s)`}
          </p>
        </div>
        <button
          type="button"
          onClick={status === "stale" || status === "dismissed" ? onGenerate : onLoad}
          disabled={busy}
          className="bg-white/[0.07] hover:bg-white/[0.12] text-white text-caption px-3 py-1.5 rounded-button disabled:opacity-50"
        >
          {busy
            ? "Cargando…"
            : status === "stale" || status === "dismissed"
              ? "Recalcular"
              : "Ver propuesta"}
        </button>
      </div>
    );
  }

  const toggle = (id) => setSelected((current) => (
    current.includes(id) ? current.filter((value) => value !== id) : [...current, id]
  ));

  return (
    <div className="mt-3 rounded-button bg-brand/5 ring-1 ring-brand/20 p-3 space-y-3">
      <div className="flex items-center justify-between gap-3">
        <div>
          <p className="text-caption font-semibold text-brand-light">
            {PROPOSAL_LABELS[status] || status}
          </p>
          <p className="text-label text-gray-400">
            Basada en la revisión {proposal.base_revision}
          </p>
        </div>
        {(status === "ready" || status === "partial" || status === "needs_input") && (
          <button
            type="button"
            onClick={() => onDismiss(proposal.id)}
            disabled={busy}
            className="text-label text-gray-400 hover:text-gray-200 disabled:opacity-50"
          >
            Descartar
          </button>
        )}
      </div>

      {operations.map((operation) => {
        const currentText = (operation.current_segments || [])
          .map((row) => row?.text || "")
          .join(" / ");
        const proposedText = textDrafts[operation.id]
          ?? operation.proposed_segments?.[0]?.text ?? "";
        if (!operation.applicable) {
          if (operation.visual_action === "regenerate_background") {
            const backgroundPrompt = backgroundDrafts[operation.id]
              ?? operation.suggested_prompt ?? "";
            const supported = operation.regeneration_supported !== false;
            return (
              <div
                key={operation.id}
                className="rounded-button bg-sky-500/10 ring-1 ring-sky-400/25 p-3 space-y-3"
              >
                <div>
                  <p className="text-caption text-sky-100 font-semibold">
                    Fondo nuevo sugerido
                  </p>
                  <p className="text-label text-sky-100/70 mt-1">
                    Revisá y ajustá el prompt. Regenerar inicia un render, pero no publica ni cierra el pedido.
                  </p>
                </div>
                {operation.current_prompt && (
                  <div className="rounded-button bg-black/20 p-2 ring-1 ring-white/[0.05]">
                    <p className="text-label uppercase tracking-wider text-gray-500 mb-1">
                      Prompt usado hasta ahora
                    </p>
                    <p className="text-label text-gray-300 whitespace-pre-wrap">
                      {operation.current_prompt}
                    </p>
                  </div>
                )}
                <label className="block">
                  <span className="text-label text-gray-300">Prompt para rehacer el fondo</span>
                  <textarea
                    aria-label="Prompt sugerido para el fondo"
                    value={backgroundPrompt}
                    onChange={(event) => setBackgroundDrafts((current) => ({
                      ...current, [operation.id]: event.target.value,
                    }))}
                    maxLength={4000}
                    rows={6}
                    className="mt-1 w-full bg-surface-3/60 ring-1 ring-white/[0.08] focus:ring-sky-400/40 focus:outline-none rounded-button px-3 py-2 text-caption text-white leading-relaxed"
                  />
                </label>
                <div className="flex items-center justify-between gap-3 flex-wrap">
                  <p className="text-label text-gray-400">
                    {operation.background_mode === "imagen"
                      ? "Modo actual: Imagen animada"
                      : "Modo actual: Video IA"}
                    {" · validación de contenido obligatoria"}
                  </p>
                  {supported ? (
                    <button
                      type="button"
                      onClick={() => onRegenerateBackground(
                        proposal.id,
                        operation.id,
                        backgroundPrompt,
                        operation.background_mode,
                      )}
                      disabled={busy || !backgroundPrompt.trim()}
                      className="bg-sky-500 hover:bg-sky-400 text-white text-caption font-semibold px-3 py-2 rounded-button disabled:opacity-40"
                    >
                      {busy ? "Iniciando…" : "Regenerar fondo con este prompt"}
                    </button>
                  ) : (
                    <div>
                      <p className="text-label text-amber-200">
                        Este video usa varias escenas; el cambio debe hacerse escena por escena.
                      </p>
                      {editorUrl && (
                        <a
                          href={editorUrl}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="inline-flex mt-2 bg-white/[0.08] hover:bg-white/[0.14] text-white text-label font-medium px-2.5 py-1.5 rounded-button"
                        >
                          Abrir editor de escenas
                        </a>
                      )}
                    </div>
                  )}
                </div>
              </div>
            );
          }
          return (
            <div key={operation.id} className="rounded-button bg-amber-500/10 ring-1 ring-amber-400/20 p-2">
              <p className="text-caption text-amber-200">
                {MANUAL_LABELS[operation.kind] || "Revisión manual"}
              </p>
              {operation.timecode_seconds != null && (
                <p className="text-label text-gray-400">Cerca de {Math.floor(operation.timecode_seconds / 60)}:{String(Math.round(operation.timecode_seconds % 60)).padStart(2, "0")}</p>
              )}
              {operation.kind === "background_review" && editorUrl && (
                <a
                  href={editorUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex mt-2 bg-white/[0.08] hover:bg-white/[0.14] text-white text-label font-medium px-2.5 py-1.5 rounded-button"
                >
                  Abrir editor de fondo
                </a>
              )}
            </div>
          );
        }
        const textEditable = (
          operation.current_segments?.length === 1
          && operation.proposed_segments?.length === 1
        );
        return (
          <div key={operation.id} className="block rounded-button bg-black/20 ring-1 ring-white/[0.06] p-2">
            <div className="flex items-start gap-2">
              {operation.status === "pending" && (
                <input
                  type="checkbox"
                  aria-label={`Seleccionar cambio: ${currentText}`}
                  checked={selected.includes(operation.id)}
                  onChange={() => toggle(operation.id)}
                  className="mt-1"
                />
              )}
              <div className="min-w-0 flex-1">
                <p className="text-label text-gray-500 line-through break-words">{currentText}</p>
                {operation.status === "pending" && textEditable ? (
                  <div className="mt-1 flex gap-2">
                    <input
                      value={proposedText}
                      onChange={(event) => setTextDrafts((current) => ({
                        ...current, [operation.id]: event.target.value,
                      }))}
                      className="min-w-0 flex-1 bg-surface-3/60 ring-1 ring-white/[0.08] rounded-button px-2 py-1 text-caption text-emerald-200"
                    />
                    {proposedText !== (operation.proposed_segments?.[0]?.text || "") && (
                      <button
                        type="button"
                        onClick={() => onAdjust(
                          proposal.id, operation.id, proposedText, proposal.base_revision,
                        )}
                        disabled={busy || !proposedText.trim()}
                        className="text-label text-brand-light disabled:opacity-50"
                      >
                        Guardar
                      </button>
                    )}
                  </div>
                ) : (
                  <p className="text-caption text-emerald-200 break-words">{proposedText}</p>
                )}
                <p className="text-label text-gray-500 mt-1">
                  {operation.operator_adjusted
                    ? "Ajustado por operador"
                    : operation.kind === "remove_terminal_period"
                      ? "Formato determinístico"
                      : operation.kind === "merge_phrase"
                        ? "Frase completa en una sola pantalla"
                      : "Pedido explícito del cliente"}
                  {operation.scope === "all_matching" ? " · todas las apariciones" : ""}
                  {operation.status === "applied" ? " · aplicado" : ""}
                </p>
              </div>
            </div>
          </div>
        );
      })}

      {(status === "ready" || status === "partial" || status === "needs_input") && (
        <LyricsProposalPreview
          requestComment={requestComment}
          lyricsContext={proposal.lyrics_context}
          operations={operations}
          selected={selected}
          textDrafts={textDrafts}
        />
      )}

      {hasUnsavedDrafts && (
        <p className="text-label text-amber-200">
          Guardá los ajustes de texto antes de aplicar la propuesta.
        </p>
      )}

      <div className="flex flex-wrap gap-2">
        {(status === "ready" || status === "partial") && applicable.length > 0 && (
          <button
            type="button"
            onClick={() => onApply(proposal.id, selected, proposal.base_revision)}
            disabled={(
              busy || !applyEnabled || selected.length === 0 || hasUnsavedDrafts
            )}
            title={
              !applyEnabled
                ? "La aplicación está deshabilitada por configuración"
                : hasUnsavedDrafts
                  ? "Guardá los ajustes de texto antes de aplicar"
                  : undefined
            }
            className="bg-brand hover:bg-brand-light text-white text-caption font-medium px-3 py-1.5 rounded-button disabled:opacity-40"
          >
            {busy ? "Aplicando…" : `Aplicar seleccionadas (${selected.length})`}
          </button>
        )}
        {(status === "applied" || status === "partially_applied") && editorUrl && (
          <a
            href={editorUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="bg-brand hover:bg-brand-light text-white text-caption font-medium px-3 py-1.5 rounded-button"
          >
            Revisar y re-renderizar
          </a>
        )}
        {(status === "stale" || status === "dismissed" || status === "partially_applied") && (
          <button
            type="button"
            onClick={onGenerate}
            disabled={busy}
            className="bg-white/[0.07] text-white text-caption px-3 py-1.5 rounded-button disabled:opacity-50"
          >
            {status === "partially_applied" ? "Recalcular pendientes" : "Recalcular"}
          </button>
        )}
      </div>
    </div>
  );
}
