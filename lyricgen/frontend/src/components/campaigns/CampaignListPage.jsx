import OperatorActivity from "../OperatorActivity";
import { useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import useCampaignResource from "../../hooks/useCampaignResource";
import { campaignRequest } from "../../lib/campaignApi";
import { relativeDate } from "../../lib/campaignPipeline";
import { campaignOverview, campaignViews, inCampaignView } from "../../lib/campaignOverview";
import { PRODUCT_TABS, productForCampaign } from "../../lib/campaignProducts";

import { matchesCampaignSearch } from "../../lib/campaignSearch";
import CampaignSearch from "../CampaignSearch";
import DeliveryPlan from "../DeliveryPlan";
import ClientChangesInbox from "./ClientChangesInbox";
import CampaignCreateWizard from "./CampaignCreateWizard";
import { Banner, Button, Chip, EmptyState, Skeleton, inputClass } from "./ui";

const STATUS = [["", "Todas"], ["active", "Activas"], ["paused", "Pausadas"], ["completed", "Completadas"], ["cancelled", "Canceladas"]];
const STATUS_LABEL = { active: "Activa", paused: "Pausada", completed: "Completada", cancelled: "Cancelada" };
const STATUS_TONE = { active: "success", paused: "warning", completed: "brand", cancelled: "danger" };

function CampaignRow({ campaign, onOpen, onAction }) {
  const summary = campaignOverview(campaign);
  const oldest = relativeDate(campaign.pipeline?.flags?.oldest_change_request_at);
  return <li className="grid grid-cols-1 items-center gap-4 px-5 py-5 md:grid-cols-[minmax(0,1.4fr)_10rem_minmax(0,1fr)_11rem]">
    <div className="min-w-0">
      <button type="button" onClick={onOpen} className="text-left text-base font-semibold text-white hover:text-brand-light focus-visible:outline focus-visible:outline-brand">{campaign.name}</button>
      <p className="mt-1 text-xs text-ink-secondary">{campaign.kind === "art_track" ? "Art tracks" : "Lyric videos"} · {summary.total} canciones{campaign.updated_at ? ` · ${relativeDate(campaign.updated_at)}` : ""}</p>
      {campaign.status !== "active" && <div className="mt-2"><Chip tone={STATUS_TONE[campaign.status] || "neutral"}>{STATUS_LABEL[campaign.status] || "Estado no disponible"}</Chip></div>}
    </div>
    <div>
      <p className="text-lg font-semibold tabular-nums text-white">{summary.delivered === null ? "Sin confirmar" : `${summary.delivered} de ${summary.total}`}</p>
      <p className="text-xs text-ink-secondary">{summary.files ? "archivos listos" : "entregadas al portal"}</p>
      {summary.delivered !== null && <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-white/10" aria-hidden="true"><div className="h-full bg-emerald-400" style={{ width: `${summary.total ? 100 * summary.delivered / summary.total : 0}%` }} /></div>}
      {summary.discarded > 0 && <p className="mt-1 text-xs text-ink-secondary">{summary.discarded} descartadas</p>}
    </div>
    <div className="space-y-1 text-xs">
      {summary.work.map(note => <p key={note} className="text-ink-secondary">{note}</p>)}
      {summary.notes.map(note => <p key={note} className="text-amber-200">{note}</p>)}
      {oldest && <p className="text-amber-200">Pedido más antiguo: {oldest}</p>}
      {summary.complete && <p className="text-emerald-200">Sin pendientes</p>}
      {!summary.complete && !summary.work.length && !summary.notes.length && <p className="text-ink-secondary">{summary.pending ? `${summary.pending} pendientes` : "Ver estado de las canciones"}</p>}
    </div>
    <div className="md:justify-self-end"><Button size="sm" onClick={onAction}>{summary.action.label} →</Button></div>
  </li>;
}

export default function CampaignListPage() {
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const query = params.get("q") || "";
  const status = params.get("status") || "";
  const product = params.get("product") || "";
  const scope = campaignViews.some(([key]) => key === params.get("scope")) ? params.get("scope") : "all";
  const list = useCampaignResource("campaigns:list", ({ signal }) => campaignRequest("/batch/campaigns", { signal }));
  const changeCampaigns = useMemo(() => (list.data?.items || []).filter(campaign => inCampaignView(campaign, "changes"))
    .sort((a, b) => String(a.pipeline?.flags?.oldest_change_request_at || "9999").localeCompare(String(b.pipeline?.flags?.oldest_change_request_at || "9999"))), [list.data]);
  const selectedChanges = changeCampaigns.find(campaign => campaign.id === params.get("changes_campaign")) || changeCampaigns[0];
  const inboxEnabled = Boolean(list.data?.features?.change_requests_inbox);
  const requestCount = changeCampaigns.reduce((sum, campaign) => sum + campaignOverview(campaign).requests, 0);
  const [planBusy, setPlanBusy] = useState(false);
  const [creating, setCreating] = useState(params.get("new") === "1");
  const items = list.data?.items || [];
  const filtered = useMemo(() => items.filter((campaign) => matchesCampaignSearch(query, campaign.name, campaign.id)
    && (!status || campaign.status === status) && (!product || productForCampaign(campaign) === product)
    && inCampaignView(campaign, scope)).sort((a, b) => Number(campaignOverview(b).attention) - Number(campaignOverview(a).attention)
      || String(b.updated_at || "").localeCompare(String(a.updated_at || ""))), [items, query, status, product, scope]);
  const productCounts = useMemo(() => items.reduce((counts, campaign) => {
    const key = productForCampaign(campaign);
    counts[key] = (counts[key] || 0) + 1;
    return counts;
  }, {}), [items]);
  const open = (campaign) => navigate(`/campaigns/${encodeURIComponent(campaign.id)}`);
  const setParam = (key, value, replace = true) => {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value); else next.delete(key);
    setParams(next, { replace });
  };
  const showChanges = () => setParams({ scope: "changes" });
  return <div className="mx-auto max-w-[1500px] space-y-6 pb-16">
    <header className="flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
      <div>
        <p className="text-[11px] font-semibold uppercase tracking-[.2em] text-brand-light">Producción masiva</p>
        <h1 className="mt-2 text-3xl font-bold tracking-tight text-white">Campañas</h1>
        <p className="mt-1.5 text-sm text-ink-secondary">Qué está entregado, qué falta y por dónde seguir.</p>
      </div>
      <Button variant="primary" size="lg" disabled={planBusy} onClick={() => setCreating(true)}>+ Nueva campaña</Button>
    </header>

    {list.data?.can_view_accounts && <nav aria-label="Campañas y cuentas" className="flex gap-2">{[["campaigns", "Campañas"], ["accounts", "Cuentas · Plan de entrega"]].map(([value, label]) => <Button key={value} disabled={planBusy} variant={(params.get("view") || "campaigns") === value ? "primary" : "ghost"} onClick={() => setParam("view", value, false)}>{label}</Button>)}</nav>}
    {list.data?.can_view_accounts && params.get("view") === "accounts" ? <DeliveryPlan onBusyChange={setPlanBusy} /> : <>
    {list.data && inboxEnabled && requestCount > 0 && scope !== "changes" && <Banner tone="warning" action={<Button variant="primary" onClick={showChanges}>Ver canciones con pedidos →</Button>}>
      <strong>{requestCount} {requestCount === 1 ? "pedido de cambios pendiente" : "pedidos de cambios pendientes"}</strong> en {changeCampaigns.length} {changeCampaigns.length === 1 ? "campaña" : "campañas"}. Abrí la canción, leé el pedido y continuá la corrección.
    </Banner>}
    <nav aria-label="Progreso de campañas" className="flex flex-wrap gap-2">
      {campaignViews.filter(([key]) => key !== "changes" || inboxEnabled).map(([key, label]) => <button key={key} type="button" aria-pressed={scope === key} onClick={() => key === "changes" ? showChanges() : setParams({ scope: key })}
        className={`rounded-xl px-4 py-2.5 text-sm font-medium ring-1 transition ${scope === key ? "bg-brand/20 text-white ring-brand/50" : "text-ink-secondary ring-white/10 hover:text-white"}`}>
        {label}<span className="ml-2 text-xs opacity-70">{items.filter(campaign => inCampaignView(campaign, key)).length}</span>
      </button>)}
    </nav>

    {scope === "changes" ? <>
      <p className="text-sm text-ink-secondary">Pedidos abiertos del cliente, incluso en campañas ya entregadas. Primero la campaña con el pedido más antiguo.</p>
      {items.some(campaign => campaign.pipeline?.portal_status_available === false) && <Banner tone="warning">No pudimos consultar todos los portales. Puede haber pedidos que todavía no aparecen.</Banner>}
      {!inboxEnabled ? <Banner tone="info">La bandeja de pedidos no está habilitada en esta cuenta.</Banner> : selectedChanges ? <>
        <label className="block text-sm text-ink-secondary">Campaña con pedidos
          <select className={`${inputClass} mt-2 w-full`} value={selectedChanges.id} onChange={event => setParam("changes_campaign", event.target.value, false)}>
            {changeCampaigns.map(campaign => <option key={campaign.id} value={campaign.id}>{campaign.name} · {campaignOverview(campaign).requests} pedidos</option>)}
          </select>
        </label>
        <ClientChangesInbox key={selectedChanges.id} campaignId={selectedChanges.id} isAdmin={Boolean(list.data?.can_view_accounts)}
          onOpenSong={songId => navigate(`/campaigns/${encodeURIComponent(selectedChanges.id)}?view=all&song=${encodeURIComponent(songId)}`)} />
      </> : list.data && <EmptyState title="No hay pedidos de cambios pendientes" description="Las campañas finalizadas siguen disponibles en Todas las campañas." />}
    </> : <>
    <nav aria-label="Productos" className="flex gap-2 overflow-x-auto pb-1">
      {[["", "Todos los productos", items.length], ...PRODUCT_TABS.map((tab) => [tab.id, tab.label, productCounts[tab.id] || 0]),
        ...(productCounts.unclassified ? [["unclassified", "Sin clasificar", productCounts.unclassified]] : [])]
        .map(([key, label, count]) => <button key={key || "all"} type="button" aria-current={product === key ? "page" : undefined}
          onClick={() => setParam("product", key, false)}
          className={`shrink-0 rounded-xl px-3.5 py-2 text-sm font-medium ring-1 transition ${product === key ? "bg-brand/20 text-white ring-brand/50" : "bg-surface-2/40 text-ink-secondary ring-white/10 hover:text-white"}`}>
          {label}<span className="ml-2 text-xs opacity-70">{count}</span>
        </button>)}
    </nav>

    <div className="flex flex-col gap-3 md:flex-row md:items-center">
      <CampaignSearch className="md:w-80" label="Buscar campañas" placeholder="Nombre de campaña" value={query} onChange={(value) => setParam("q", value)} />
      <label className="text-sm text-ink-secondary">Estado de campaña
        <select aria-label="Estado de campaña" value={status} onChange={event => setParam("status", event.target.value, false)} className={`${inputClass} ml-2 !w-auto`}>
          {STATUS.map(([key, label]) => <option key={key || "all"} value={key}>{key ? label : "Cualquier estado"}</option>)}
        </select>
      </label>
      {(query || status || product || scope !== "all") && <Button size="sm" variant="ghost" onClick={() => setParams({ scope: "all" })}>Quitar filtros y ver todas</Button>}
    </div>

    </>}
    {list.error && <Banner tone="danger" action={<Button size="sm" onClick={list.reload}>Reintentar</Button>}>{list.error.message || "No se pudieron cargar las campañas."}</Banner>}
    {list.loading && <div className="grid gap-4 md:grid-cols-2 2xl:grid-cols-3">
      <p role="status" className="sr-only">Cargando campañas…</p>
      {Array.from({ length: 4 }, (_, index) => <Skeleton key={index} className="h-40 rounded-card" />)}
    </div>}
    {list.data && scope !== "changes" && filtered.length > 0 && <section aria-label="Lista de campañas" className="overflow-hidden rounded-card bg-surface-2/40 ring-1 ring-white/[0.07]">
      <div aria-hidden="true" className="hidden grid-cols-[minmax(0,1.4fr)_10rem_minmax(0,1fr)_11rem] gap-4 border-b border-white/10 px-5 py-3 text-xs text-ink-secondary md:grid">
        <span>Campaña</span><span>Entrega</span><span>Pendientes</span><span className="text-right">Próxima acción</span>
      </div>
      <ul className="divide-y divide-white/[0.06]">{filtered.map(campaign => <CampaignRow key={campaign.id} campaign={campaign} onOpen={() => open(campaign)} onAction={() => navigate(campaignOverview(campaign).action.href)} />)}</ul>
    </section>}
    {list.data && !items.length && <div className="rounded-card bg-surface-2/30 ring-1 ring-white/[0.06]">
      <EmptyState title="Todavía no hay campañas" description="Creá una campaña, subí la carpeta de audios y seguí cada canción hasta el portal del cliente."
        action={<Button variant="primary" onClick={() => setCreating(true)}>Crear la primera campaña</Button>} />
    </div>}
    {list.data && scope !== "changes" && items.length > 0 && !filtered.length && <EmptyState title="No hay campañas con estos filtros"
      action={<Button size="sm" onClick={() => setParams({})}>Limpiar filtros</Button>} />}

    <details className="rounded-card ring-1 ring-white/10"><summary className="cursor-pointer px-4 py-3 text-sm text-ink-secondary">Mi actividad de producción</summary><div className="p-4 pt-0"><OperatorActivity /></div></details>
    </>}
    {creating && <CampaignCreateWizard initialProductId={product} onClose={() => { setCreating(false); if (params.get("new")) setParam("new", ""); }}
      onFinished={(campaign) => { setCreating(false); void list.reload(); navigate(`/campaigns/${encodeURIComponent(campaign.id)}`); }} />}
  </div>;
}
