import { campaignCounts, totalOf } from "./campaignPipeline";

export const campaignViews = [["all", "Todas las campañas"], ["changes", "Pedidos de cambios"], ["active", "En curso"], ["attention", "Necesitan atención"], ["done", "Finalizadas"]];
export const campaignOrder = value => ["calendar", "effort", "learning"].includes(value) ? value : "calendar";

/** Delivery is confirmed by the pipeline's portal lookup, never by approval. */
export function campaignOverview(campaign) {
  const counts = campaignCounts(campaign);
  const flags = campaign.pipeline?.flags || {};
  const files = campaign.destination_portal === "files";
  const known = Boolean(campaign.pipeline) && (files || campaign.pipeline.portal_status_available === true);
  const total = Math.max(totalOf(counts, { includeDiscarded: true }), campaign.registered_count || 0, campaign.expected_count || 0);
  const delivered = known ? counts.delivered + (files ? counts.approved : 0) : null;
  const pending = known ? Math.max(0, total - delivered - counts.discarded) : null;
  const cancelled = campaign.status === "cancelled";
  const requests = flags.change_requests_open || flags.change_requests || 0;
  const attention = !cancelled && Boolean(requests || counts.attention || flags.metadata_missing || flags.upload_errors || flags.portal_outdated);
  const complete = known && total > counts.discarded && pending === 0 && !attention && !cancelled;
  const notes = [
    requests && `${requests} ${requests === 1 ? "cambio pedido" : "cambios pedidos"} por el cliente`,
    counts.attention && `${counts.attention} con errores`,
    flags.metadata_missing && `${flags.metadata_missing} con datos incompletos`,
    flags.portal_outdated && `${flags.portal_outdated} con una versión nueva por enviar`,
  ].filter(Boolean);
  const work = [
    counts.lyrics && `${counts.lyrics} letras por revisar`,
    counts.qc && `${counts.qc} videos por revisar`,
    !files && counts.approved && `${counts.approved} aprobadas, sin enviar`,
    counts.ready && `${counts.ready} listas para generar`,
    counts.audio && `${counts.audio} en preparación de audio`,
    counts.rendering && `${counts.rendering} videos generándose`,
  ].filter(Boolean);
  let action = { label: "Ver campaña", view: "all" };
  if (!cancelled && campaign.status !== "paused") {
    if (requests) action = { label: "Revisar cambios", view: "changes" };
    else if (counts.attention) action = { label: "Resolver errores", view: "attention" };
    else if (flags.metadata_missing || flags.upload_errors) action = { label: "Completar audios", view: "audio" };
    else if (!files && counts.approved) action = { label: "Preparar entrega", view: "approved" };
    else if (counts.qc) action = { label: "Revisar videos", view: "qc" };
    else if (counts.lyrics) action = { label: "Revisar letras", view: "lyrics" };
    else if (counts.ready) action = { label: "Preparar videos", view: "ready" };
    else if (complete) action = { label: files ? "Ver archivos" : "Ver entregas", view: files ? "all" : "delivered" };
  }
  return { total, delivered, pending, complete, attention, requests, notes, work, files, discarded: counts.discarded,
    action: { ...action, href: `/campaigns/${encodeURIComponent(campaign.id)}?view=${action.view}&order=calendar` } };
}

export function inCampaignView(campaign, view) {
  const summary = campaignOverview(campaign);
  return view === "all" || (view === "changes" ? summary.requests > 0 && campaign.status !== "cancelled" : view === "done" ? summary.complete : view === "attention" ? summary.attention
    : !summary.complete && campaign.status !== "cancelled");
}
