import { useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import useCampaignResource from "../../hooks/useCampaignResource";
import { campaignRequest } from "../../lib/campaignApi";
import { campaignCounts, campaignNextStep, doneCount, relativeDate, stagesFor, toneOf, totalOf } from "../../lib/campaignPipeline";
import { matchesCampaignSearch } from "../../lib/campaignSearch";
import CampaignSearch from "../CampaignSearch";
import CampaignCreateWizard from "./CampaignCreateWizard";
import { Banner, Button, Chip, EmptyState, Skeleton } from "./ui";

const STATUS = [["", "Todas"], ["active", "Activas"], ["paused", "Pausadas"], ["completed", "Completadas"], ["cancelled", "Canceladas"]];
const STATUS_LABEL = { active: "Activa", paused: "Pausada", completed: "Completada", cancelled: "Cancelada" };
const STATUS_TONE = { active: "success", paused: "warning", completed: "brand", cancelled: "danger" };

function MiniPipeline({ counts, kind }) {
  const total = totalOf(counts);
  const stages = stagesFor(kind);
  return <div className="flex h-2 overflow-hidden rounded-full bg-white/[0.06]" aria-hidden="true">
    {total > 0 && stages.map((stage) => counts[stage.key] > 0 && <span key={stage.key} className={`${toneOf(stage.key).bar} h-full`} style={{ width: `${(100 * counts[stage.key]) / total}%` }} />)}
  </div>;
}

function CampaignCard({ campaign, onOpen }) {
  const counts = campaignCounts(campaign);
  const kind = campaign.kind || "lyric_video";
  const total = totalOf(counts) || campaign.registered_count || 0;
  const done = doneCount(counts);
  const next = campaignNextStep(counts, kind, { portalSends: campaign.destination_portal !== "files" });
  const percent = total ? Math.round((100 * done) / total) : 0;
  return <button type="button" onClick={onOpen} aria-label={`${campaign.name} · ${STATUS_LABEL[campaign.status] || campaign.status}`}
    className="group flex flex-col gap-4 rounded-card bg-surface-2/50 p-5 text-left ring-1 ring-white/[0.07] transition duration-brand hover:-translate-y-0.5 hover:bg-surface-2/80 hover:ring-brand/40 focus-visible:outline focus-visible:outline-2 focus-visible:outline-brand">
    <div className="flex items-start justify-between gap-3">
      <div className="min-w-0">
        <p className="truncate text-base font-semibold text-white">{campaign.name}</p>
        <p className="mt-0.5 text-xs text-ink-secondary">{kind === "art_track" ? "Art tracks" : "Lyric videos"} · {total} {total === 1 ? "canción" : "canciones"}{campaign.updated_at ? ` · ${relativeDate(campaign.updated_at)}` : ""}</p>
      </div>
      <Chip tone={STATUS_TONE[campaign.status] || "neutral"}>{STATUS_LABEL[campaign.status] || "Estado no disponible"}</Chip>
    </div>
    <MiniPipeline counts={counts} kind={kind} />
    <div className="flex flex-wrap items-end justify-between gap-3">
      <div>
        <p className="text-2xl font-semibold tabular-nums text-white">{percent}%<span className="ml-1.5 text-xs font-normal text-ink-secondary">{done} de {total} terminadas</span></p>
      </div>
      {next && <span className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium ${next.passive ? "text-ink-secondary" : "bg-white/[0.06] text-white ring-1 ring-white/10 group-hover:ring-brand/40"}`}>
        {!next.passive && <span aria-hidden="true" className={`h-1.5 w-1.5 rounded-full ${toneOf(next.stage).dot}`} />}{next.label}{!next.passive && <span aria-hidden="true">→</span>}
      </span>}
    </div>
  </button>;
}

export default function CampaignListPage() {
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const query = params.get("q") || "";
  const status = params.get("status") || "";
  const [creating, setCreating] = useState(params.get("new") === "1");
  const list = useCampaignResource("campaigns:list", ({ signal }) => campaignRequest("/batch/campaigns", { signal }));
  const items = list.data?.items || [];
  const filtered = items.filter((campaign) => matchesCampaignSearch(query, campaign.name, campaign.id) && (!status || campaign.status === status));
  const totals = useMemo(() => items.filter((campaign) => campaign.status === "active").reduce((sum, campaign) => {
    const counts = campaignCounts(campaign);
    return { active: sum.active + 1, lyrics: sum.lyrics + counts.lyrics, qc: sum.qc + counts.qc, delivered: sum.delivered + counts.delivered + counts.approved };
  }, { active: 0, lyrics: 0, qc: 0, delivered: 0 }), [items]);
  const setParam = (key, value, replace = true) => {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value); else next.delete(key);
    setParams(next, { replace });
  };
  return <div className="mx-auto max-w-[1500px] space-y-6 pb-16">
    <header className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
      <div>
        <p className="text-[11px] font-semibold uppercase tracking-[.2em] text-brand-light">Producción masiva</p>
        <h1 className="mt-2 text-3xl font-bold tracking-tight text-white">Campañas</h1>
        <p className="mt-1.5 text-sm text-ink-secondary">De la carpeta de audios al portal del cliente, canción por canción.</p>
      </div>
      <Button variant="primary" size="lg" onClick={() => setCreating(true)}>+ Nueva campaña</Button>
    </header>

    <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
      {[["Campañas activas", totals.active], ["Letras por revisar", totals.lyrics], ["Videos por revisar", totals.qc], ["Terminadas", totals.delivered]].map(([label, value]) =>
        <div key={label} className="rounded-card bg-surface-2/40 p-4 ring-1 ring-white/[0.06]">
          <p className="text-xs text-ink-secondary">{label}</p>
          {list.data ? <p className="mt-1 text-2xl font-semibold tabular-nums text-white">{value}</p> : <Skeleton className="mt-2 h-7 w-12" />}
        </div>)}
    </div>

    <div className="flex flex-col gap-3 md:flex-row md:items-center">
      <CampaignSearch className="md:w-80" label="Buscar campañas" placeholder="Nombre de campaña" value={query} onChange={(value) => setParam("q", value)} />
      <div role="radiogroup" aria-label="Estado de campaña" className="flex gap-1 overflow-x-auto rounded-button bg-black/25 p-1 ring-1 ring-white/10">
        {STATUS.map(([key, label]) => <button key={key || "all"} type="button" role="radio" aria-checked={status === key} onClick={() => setParam("status", key, false)}
          className={`whitespace-nowrap rounded-lg px-3 py-1.5 text-sm font-medium transition ${status === key ? "bg-white/10 text-white" : "text-ink-secondary hover:text-white"}`}>{label}</button>)}
      </div>
    </div>

    {list.error && <Banner tone="danger" action={<Button size="sm" onClick={list.reload}>Reintentar</Button>}>{list.error.message || "No se pudieron cargar las campañas."}</Banner>}
    {list.loading && <div className="grid gap-4 md:grid-cols-2 2xl:grid-cols-3">
      <p role="status" className="sr-only">Cargando campañas…</p>
      {Array.from({ length: 4 }, (_, index) => <Skeleton key={index} className="h-40 rounded-card" />)}
    </div>}
    {list.data && <div className="grid gap-4 md:grid-cols-2 2xl:grid-cols-3">
      {filtered.map((campaign) => <CampaignCard key={campaign.id} campaign={campaign} onOpen={() => navigate(`/campaigns/${encodeURIComponent(campaign.id)}`)} />)}
    </div>}
    {list.data && !items.length && <div className="rounded-card bg-surface-2/30 ring-1 ring-white/[0.06]">
      <EmptyState title="Todavía no hay campañas" description="Creá una campaña, subí la carpeta de audios y seguí cada canción hasta el portal del cliente."
        action={<Button variant="primary" onClick={() => setCreating(true)}>Crear la primera campaña</Button>} />
    </div>}
    {list.data && items.length > 0 && !filtered.length && <EmptyState title="No hay campañas con estos filtros"
      action={<Button size="sm" onClick={() => setParams({})}>Limpiar filtros</Button>} />}

    {creating && <CampaignCreateWizard onClose={() => { setCreating(false); if (params.get("new")) setParam("new", ""); }}
      onFinished={(campaign) => { setCreating(false); void list.reload(); navigate(`/campaigns/${encodeURIComponent(campaign.id)}`); }} />}
  </div>;
}
