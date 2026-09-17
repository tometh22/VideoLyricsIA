import { useEffect } from "react";

import SectionHeader from "../../layout/SectionHeader";

import ChangeRequestsPanel from "./ChangeRequestsPanel";
import useChangeRequests from "./useChangeRequests";

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
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <h2 id="change-requests-title" className="text-2xl font-bold text-white">
            Cambios UMG
          </h2>
          <p className="text-ui text-gray-400 mt-1">
            Revisá el pedido, la letra y los prompts sugeridos; mirá cada render antes de publicarlo.
          </p>
        </div>
        <div className="flex items-center gap-2 text-caption">
          <span className="rounded-full bg-amber-500/15 text-amber-200 ring-1 ring-amber-400/25 px-3 py-1.5 font-semibold">
            {changes.crPendingCount} pendientes
          </span>
          <span className="rounded-full bg-emerald-500/10 text-emerald-200 ring-1 ring-emerald-400/20 px-3 py-1.5">
            {changes.crResolvedCount} resueltos
          </span>
        </div>
      </div>

      <div className="rounded-card bg-brand/[0.06] ring-1 ring-brand/20 px-4 py-3">
        <p className="text-caption text-gray-200">
          Cada pedido se revisa completo en esta pantalla. Aplicar una propuesta modifica la
          letra del editor; regenerar un fondo crea un corte nuevo. Ninguna acción publica ni
          cierra el pedido hasta que revises el video y uses Publicar actualización.
        </p>
      </div>

      <div className="w-full min-w-0">
        <SectionHeader
          title="Pedidos del portal"
          subtitle="UMG Argentina y UMG Chile · ordenados por fecha de envío"
        />
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
        />
      </div>
    </section>
  );
}
