import { useEffect, useState } from "react";

import { API, fetchJson } from "../../adminApi";
import ChangeRequestsPanel from "./ChangeRequestsPanel";
import useChangeRequests from "./useChangeRequests";
import ChangeRequestRenderReview from "./ChangeRequestRenderReview";
import { PublicationModeSwitch } from "./ClientVisibility";

export default function ChangeRequestsSection({ initialPendingCount, onPendingCountChange }) {
  const changes = useChangeRequests({ initialPendingCount });
  const [qualityCohort, setQualityCohort] = useState(null);

  useEffect(() => {
    onPendingCountChange?.(changes.crPendingCount);
  }, [changes.crPendingCount, onPendingCountChange]);

  useEffect(() => {
    let active = true;
    fetchJson(`${API}/admin/quality/change-requests`)
      .then((data) => { if (active) setQualityCohort(data.delivery_cohort || null); })
      .catch(() => { if (active) setQualityCohort({ unavailable: true }); });
    return () => { active = false; };
  }, []);

  return (
    <section
      className="w-full min-w-0 space-y-5"
      aria-labelledby="change-requests-title"
      data-testid="change-requests-fullscreen"
    >
      <div className="flex items-start justify-between gap-4 flex-wrap border-b border-white/[0.06] pb-5">
        <div>
          <div className="flex items-center gap-2">
            <span className="h-2.5 w-2.5 rounded-full bg-brand shadow-[0_0_18px_rgba(124,58,237,0.75)]" />
            <p className="text-label font-semibold uppercase tracking-[0.18em] text-brand-light">
              Operación
            </p>
          </div>
          <h2 id="change-requests-title" className="mt-2 text-3xl font-bold tracking-tight text-white">
            Cambios UMG
          </h2>
          <p className="text-ui text-gray-400 mt-1 max-w-2xl">
            Atendé cada pedido de punta a punta: interpretar, corregir, revisar el corte y publicar.
          </p>
        </div>
        <div className="flex items-stretch gap-2 text-caption">
          <div className="min-w-[6.5rem] rounded-xl bg-amber-500/[0.08] px-3 py-2 ring-1 ring-amber-400/15">
            <p className="text-xl font-bold text-amber-200">{changes.crPendingCount}</p>
            <p className="text-label text-amber-300">pendientes</p>
          </div>
          <div className="min-w-[6.5rem] rounded-xl bg-emerald-500/[0.06] px-3 py-2 ring-1 ring-emerald-400/15">
            <p className="text-xl font-bold text-emerald-200">{changes.crResolvedCount}</p>
            <p className="text-label text-emerald-300">resueltos</p>
          </div>
        </div>
      </div>

      {qualityCohort?.status === "ready" && (
        <div className="rounded-xl border border-white/[0.08] bg-white/[0.02] p-4" data-testid="change-request-quality-cohort">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <h3 className="text-ui font-semibold text-white">Calidad de entregas · letra y timing</h3>
              <p className="mt-1 text-caption text-gray-400">
                Videos con pedidos dentro de 30 días desde su primera publicación registrada. Solo incluye cohortes con seguimiento completo.
                {qualityCohort.cohort_start && qualityCohort.cohort_end
                  ? ` Cohorte: ${new Date(qualityCohort.cohort_start).toLocaleDateString("es-AR")} – ${new Date(qualityCohort.cohort_end).toLocaleDateString("es-AR")}.`
                  : " Todavía no hay una cohorte madura."}
              </p>
            </div>
            <span className="rounded-full bg-white/[0.05] px-2.5 py-1 text-label text-gray-300">
              {qualityCohort.eligible_videos} videos evaluables
            </span>
          </div>
          <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
            <div><p className="text-2xl font-bold text-white">{qualityCohort.lyric_or_timing_request_rate == null ? "—" : `${Math.round(qualityCohort.lyric_or_timing_request_rate * 100)}%`}</p><p className="text-label text-gray-400">{qualityCohort.videos_with_lyric_or_timing_request} de {qualityCohort.eligible_videos} con pedido</p></div>
            <div><p className="text-2xl font-bold text-white">{qualityCohort.videos_by_category?.letra ?? 0}</p><p className="text-label text-gray-400">videos con pedido de letra</p></div>
            <div><p className="text-2xl font-bold text-white">{qualityCohort.videos_by_category?.sincronizacion ?? 0}</p><p className="text-label text-gray-400">videos con pedido de timing</p></div>
            <div><p className="text-2xl font-bold text-white">{qualityCohort.videos_without_reconstructable_first_publish ?? 0}</p><p className="text-label text-gray-400">sin fecha reconstruible</p></div>
          </div>
          <p className="mt-3 text-label text-gray-500">
            Las categorías pueden coincidir en un mismo video. La fecha inicial sale del historial de publicación; los reenvíos del mismo corte no reinician el seguimiento.
          </p>
          <p className="mt-1 text-label text-gray-500">
            Además: {qualityCohort.videos_by_category?.estructura ?? 0} videos con pedidos de estructura · {qualityCohort.videos_by_category?.repeticiones ?? 0} con pedidos sobre repeticiones.
          </p>
        </div>
      )}

      <div className="w-full min-w-0 space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2 text-label text-gray-500">
          <span>UMG Argentina y UMG Chile · más recientes primero</span>
          <span className="hidden sm:inline">J/K navega · / busca · ⌘↵ ejecuta la acción recomendada</span>
        </div>
        <PublicationModeSwitch
          mode={changes.crPublicationMode}
          busy={changes.crModeBusy}
          canChange={changes.crCanChangeMode}
          onChange={changes.changePublicationMode}
        />
        <ChangeRequestsPanel
          visibilityBusyId={changes.crVisibilityBusyId}
          setDeliveryVisibility={changes.setDeliveryVisibility}
          changeRequests={changes.changeRequests}
          crStatusFilter={changes.crStatusFilter}
          setCrStatusFilter={changes.setCrStatusFilter}
          crPendingCount={changes.crPendingCount}
          crResolvedCount={changes.crResolvedCount}
          crLoading={changes.crLoading}
          crResolvingId={changes.crResolvingId}
          resolveChangeRequest={changes.resolveChangeRequest}
          confirmChangeRequestPublication={changes.confirmChangeRequestPublication}
          reopenChangeRequest={changes.reopenChangeRequest}
          crPublishingId={changes.crPublishingId}
          crPublishNotice={changes.crPublishNotice}
          dismissPublishNotice={() => changes.setCrPublishNotice(null)}
          publishDeliveryUpdate={changes.publishDeliveryUpdate}
          reconcilePublication={changes.reconcilePublication}
          prepareProRes={changes.prepareProRes}
          onProResConfigured={changes.handleProResConfigured}
          proposalEnabled={changes.crProposalEnabled}
          proposalApplyEnabled={changes.crProposalApplyEnabled}
          proposalBusyId={changes.crProposalBusyId}
          proposalDetails={changes.crProposalDetails}
          generateProposal={changes.generateChangeRequestProposal}
          loadProposal={changes.loadChangeRequestProposal}
          adjustProposal={changes.adjustChangeRequestProposal}
          applyProposal={changes.applyChangeRequestProposal}
          dismissProposal={changes.dismissChangeRequestProposal}
          regenerateBackground={changes.regenerateBackgroundFromProposal}
          reviewForRender={changes.reviewForRender}
          refreshChangeRequests={changes.refreshChangeRequests}
        />
        {changes.crRenderReview && <ChangeRequestRenderReview review={changes.crRenderReview}
          busy={changes.crProposalBusyId === changes.crRenderReview.change_request_id}
          onConfirm={changes.confirmRender} onClose={() => changes.setCrRenderReview(null)} />}
      </div>
    </section>
  );
}
