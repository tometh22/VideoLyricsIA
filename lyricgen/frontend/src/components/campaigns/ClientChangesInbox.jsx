import { useCallback, useEffect, useRef, useState } from "react";
import { campaignRequest } from "../../lib/campaignApi";
import { CHANGE_SLA_HOURS, businessHoursSince, changeSlaTone, portalLabel, relativeDate } from "../../lib/campaignPipeline";
import { Banner, Button, Chip, EmptyState, Skeleton } from "./ui";

const FILTERS = [["open", "Abiertos"], ["resolved", "Resueltos"], ["all", "Todos"]];
const STEP_TONE = { done: "success", idle: "neutral", attention: "warning", busy: "info" };
const APPROVAL = { approved: "El cliente aprobó esta versión", pending: "El cliente todavía no aprobó esta versión" };

/**
 * Client change requests for this campaign. Read-only on purpose: every action
 * still happens in Admin > Cambios, so this adds visibility, not new power.
 */
export default function ClientChangesInbox({ campaignId, isAdmin = false, onOpenSong, request = campaignRequest }) {
  const [status, setStatus] = useState("open");
  const [state, setState] = useState({ items: [], counts: null, cursor: null, available: true, loading: true, error: "" });
  const controller = useRef(null);

  const load = useCallback(async (cursor = null) => {
    controller.current?.abort();
    const abort = new AbortController();
    controller.current = abort;
    setState((old) => ({ ...old, loading: true, error: "", ...(cursor ? {} : { items: [] }) }));
    try {
      const query = new URLSearchParams({ status, limit: "50", ...(cursor ? { cursor } : {}) });
      const result = await request(`/batch/campaigns/${encodeURIComponent(campaignId)}/change-requests?${query}`, { signal: abort.signal });
      if (abort.signal.aborted) return;
      setState((old) => ({
        items: cursor ? [...old.items, ...result.items] : result.items,
        counts: result.counts, cursor: result.next_cursor, available: result.available !== false,
        loading: false, error: "",
      }));
    } catch (error) {
      if (!abort.signal.aborted) setState((old) => ({ ...old, loading: false, error: error.message || "No se pudieron cargar los pedidos." }));
    }
  }, [campaignId, request, status]);

  useEffect(() => { void load(); return () => controller.current?.abort(); }, [load]);

  const { items, counts, cursor, available, loading, error } = state;
  return <section aria-label="Cambios del cliente" className="space-y-4 rounded-card bg-surface-2/40 p-4 ring-1 ring-white/[0.06]">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div>
        <h2 className="text-base font-semibold">Cambios del cliente</h2>
        <p className="text-xs text-ink-secondary">Pedidos que el cliente hizo desde el portal sobre las canciones de esta campaña. El objetivo es responder en {CHANGE_SLA_HOURS} horas hábiles.</p>
      </div>
      <div className="flex items-center gap-2">
        <div role="radiogroup" aria-label="Estado del pedido" className="flex gap-1 rounded-button bg-black/25 p-1 ring-1 ring-white/10">
          {FILTERS.map(([key, label]) => {
            const count = counts ? (key === "all" ? (counts.open ?? 0) + (counts.resolved ?? 0) : counts[key]) : null;
            return <button key={key} type="button" role="radio" aria-checked={status === key} onClick={() => setStatus(key)}
              className={`whitespace-nowrap rounded-lg px-3 py-1.5 text-sm font-medium transition ${status === key ? "bg-white/10 text-white" : "text-ink-secondary hover:text-white"}`}>
              {label}{count != null && <span className="ml-1.5 tabular-nums opacity-70">{count}</span>}</button>;
          })}
        </div>
        <Button size="sm" variant="ghost" onClick={() => load()} aria-label="Actualizar pedidos" title="Actualizar pedidos">↻</Button>
      </div>
    </div>

    {!available && <Banner tone="warning">No pudimos consultar el portal del cliente. No es que no haya pedidos: reintentá en un momento.</Banner>}
    {error && <Banner tone="danger" action={<Button size="sm" onClick={() => load()}>Reintentar</Button>}>{error}</Banner>}
    {loading && !items.length && <div className="space-y-2" role="status" aria-label="Cargando pedidos">{[0, 1, 2].map((n) => <Skeleton key={n} className="h-20 rounded-xl" />)}</div>}
    {!loading && available && !error && !items.length && <EmptyState icon="✓" title={status === "resolved" ? "Todavía no hay pedidos resueltos" : "No hay pedidos del cliente"}
      description={status === "open" ? "Cuando el cliente pida un cambio desde el portal, aparece acá." : ""} />}

    <ul className="space-y-2">
      {items.map((item) => {
        const open = !item.resolved_at;
        const hours = open ? businessHoursSince(item.submitted_at) : 0;
        return <li key={item.id} className="rounded-xl bg-black/20 p-4 ring-1 ring-white/[0.06]">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="min-w-0">
              <p className="truncate text-sm font-semibold text-white">{item.song}<span className="font-normal text-ink-secondary"> · {item.artist}</span></p>
              <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                <Chip tone="brand">{portalLabel(item.portal_id)}</Chip>
                <Chip tone={STEP_TONE[item.step?.tone] || "neutral"}>{item.step?.label}</Chip>
                {open && <Chip tone={changeSlaTone(hours)} title={`${Math.floor(hours)} horas hábiles sin resolver (objetivo ${CHANGE_SLA_HOURS})`}>{relativeDate(item.submitted_at)}</Chip>}
                {!open && <Chip>{relativeDate(item.resolved_at)}</Chip>}
                {item.client_approval && <Chip tone={item.client_approval === "approved" ? "success" : "neutral"} title={APPROVAL[item.client_approval]}>
                  {item.client_approval === "approved" ? "Cliente aprobó" : "Cliente sin aprobar"} · v{item.published_revision}</Chip>}
              </div>
            </div>
            <div className="flex items-center gap-2">
              {item.song_id && <Button size="sm" variant="ghost" onClick={() => onOpenSong?.(item.song_id)}>Ver canción</Button>}
              {open && (isAdmin
                ? <a className="text-sm text-brand-light underline" href={`/admin?section=cambios&change_request_id=${encodeURIComponent(item.id)}`}>Resolver en Admin →</a>
                : <span className="text-xs text-ink-secondary">Lo resuelve un admin</span>)}
            </div>
          </div>
          <p className="mt-3 whitespace-pre-wrap text-sm text-ink-primary/90">{item.comment}</p>
          {!open && item.resolution_note && <p className="mt-2 text-xs text-ink-secondary">Respuesta: {item.resolution_note}</p>}
        </li>;
      })}
    </ul>
    {cursor && <div className="flex justify-center"><Button variant="ghost" disabled={loading} onClick={() => load(cursor)}>{loading ? "Cargando…" : "Ver más"}</Button></div>}
  </section>;
}
