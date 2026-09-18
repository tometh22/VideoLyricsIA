import { useEffect } from "react";

import ChangeRequestsPanel from "./ChangeRequestsPanel";
import useChangeRequests from "./useChangeRequests";
import ChangeRequestRenderReview from "./ChangeRequestRenderReview";

export default function ChangeRequestsSection({ initialPendingCount, onPendingCountChange }) {
  const changes = useChangeRequests({ initialPendingCount });

  useEffect(() => {
    onPendingCountChange?.(changes.crPendingCount);
  }, [changes.crPendingCount, onPendingCountChange]);

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
            <p className="text-label text-amber-100/60">pendientes</p>
          </div>
          <div className="min-w-[6.5rem] rounded-xl bg-emerald-500/[0.06] px-3 py-2 ring-1 ring-emerald-400/15">
            <p className="text-xl font-bold text-emerald-200">{changes.crResolvedCount}</p>
            <p className="text-label text-emerald-100/60">resueltos</p>
          </div>
        </div>
      </div>

      <div className="w-full min-w-0 space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2 text-label text-gray-500">
          <span>UMG Argentina y UMG Chile · más recientes primero</span>
          <span className="hidden sm:inline">J/K navega · / busca · ⌘↵ ejecuta la acción recomendada</span>
        </div>
        <ChangeRequestsPanel
          changeRequests={changes.changeRequests}
          crStatusFilter={changes.crStatusFilter}
          setCrStatusFilter={changes.setCrStatusFilter}
          crPendingCount={changes.crPendingCount}
          crResolvedCount={changes.crResolvedCount}
          crLoading={changes.crLoading}
          crResolvingId={changes.crResolvingId}
          resolveChangeRequest={changes.resolveChangeRequest}
          reopenChangeRequest={changes.reopenChangeRequest}
          crPublishingId={changes.crPublishingId}
          crPublishNotice={changes.crPublishNotice}
          dismissPublishNotice={() => changes.setCrPublishNotice(null)}
          publishDeliveryUpdate={changes.publishDeliveryUpdate}
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
