// Workspace operativo de pedidos de cambio de UMG
// (delivery_change_requests). Una cola compacta mantiene el contexto y el
// panel de detalle guía el caso por cinco etapas: interpretar, aplicar,
// renderizar, revisar y publicar. El pedido seleccionado queda en la URL
// para que el editor pueda devolver al operador al mismo punto del flujo.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { fmtDate, fmtAgo } from "../../adminApi";
import FilterBar from "../../primitives/FilterBar";
import EmptyState from "../../primitives/EmptyState";
import TableSkeleton from "../../primitives/TableSkeleton";
import EnableProResModal from "../../../EnableProResModal";
import ChangeRequestQueue from "./ChangeRequestQueue";
import RequestWorkflowStepper from "./RequestWorkflowStepper";
import {
  PORTAL_LABELS,
  requestSearchText,
  requestWorkflow,
  workflowMatchesFilter,
} from "./changeRequestWorkflow";

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
export function publicationStatus(publication) {
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

function editorUrlWithRequest(jobId, requestId, suppliedUrl) {
  const rawUrl = suppliedUrl || (jobId ? `/videos/${jobId}/edit-lyrics` : null);
  if (!rawUrl || requestId == null || /[?&]change_request_id=/.test(rawUrl)) return rawUrl;
  const hashIndex = rawUrl.indexOf("#");
  const path = hashIndex >= 0 ? rawUrl.slice(0, hashIndex) : rawUrl;
  const hash = hashIndex >= 0 ? rawUrl.slice(hashIndex) : "";
  const separator = path.includes("?") ? "&" : "?";
  return `${path}${separator}change_request_id=${encodeURIComponent(requestId)}${hash}`;
}

export default function ChangeRequestsPanel({
  changeRequests,
  crStatusFilter,
  setCrStatusFilter,
  crPendingCount,
  crResolvedCount,
  crLoading,
  crResolvingId,
  resolveChangeRequest,
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
  const [search, setSearch] = useState("");
  const [stageFilter, setStageFilter] = useState("all");
  const searchInputRef = useRef(null);
  const initialRequestId = useMemo(() => {
    if (typeof window === "undefined") return null;
    const params = new URLSearchParams(window.location.search);
    return params.get("change_request_id") || params.get("request");
  }, []);
  const [selectedId, setSelectedId] = useState(initialRequestId);
  const [returnNotice, setReturnNotice] = useState(() => {
    if (typeof window === "undefined") return null;
    return new URLSearchParams(window.location.search).get("render_submitted") === "1"
      ? "El render corregido fue enviado. Podés seguir su progreso desde este pedido; el portal conserva el corte anterior hasta que lo publiques."
      : null;
  });
  const setDraft = (id, val) => setDrafts((d) => ({ ...d, [id]: val }));

  const filterOptions = [
    { id: "pending", label: "Pendientes", badge: crPendingCount },
    { id: "resolved", label: "Resueltos", badge: crResolvedCount },
    { id: "all", label: "Todos" },
  ];

  const filteredRequests = useMemo(() => {
    const query = search.trim().toLocaleLowerCase("es");
    return changeRequests.filter((item) => {
      if (query && !requestSearchText(item).includes(query)) return false;
      const workflow = requestWorkflow(item, proposalDetails[item.id], proposalEnabled);
      return workflowMatchesFilter(workflow, stageFilter);
    });
  }, [changeRequests, proposalDetails, proposalEnabled, search, stageFilter]);

  const selectedItem = useMemo(() => (
    filteredRequests.find((item) => String(item.id) === String(selectedId))
    || filteredRequests[0]
    || null
  ), [filteredRequests, selectedId]);

  const selectRequest = useCallback((id) => {
    setSelectedId(id);
    if (typeof window !== "undefined") {
      const url = new URL(window.location.href);
      url.searchParams.set("change_request_id", id);
      url.searchParams.delete("request");
      window.history.replaceState(window.history.state, "", url);
    }
  }, []);

  useEffect(() => {
    if (selectedItem && String(selectedItem.id) !== String(selectedId)) {
      selectRequest(selectedItem.id);
    }
  }, [selectRequest, selectedId, selectedItem]);

  useEffect(() => {
    const handleKeyDown = (event) => {
      if (event.defaultPrevented) return;
      const target = event.target;
      const typing = target instanceof HTMLInputElement
        || target instanceof HTMLTextAreaElement
        || target instanceof HTMLSelectElement
        || target?.isContentEditable;
      if (event.key === "/" && !typing) {
        event.preventDefault();
        searchInputRef.current?.focus();
        return;
      }
      if (typing || !["j", "k", "ArrowDown", "ArrowUp"].includes(event.key)) return;
      if (!filteredRequests.length) return;
      event.preventDefault();
      const currentIndex = Math.max(0, filteredRequests.findIndex(
        (item) => String(item.id) === String(selectedItem?.id),
      ));
      const direction = event.key === "j" || event.key === "ArrowDown" ? 1 : -1;
      const nextIndex = Math.min(
        filteredRequests.length - 1,
        Math.max(0, currentIndex + direction),
      );
      selectRequest(filteredRequests[nextIndex].id);
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [filteredRequests, selectRequest, selectedItem?.id]);

  const publishItem = useCallback((item) => {
    const status = publicationStatus(item.publication);
    if (status.needsProResSetup) {
      setProResSetup({
        jobId: item.delivery?.job_id,
        requestId: item.id,
        frameSize: item.delivery?.frame_size,
      });
      return;
    }
    publishDeliveryUpdate(item.delivery?.job_id, item.delivery?.portal_id, item.id);
  }, [publishDeliveryUpdate]);

  useEffect(() => {
    if (!returnNotice || typeof window === "undefined") return;
    const url = new URL(window.location.href);
    url.searchParams.delete("render_submitted");
    window.history.replaceState(window.history.state, "", url);
  }, [returnNotice]);

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

      {returnNotice && (
        <div role="status" className="flex items-start justify-between gap-3 rounded-xl bg-sky-500/[0.08] p-3 text-caption text-sky-100 ring-1 ring-sky-400/20">
          <span>{returnNotice}</span>
          <button type="button" onClick={() => setReturnNotice(null)} className="shrink-0 text-label opacity-70 hover:opacity-100">
            Cerrar
          </button>
        </div>
      )}

      {crPublishNotice && (
        <div
          role="status"
          className={`rounded-card p-3 text-caption ring-1 flex items-start justify-between gap-3 ${
            TONE_STYLES[crPublishNotice.tone === "ok" ? "ok" : "wait"]
          }`}
        >
          <span>{crPublishNotice.text}</span>
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
      ) : changeRequests.length === 0 ? (
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
        <div className="grid min-w-0 gap-4 xl:grid-cols-[minmax(17rem,0.72fr)_minmax(0,2.28fr)]">
          <ChangeRequestQueue
            items={filteredRequests}
            allItems={changeRequests}
            selectedId={selectedItem?.id}
            onSelect={selectRequest}
            proposals={proposalDetails}
            proposalEnabled={proposalEnabled}
            search={search}
            onSearchChange={setSearch}
            stageFilter={stageFilter}
            onStageFilterChange={setStageFilter}
            searchInputRef={searchInputRef}
          />
          {selectedItem ? (
            <ChangeRequestCard
              key={selectedItem.id}
              item={selectedItem}
              draft={drafts[selectedItem.id] || ""}
              onDraftChange={(value) => setDraft(selectedItem.id, value)}
              resolving={crResolvingId === selectedItem.id}
              publishing={crPublishingId === selectedItem.id}
              onResolve={() => resolveChangeRequest(selectedItem.id, drafts[selectedItem.id])}
              onReopen={() => reopenChangeRequest(selectedItem.id)}
              onPublish={() => publishItem(selectedItem)}
              proposalEnabled={proposalEnabled}
              proposalApplyEnabled={proposalApplyEnabled}
              proposalBusy={proposalBusyId === selectedItem.id}
              proposal={proposalDetails[selectedItem.id] || null}
              onGenerateProposal={() => generateProposal(selectedItem.id)}
              onLoadProposal={() => loadProposal(selectedItem.id)}
              onAdjustProposal={(proposalId, operationId, requestedText, baseRevision) =>
                adjustProposal(
                  selectedItem.id, proposalId, operationId, requestedText, baseRevision,
                )
              }
              onApplyProposal={(proposalId, operationIds, baseRevision) =>
                applyProposal(selectedItem.id, proposalId, operationIds, baseRevision)
              }
              onDismissProposal={(proposalId) => dismissProposal(selectedItem.id, proposalId)}
              onRegenerateBackground={(proposalId, operationId, prompt, backgroundMode) =>
                regenerateBackground(
                  selectedItem.id, proposalId, operationId, selectedItem.delivery?.job_id,
                  prompt, backgroundMode,
                )
              }
            />
          ) : (
            <div className="rounded-2xl bg-surface-2/30 p-8 text-center ring-1 ring-white/[0.06]">
              <p className="text-ui font-semibold text-white">No hay pedidos con estos filtros</p>
              <p className="mt-1 text-caption text-gray-500">Limpiá la búsqueda o elegí otra etapa.</p>
            </div>
          )}
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
  item, draft, onDraftChange, resolving, publishing,
  onResolve, onReopen, onPublish,
  proposalEnabled, proposalApplyEnabled, proposalBusy, proposal,
  onGenerateProposal, onLoadProposal, onAdjustProposal, onApplyProposal,
  onDismissProposal, onRegenerateBackground,
}) {
  const d = item.delivery || {};
  const isResolved = !!item.resolved_at;
  const status = publicationStatus(item.publication);
  const workflow = requestWorkflow(item, proposal, proposalEnabled);
  const videoRef = useRef(null);
  const proposalRef = useRef(null);
  // Un pedido resuelto AL PUBLICAR no necesita que nadie confirme nada: la
  // corrección ya está en el portal. Uno cerrado a mano sí se explica.
  const closedByPublication = item.resolution_source === "publication";

  const seekVideo = useCallback((seconds) => {
    const video = videoRef.current;
    if (!video) return;
    video.currentTime = Math.max(0, Number(seconds) || 0);
    video.play?.().catch?.(() => {});
    video.scrollIntoView?.({ behavior: "smooth", block: "center" });
  }, []);

  const effectiveProposal = proposal || item.proposal;
  const editorUrl = editorUrlWithRequest(d.job_id, item.id, effectiveProposal?.editor_url);
  let primaryAction;
  if (isResolved) {
    primaryAction = { label: resolving ? "Reabriendo…" : "Reabrir pedido", onClick: onReopen, disabled: resolving };
  } else if (status.canPublish) {
    primaryAction = {
      label: publishing ? "Publicando…" : status.publishLabel || "Publicar actualización",
      onClick: onPublish,
      disabled: publishing,
    };
  } else if (workflow.key === "rendering") {
    primaryAction = { label: "Generando corte nuevo…", disabled: true };
  } else if (proposalEnabled && !effectiveProposal) {
    primaryAction = { label: proposalBusy ? "Analizando…" : "Analizar pedido", onClick: onGenerateProposal, disabled: proposalBusy };
  } else if (proposalEnabled && item.proposal && !proposal) {
    primaryAction = { label: proposalBusy ? "Cargando…" : "Ver propuesta", onClick: onLoadProposal, disabled: proposalBusy };
  } else if (["ready", "partial", "needs_input"].includes(proposal?.status)) {
    primaryAction = {
      label: "Revisar propuesta",
      onClick: () => proposalRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }),
    };
  } else if (["applied", "partially_applied"].includes(proposal?.status) && d.job_id) {
    primaryAction = { label: "Revisar y generar corte", href: editorUrl };
  } else if (d.job_id) {
    primaryAction = { label: "Editar letra", href: editorUrl };
  } else {
    primaryAction = { label: "Sin acción disponible", disabled: true };
  }

  useEffect(() => {
    const handleShortcut = (event) => {
      if (!(event.metaKey || event.ctrlKey) || event.key !== "Enter") return;
      if (primaryAction.disabled) return;
      event.preventDefault();
      if (primaryAction.onClick) primaryAction.onClick();
      else if (primaryAction.href) window.location.assign(primaryAction.href);
    };
    window.addEventListener("keydown", handleShortcut);
    return () => window.removeEventListener("keydown", handleShortcut);
  }, [primaryAction.disabled, primaryAction.href, primaryAction.onClick]);

  return (
    <article className="min-w-0 rounded-2xl bg-surface-2/25 ring-1 ring-white/[0.07]">
      {/* Contexto del delivery */}
      <header className="flex flex-wrap items-start justify-between gap-4 border-b border-white/[0.06] px-4 py-4 sm:px-5">
        <div className="min-w-0">
          <p className="text-label uppercase tracking-[0.18em] text-brand-light font-bold mb-1">
            {d.artist || "(sin artista)"}
          </p>
          <h3 className="text-xl font-bold leading-tight text-white">
            {d.song || "(canción eliminada)"}
          </h3>
          <div className="flex items-center gap-2 mt-2 flex-wrap text-label text-gray-500">
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
          className={`text-label font-semibold px-2.5 py-1 rounded-full shrink-0 ring-1 ${
            isResolved ? "bg-emerald-500/15 text-emerald-300" : "bg-amber-500/15 text-amber-300"
          }`}
        >
          {isResolved ? "Resuelto" : "Pendiente"}
        </span>
      </header>

      <div className="grid min-w-0 gap-5 p-4 sm:p-5 lg:grid-cols-[minmax(18rem,0.9fr)_minmax(0,1.1fr)]">
        <div className="min-w-0 space-y-4 lg:sticky lg:top-4 lg:self-start">
          <RequestWorkflowStepper workflow={workflow} />

          <div className="overflow-hidden rounded-2xl bg-black/30 ring-1 ring-white/[0.08]">
            {d.video_url ? (
              <video
                ref={videoRef}
                src={d.video_url}
                poster={d.thumbnail_url || undefined}
                controls
                preload="metadata"
                className="aspect-video max-h-[25rem] w-full bg-black object-contain"
                aria-label={`Video de ${d.artist || "artista"} — ${d.song || "canción"}`}
              />
            ) : d.thumbnail_url ? (
              <img
                src={d.thumbnail_url}
                alt="Preview del video"
                loading="lazy"
                className="aspect-video max-h-[25rem] w-full bg-black object-contain"
              />
            ) : (
              <div className="flex aspect-video items-center justify-center text-caption text-gray-600">
                Sin preview disponible
              </div>
            )}
            {(d.video_url || d.thumbnail_url) && (
              <div className="flex items-center justify-between gap-3 border-t border-white/[0.06] px-3 py-2">
                <span className="text-label text-gray-500">Corte actual del operador</span>
                <a
                  href={d.video_url || d.thumbnail_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-label text-brand-light hover:text-white"
                >
                  Abrir original ↗
                </a>
              </div>
            )}
          </div>

          <div className={`rounded-xl ring-1 p-3 ${TONE_STYLES[status.tone]}`}>
            <p className="text-caption font-semibold">{status.title}</p>
            <p className="text-label opacity-80 mt-0.5 leading-relaxed">{status.detail}</p>
            {item.publication?.stale_since && (
              <p className="text-label opacity-70 mt-1">
                Cambios en curso desde {fmtAgo(item.publication.stale_since)}.
              </p>
            )}
          </div>
        </div>

        <div className="min-w-0 space-y-4">
          <section className="rounded-2xl bg-white/[0.025] p-4 ring-1 ring-white/[0.07]">
            <div className="flex items-center justify-between gap-3">
              <p className="text-label font-semibold uppercase tracking-[0.16em] text-gray-500">Pedido original</p>
              <span className="text-[10px] text-gray-600">{fmtAgo(item.submitted_at)}</span>
            </div>
            <p className="mt-3 whitespace-pre-wrap text-ui leading-relaxed text-gray-100">
              {item.comment}
            </p>
            <p className="mt-3 text-label text-gray-600">Enviado el {fmtDate(item.submitted_at)}</p>
          </section>

          {!isResolved && proposalEnabled && (
            <div ref={proposalRef} className="scroll-mt-4">
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
                onSeek={seekVideo}
                requestId={item.id}
              />
            </div>
          )}

          {isResolved ? (
            <div className="rounded-2xl bg-emerald-500/[0.06] p-4 text-label text-gray-400 ring-1 ring-emerald-400/15">
              <span className="text-emerald-300 font-medium">
                {closedByPublication
                  ? `Resuelto al publicar la versión ${item.resolved_by_revision}`
                  : "Resuelto"}
              </span>
              {!closedByPublication && item.resolved_by && <> por <b>{item.resolved_by}</b></>}
              {" "}el {fmtDate(item.resolved_at)}
              {item.resolution_note && (
                <p className="mt-2 whitespace-pre-wrap text-gray-300">
                  <span className="text-gray-500">Respuesta: </span>{item.resolution_note}
                </p>
              )}
            </div>
          ) : (
            <details className="group rounded-xl bg-white/[0.02] ring-1 ring-white/[0.06]">
              <summary className="cursor-pointer list-none px-4 py-3 text-label text-gray-500 hover:text-gray-300">
                Cerrar manualmente o dejar una respuesta
                <span className="float-right transition-transform group-open:rotate-180">⌄</span>
              </summary>
              <div className="space-y-2 border-t border-white/[0.06] p-4">
                <p className="text-label text-gray-500">
                  Usalo sólo cuando el pedido no requiera publicar un corte nuevo.
                </p>
                <input
                  type="text"
                  placeholder="Respuesta opcional"
                  value={draft}
                  onChange={(event) => onDraftChange(event.target.value)}
                  maxLength={2000}
                  className="w-full rounded-xl bg-surface-3/40 px-3 py-2 text-caption text-white ring-1 ring-white/[0.06] placeholder:text-gray-600 focus:outline-none focus:ring-brand/40"
                />
                <div className="flex justify-end">
                  <button
                    onClick={onResolve}
                    disabled={resolving}
                    className="rounded-lg bg-white/[0.07] px-3 py-1.5 text-caption font-medium text-white hover:bg-white/[0.12] disabled:opacity-50"
                  >
                    {resolving ? "Guardando…" : "Marcar resuelto sin publicar"}
                  </button>
                </div>
              </div>
            </details>
          )}
        </div>
      </div>

      <footer className="sticky bottom-0 z-10 flex flex-wrap items-center justify-between gap-3 rounded-b-2xl border-t border-white/[0.08] bg-surface-2/95 px-4 py-3 shadow-[0_-18px_40px_rgba(0,0,0,0.24)] backdrop-blur sm:px-5">
        <div className="min-w-0">
          <p className="text-caption font-semibold text-white">{workflow.label}</p>
          <p className="truncate text-label text-gray-500">
            {isResolved ? "Podés reabrirlo si el cliente necesita otra corrección." : "Acción recomendada para este pedido"}
          </p>
        </div>
        <div className="flex items-center gap-2">
          {!isResolved && d.job_id && primaryAction.label !== "Editar letra" && (
            <a
              href={editorUrlWithRequest(d.job_id, item.id)}
              className="rounded-xl px-3 py-2 text-caption font-medium text-gray-300 hover:bg-white/[0.06] hover:text-white"
            >
              Editar letra
            </a>
          )}
          {primaryAction.href ? (
            <a
              href={primaryAction.href}
              className="rounded-xl bg-brand px-4 py-2.5 text-caption font-semibold text-white shadow-lg shadow-brand/15 hover:bg-brand-light"
            >
              {primaryAction.label}
            </a>
          ) : (
            <button
              type="button"
              aria-label={primaryAction.label}
              onClick={primaryAction.onClick}
              disabled={primaryAction.disabled}
              className="rounded-xl bg-brand px-4 py-2.5 text-caption font-semibold text-white shadow-lg shadow-brand/15 hover:bg-brand-light disabled:cursor-not-allowed disabled:opacity-45"
            >
              {primaryAction.label}
              {!primaryAction.disabled && <span className="ml-2 hidden text-[10px] opacity-60 sm:inline">⌘↵</span>}
            </button>
          )}
        </div>
      </footer>
    </article>
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
  requestComment, lyricsContext, operations, selected, textDrafts, onSeek,
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
            <button
              type="button"
              onClick={() => onSeek?.(row.start)}
              disabled={!onSeek}
              className="pt-0.5 text-left text-label font-mono text-gray-500 hover:text-brand-light disabled:cursor-default disabled:hover:text-gray-500"
              title={onSeek ? "Reproducir desde este momento" : undefined}
            >
              {previewTimestamp(row.start)}
            </button>
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
  summary, proposal, requestComment, busy, applyEnabled, jobId, requestId,
  onGenerate, onLoad, onAdjust, onApply, onDismiss, onRegenerateBackground, onSeek,
}) {
  const operations = proposal?.operations || [];
  const applicable = operations.filter(
    (operation) => operation.applicable && operation.status === "pending",
  );
  const [selected, setSelected] = useState([]);
  const [textDrafts, setTextDrafts] = useState({});
  const [backgroundDrafts, setBackgroundDrafts] = useState({});
  const [saveStates, setSaveStates] = useState({});
  const saveTimersRef = useRef(new Map());

  useEffect(() => {
    setSelected(applicable.map((operation) => operation.id));
    setTextDrafts(Object.fromEntries(applicable.map((operation) => [
      operation.id,
      operation.proposed_segments?.[0]?.text || "",
    ])));
    setBackgroundDrafts(Object.fromEntries(operations
      .filter((operation) => operation.visual_action === "regenerate_background")
      .map((operation) => [operation.id, operation.suggested_prompt || ""])));
    setSaveStates({});
  }, [proposal?.id, proposal?.updated_at]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => () => {
    saveTimersRef.current.forEach((timer) => clearTimeout(timer));
    saveTimersRef.current.clear();
  }, []);

  const saveTextDraft = useCallback(async (operation, value) => {
    if (!proposal || !operation || !value.trim()) return;
    const original = operation.proposed_segments?.[0]?.text || "";
    if (value === original) {
      setSaveStates((current) => ({ ...current, [operation.id]: null }));
      return;
    }
    const timer = saveTimersRef.current.get(operation.id);
    if (timer) clearTimeout(timer);
    saveTimersRef.current.delete(operation.id);
    setSaveStates((current) => ({ ...current, [operation.id]: "saving" }));
    const result = await onAdjust(
      proposal.id, operation.id, value, proposal.base_revision,
    );
    setSaveStates((current) => ({
      ...current,
      [operation.id]: result ? "saved" : "error",
    }));
  }, [onAdjust, proposal]);

  const updateTextDraft = useCallback((operation, value) => {
    setTextDrafts((current) => ({ ...current, [operation.id]: value }));
    setSaveStates((current) => ({ ...current, [operation.id]: "pending" }));
    const existing = saveTimersRef.current.get(operation.id);
    if (existing) clearTimeout(existing);
    const timer = setTimeout(() => saveTextDraft(operation, value), 700);
    saveTimersRef.current.set(operation.id, timer);
  }, [saveTextDraft]);

  const hasUnsavedDrafts = applicable.some((operation) => (
    operation.current_segments?.length === 1
    && operation.proposed_segments?.length === 1
    && (textDrafts[operation.id] ?? operation.proposed_segments?.[0]?.text ?? "")
      !== (operation.proposed_segments?.[0]?.text ?? "")
  ));

  const effective = proposal || summary;
  const status = effective?.status;
  const editorUrl = editorUrlWithRequest(jobId, requestId, proposal?.editor_url);

  if (!effective) {
    return (
      <div className="rounded-2xl bg-brand/[0.06] ring-1 ring-brand/20 p-4">
        <p className="text-caption font-semibold text-white">Convertir el pedido en cambios revisables</p>
        <p className="text-label text-gray-400 mt-1">
          El asistente puede convertir timestamps y reemplazos explícitos en un diff revisable.
        </p>
      </div>
    );
  }

  if (!proposal) {
    return (
      <div className="rounded-2xl bg-brand/[0.06] ring-1 ring-brand/20 p-4">
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
      </div>
    );
  }

  const toggle = (id) => setSelected((current) => (
    current.includes(id) ? current.filter((value) => value !== id) : [...current, id]
  ));

  return (
    <section className="rounded-2xl bg-brand/[0.045] ring-1 ring-brand/20 p-4 space-y-4" aria-label="Propuesta de cambios">
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
                      onChange={(event) => updateTextDraft(operation, event.target.value)}
                      onBlur={() => saveTextDraft(operation, proposedText)}
                      className="min-w-0 flex-1 rounded-lg bg-surface-3/60 px-2.5 py-1.5 text-caption text-emerald-200 ring-1 ring-white/[0.08] focus:outline-none focus:ring-emerald-400/35"
                    />
                    <span className={`shrink-0 self-center text-[10px] ${
                      saveStates[operation.id] === "error" ? "text-red-300"
                        : saveStates[operation.id] === "saved" ? "text-emerald-300"
                          : "text-gray-500"
                    }`}>
                      {saveStates[operation.id] === "saving" ? "Guardando…"
                        : saveStates[operation.id] === "saved" ? "Guardado"
                          : saveStates[operation.id] === "error" ? "Reintentar al salir"
                            : saveStates[operation.id] === "pending" ? "Autoguardado pendiente" : ""}
                    </span>
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
          onSeek={onSeek}
        />
      )}

      {hasUnsavedDrafts && (
        <p className="text-label text-amber-200">
          Estamos guardando tus ajustes antes de habilitar la aplicación.
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
            className="rounded-xl bg-brand px-4 py-2.5 text-caption font-semibold text-white hover:bg-brand-light disabled:opacity-40"
          >
            {busy ? "Aplicando…" : `Aplicar seleccionadas (${selected.length})`}
          </button>
        )}
        {(status === "applied" || status === "partially_applied") && editorUrl && (
          <a
            href={editorUrl}
            className="rounded-xl bg-brand px-4 py-2.5 text-caption font-semibold text-white hover:bg-brand-light"
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
    </section>
  );
}
