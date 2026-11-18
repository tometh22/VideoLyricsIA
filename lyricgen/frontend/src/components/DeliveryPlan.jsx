import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { createPortal } from "react-dom";
import useDialogA11y from "../hooks/useDialogA11y";
import { campaignGenerateForm } from "../lib/campaignCreative";
import { matchesCampaignSong } from "../lib/campaignSearch";

const API = import.meta.env.VITE_API_URL || "";
const labels = { veo: "Veo", photo_effect: "Foto + efecto", unknown: "Sin asignar / verificar" };
const accounts = { argentina: "Universal Argentina", chile: "Universal Chile" };
const button = "rounded-xl px-4 py-2 text-sm font-semibold ring-1 ring-white/15 hover:bg-white/10 disabled:cursor-not-allowed disabled:opacity-40";
const primary = `${button} bg-brand text-white ring-brand hover:bg-brand/80`;
const input = "rounded-xl bg-surface-2 px-3 py-2 text-sm text-white ring-1 ring-white/15";

async function request(path, options = {}) {
  const response = await fetch(`${API}${path}`, { ...options, cache: "no-store", headers: {
    Authorization: `Bearer ${localStorage.getItem("genly_token") || ""}`,
    ...(options.body && !(options.body instanceof FormData) ? { "Content-Type": "application/json" } : {}),
    ...options.headers,
  } });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(typeof body.detail === "string" ? body.detail : body.detail?.message || body.detail?.code || `No se pudo completar la operación (${response.status})`);
    error.status = response.status;
    throw error;
  }
  return body;
}

function Modal({ title, onClose, busy, children }) {
  const ref = useDialogA11y({ onClose, closeOnEscape: !busy });
  return createPortal(<div ref={ref} role="dialog" aria-modal="true" aria-label={title} tabIndex={-1} className="fixed inset-0 z-[80] grid place-items-center overflow-y-auto bg-black/75 p-4">
    <div className="my-auto max-h-[90vh] w-full max-w-3xl overflow-y-auto rounded-2xl bg-surface-2 p-6 shadow-2xl ring-1 ring-white/15">
      <div className="mb-4 flex items-center justify-between gap-3"><h2 className="text-xl font-semibold">{title}</h2><button className={button} disabled={busy} onClick={onClose}>Cerrar</button></div>{children}
    </div>
  </div>, document.body);
}

function Count({ value, label, tone = "" }) {
  return <div className={`rounded-xl p-4 ring-1 ring-white/10 ${tone || "bg-white/[0.03]"}`}><p className="text-2xl font-bold tabular-nums">{value}</p><p className="mt-1 text-xs text-ink-secondary">{label}</p></div>;
}

export function DeliveryReviewLink({ row, peerOrigin, children }) {
  const view = row.stage === "generated" ? "qc" : "delivered";
  const path = row.campaign_id ? `/campaigns/${encodeURIComponent(row.campaign_id)}?view=${view}` : `/videos/${encodeURIComponent(row.job_id)}`;
  if (row.source === "missing") return <span className="text-sm text-ink-secondary" title="El trabajo original ya no está disponible. Consultá la publicación en el portal del cliente.">{children} · consultar portal</span>;
  if (row.source === "peer") {
    if (!["https://genly.pro", "https://staging.genly.pro"].includes(peerOrigin)) return <span className="text-sm text-ink-secondary">{children} · otro entorno</span>;
    return <a className="text-sm text-brand-light underline" href={`${peerOrigin}${path}`} target="_blank" rel="noopener noreferrer">{children} · otro entorno ↗</a>;
  }
  return <Link className="text-sm text-brand-light underline" to={path}>{children}</Link>;
}

export default function DeliveryPlan({ campaignId, onBusyChange }) {
  const [params, setParams] = useSearchParams();
  const portal = params.get("account") || "argentina";
  const period = campaignId ? "" : params.get("period") || "";
  const scope = useMemo(() => campaignId ? { campaign_id: campaignId } : { portal_id: portal, period }, [campaignId, portal, period]);
  const scopeKey = JSON.stringify(scope);
  const [data, setData] = useState(null), [error, setError] = useState(""), [message, setMessage] = useState("");
  const [loading, setLoading] = useState(true), [busy, setBusy] = useState(false);
  const [proposal, setProposal] = useState(null), [choice, setChoice] = useState(null), [generation, setGeneration] = useState(null);
  const [progress, setProgress] = useState(null), [selected, setSelected] = useState(new Set());
  const [filter, setFilter] = useState("all"), [query, setQuery] = useState(""), [page, setPage] = useState(1);
  const [accountDraft, setAccountDraft] = useState(""), [periodDraft, setPeriodDraft] = useState("");
  const requestRef = useRef(null), running = useRef(false);
  useEffect(() => { onBusyChange?.(busy); }, [busy, onBusyChange]);
  useEffect(() => () => onBusyChange?.(false), [onBusyChange]);
  const load = useCallback(async () => {
    requestRef.current?.abort();
    const controller = new AbortController(); requestRef.current = controller;
    setLoading(true);
    try {
      const next = await request(`/batch/delivery-plan?${new URLSearchParams(JSON.parse(scopeKey))}`, { signal: controller.signal });
      if (!controller.signal.aborted) {
        setData(next); setError("");
        setAccountDraft(next.portal_id || ""); setPeriodDraft(next.campaign_period || "");
      }
      return next;
    } catch (e) { if (!controller.signal.aborted) setError(e.message); return null; }
    finally { if (!controller.signal.aborted) setLoading(false); }
  }, [scopeKey]);
  useEffect(() => {
    setData(null); setProposal(null); setSelected(new Set()); setGeneration(null); setMessage(""); setPage(1);
    load(); return () => requestRef.current?.abort();
  }, [load]);
  const run = async fn => {
    if (running.current) return;
    running.current = true; setBusy(true); setError(""); setMessage("");
    try { await fn(); } catch (e) { setError(e.message); }
    finally { running.current = false; setBusy(false); }
  };
  const navigateScope = patch => {
    const next = new URLSearchParams(params);
    Object.entries(patch).forEach(([key, value]) => value ? next.set(key, value) : next.delete(key));
    setParams(next);
  };
  const preview = () => run(async () => {
    const result = await request("/batch/delivery-plan/preview", { method: "POST", body: JSON.stringify({ ...scope, fingerprint: data.fingerprint }) });
    setProposal(result);
  });
  const apply = () => run(async () => {
    await request("/batch/delivery-plan/apply", { method: "POST", body: JSON.stringify({ preview_id: proposal.preview_id }) });
    setProposal(null); setSelected(new Set()); await load();
    setMessage("Reparto guardado. Las canciones con letra y tiempos aprobados ya pueden generarse.");
  });
  const saveChoice = () => run(async () => {
    await request(`/batch/delivery-plan/campaigns/${choice.campaign_id}/items/${choice.item_id}`, {
      method: "PUT", body: JSON.stringify({ revision: choice.revision, type: choice.after_type, pinned: choice.after_pinned }),
    });
    setChoice(null); setProposal(null); setSelected(new Set()); await load(); setMessage("Fondo guardado.");
  });
  const associate = () => run(async () => {
    await request(`/batch/delivery-plan/campaigns/${campaignId}/account`, { method: "PUT", body: JSON.stringify({ portal_id: accountDraft, period: periodDraft }) });
    setProposal(null); await load(); setMessage("Cuenta y período guardados.");
  });
  const generate = () => run(async () => {
    const batch = [...generation], failures = []; let sent = 0;
    setProgress({ sent: 0, total: batch.length, title: "Preparando…" });
    for (const row of batch) {
      setProgress({ sent, total: batch.length, title: row.title });
      try {
        const job = await request(`/status/${row.job_id}`);
        if (job.status !== "lyrics_approved") throw new Error("Cambió el estado; revisá la canción.");
        await request("/generate", { method: "POST", body: campaignGenerateForm(row, job) });
        sent += 1;
      } catch (e) {
        failures.push(`${row.title}: ${e.message}`);
        if (e.status === 429 || e.status === 401 || e.status === 403) break;
      }
    }
    setGeneration(null); setProgress(null); setSelected(new Set()); await load();
    setMessage(`${sent} de ${batch.length} enviados a generación.${sent < batch.length ? " Los no enviados siguen pendientes; actualizá y volvé a seleccionarlos." : ""}`);
    if (failures.length) setError(failures.join(" · "));
  });
  const items = data?.items || [];
  const filtered = items.filter(row => matchesCampaignSong(query, row) && (
    (filter === "all" && ["pending", "in_progress"].includes(row.stage)) || filter === row.type || filter === row.stage || (filter === "ready" && row.ready)
    || (filter === "review" && !row.ready && row.stage === "pending" && row.status !== "lyrics_approved")
    || (filter === "pinned" && row.pinned)));
  const ready = filtered.filter(row => row.ready), eligible = ready.filter(row => selected.has(row.item_id));
  const pages = Math.max(1, Math.ceil(filtered.length / 25)), currentPage = Math.min(page, pages);
  const visible = filtered.slice((currentPage - 1) * 25, currentPage * 25);
  const total = bucket => Object.values(bucket || {}).reduce((a, b) => a + b, 0);
  const projected = data?.projected || {};
  const classified = (projected.veo || 0) + (projected.photo_effect || 0);
  const veoPercent = classified ? Math.round(projected.veo / classified * 100) : 0;
  const selectedMix = rows => `${rows.filter(r => r.type === "veo").length} Veo + ${rows.filter(r => r.type === "photo_effect").length} foto con efecto`;

  return <section className="space-y-5" aria-label="Plan de entrega">
    <div className="flex flex-wrap items-start justify-between gap-4">
      <div><p className="text-xs font-semibold uppercase tracking-[.16em] text-brand-light">{campaignId ? "Saldo de campaña" : "Cuenta cliente · portal de entregables"}</p><h2 className="mt-1 text-2xl font-bold">Plan de entrega</h2><p className="mt-1 text-sm text-ink-secondary">Equilibrá los fondos antes de generar. Objetivo de {campaignId ? "esta campaña" : "esta cuenta"}: 50% Veo y 50% foto + efecto.</p></div>
      <button className={button} disabled={busy || loading} onClick={() => { setProposal(null); load(); }}>Actualizar saldo</button>
    </div>
    {!campaignId && <fieldset disabled={busy} className="flex flex-wrap items-end gap-4 rounded-2xl bg-surface-2/40 p-4 ring-1 ring-white/10">
      <label className="space-y-1 text-xs text-ink-secondary"><span className="block">Cuenta cliente</span><select aria-label="Cuenta cliente" className={input} value={portal} onChange={e => navigateScope({ account: e.target.value })}>{Object.entries(accounts).map(([key, name]) => <option key={key} value={key}>{name}</option>)}</select></label>
      <label className="space-y-1 text-xs text-ink-secondary"><span className="block">Período</span><input aria-label="Período del saldo" type="month" className={input} value={period} onChange={e => navigateScope({ period: e.target.value })} /></label>
      <button className={button} onClick={() => navigateScope({ period: "" })}>Todo el acumulado</button>
      <p className="text-xs text-ink-secondary">{period ? "Entregas del mes + pendientes de campañas con ese período asignado." : "Todas las entregas vigentes y las campañas asociadas a esta cuenta."}</p>
    </fieldset>}
    {error && !proposal && !choice && !generation && <p role="alert" className="rounded-xl bg-red-500/10 p-4 text-sm text-red-200">{error}</p>}
    {message && <p role="status" className="rounded-xl bg-emerald-500/10 p-4 text-sm text-emerald-200">{message}</p>}
    {loading && <p role="status" className="text-sm text-ink-secondary">Actualizando saldo y asignaciones…</p>}
    {data && <>
      {campaignId && <div className="flex flex-wrap items-center gap-3 rounded-xl bg-white/[0.03] p-4 ring-1 ring-white/10">
        <span className="text-sm">Cuenta: <strong>{accounts[data.portal_id] || "Sin asociar"}</strong></span>
        {data.can_view_accounts && data.portal_id && <Link className="text-sm text-brand-light underline" to={`/campaigns?view=accounts&account=${data.portal_id}`}>Ver saldo de la cuenta</Link>}
        {data.can_manage && <details className="w-full"><summary className="cursor-pointer text-xs text-ink-secondary">{data.portal_id ? "Período de la campaña" : "Asociar al cliente del portal"}</summary><fieldset disabled={busy} className="mt-3 flex flex-wrap items-end gap-3"><label className="text-xs">Cuenta<select aria-label="Asociar cuenta" className={`${input} ml-2`} disabled={!!data.portal_id} value={accountDraft} onChange={e => setAccountDraft(e.target.value)}><option value="">Elegir cuenta</option>{Object.entries(accounts).map(([key, name]) => <option key={key} value={key}>{name}</option>)}</select></label><label className="text-xs">Mes del pedido<input aria-label="Mes del pedido" type="month" className={`${input} ml-2`} value={periodDraft} onChange={e => setPeriodDraft(e.target.value)} /></label><button className={button} disabled={!accountDraft} onClick={associate}>Guardar asociación</button></fieldset></details>}
      </div>}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Count value={total(data.buckets.delivered)} label="Lyric videos entregados vigentes" />
        <Count value={total(data.buckets.generated)} label="Generados sin entregar" />
        <Count value={total(data.buckets.pending)} label="Cargados, pendientes de generar" />
        <Count value={items.filter(r => r.ready).length} label="Listos para generar con fondo asignado" tone="bg-emerald-500/10" />
      </div>
      <div className="overflow-hidden rounded-2xl bg-surface-2/40 ring-1 ring-white/10">
        <div className="flex flex-wrap items-center justify-between gap-3 p-5"><h3 className="font-semibold">Saldo y proyección</h3><span className="rounded-full bg-brand/15 px-3 py-1 text-xs font-semibold text-brand-light">Objetivo 50 / 50</span></div>
        <div className="overflow-x-auto"><table className="w-full text-left text-sm"><thead className="bg-black/20 text-xs text-ink-secondary"><tr><th className="p-3">Fondo</th><th className="p-3 text-right">Entregados</th><th className="p-3 text-right">Generados sin entregar</th><th className="p-3 text-right">En generación</th><th className="p-3 text-right">Pendientes</th><th className="p-3 text-right">Proyectado</th></tr></thead><tbody>{Object.entries(labels).map(([key, label]) => <tr key={key} className="border-t border-white/5"><th className="p-3 font-medium">{label}</th>{["delivered", "generated", "in_progress", "pending"].map(stage => <td key={stage} className="p-3 text-right tabular-nums">{data.buckets[stage][key] || 0}</td>)}<td className="p-3 text-right font-semibold tabular-nums">{projected[key] || 0}</td></tr>)}</tbody></table></div>
        <div className="space-y-2 p-5"><div className="flex h-3 overflow-hidden rounded-full bg-white/5" aria-label={`Proyección clasificada: ${veoPercent}% Veo`}><div className="bg-violet-400" style={{ width: `${veoPercent}%` }} /><div className="bg-cyan-400" style={{ width: `${classified ? 100 - veoPercent : 0}%` }} /></div><p className="text-xs text-ink-secondary">{classified ? `${veoPercent}% Veo · ${100 - veoPercent}% foto con efecto entre los clasificados.` : "Todavía no hay fondos clasificados."} {projected.unknown > 0 && `${projected.unknown} sin asignar o verificar quedan fuera de ese porcentaje.`}</p><p className="text-xs text-ink-secondary">{data.excluded_art_tracks} Art Tracks aparte. Cada publicación se cuenta una vez por portal, sin multiplicar formatos.</p></div>
      </div>
      {data.warnings.map(text => <p key={text} className="rounded-xl bg-amber-500/10 p-3 text-sm text-amber-100">{text}</p>)}
      {!!data.issues?.length && <details className="rounded-xl bg-amber-500/10 p-4"><summary className="cursor-pointer text-sm font-semibold">Verificar {data.issues.length} fondos antes de equilibrar</summary><ul className="mt-3 space-y-3 text-sm">{data.issues.map((row, index) => <li key={`${row.job_id}-${index}`}><DeliveryReviewLink row={row} peerOrigin={data.peer_frontend_url}>{row.title}</DeliveryReviewLink><p className="mt-1 text-xs text-ink-secondary">{row.reason}</p></li>)}</ul></details>}
      {!!data.unmapped_campaigns.length && <details className="rounded-xl bg-white/[0.03] p-4"><summary className="cursor-pointer text-sm">Revisar {data.unmapped_campaigns.length} campañas sin asociación completa</summary><ul className="mt-3 space-y-2 text-sm">{data.unmapped_campaigns.map(c => <li key={c.id}><Link className="text-brand-light underline" to={`/campaigns/${c.id}?view=plan`}>{c.name}</Link> · {c.reason === "account" ? "Falta cuenta" : "Falta período"}</li>)}</ul></details>}
      <div className="rounded-2xl bg-brand/10 p-5 ring-1 ring-brand/25">
        <div className="flex flex-wrap items-center justify-between gap-4"><div><h3 className="font-semibold">Equilibrar pendientes</h3><p className="mt-1 text-sm">Propuesta: <strong>{data.suggestion.veo} Veo</strong> y <strong>{data.suggestion.photo_effect} foto + efecto</strong> entre {data.suggestion.total} canciones disponibles.</p><p className="mt-1 text-xs text-ink-secondary">Conserva elecciones fijadas y videos ya generados. Podés revisar cada canción antes de guardar.</p></div><button className={primary} disabled={busy || loading || !data.can_manage || !!data.blockers.length} onClick={preview}>Revisar reparto</button></div>
        {data.suggestion.odd_total && <p className="mt-3 text-xs text-amber-100">El total es impar: la propuesta deja una foto adicional. El redondeo queda registrado al aplicar.</p>}
        {data.suggestion.remaining_gap > 1 && <p className="mt-3 text-sm text-amber-100">No alcanzan las pendientes disponibles para equilibrar: quedaría una diferencia de {data.suggestion.remaining_gap} videos.</p>}
        {data.blockers.map(text => <p key={text} className="mt-2 text-sm text-amber-100">{text}</p>)}
      </div>
      {!campaignId && <details className="rounded-xl p-4 ring-1 ring-white/10"><summary className="cursor-pointer text-sm font-semibold">Campañas de esta cuenta ({data.campaigns.length})</summary><div className="mt-3 grid gap-2 sm:grid-cols-2">{data.campaigns.map(c => <div key={c.id} className="rounded-lg bg-white/[0.03] p-3"><Link className="text-sm text-brand-light underline" to={`/campaigns/${c.id}?view=plan`}>{c.name}</Link><p className="mt-1 text-xs text-ink-secondary">{c.pending} pendientes · {c.ready} listas{!c.writable && " · Consultada desde otro entorno"}</p></div>)}</div></details>}
      <div className="overflow-hidden rounded-2xl bg-surface-2/40 ring-1 ring-white/10">
        <div className="space-y-3 p-5"><h3 className="font-semibold">Asignar fondos y generar</h3><fieldset disabled={busy} className="flex flex-wrap gap-3"><input aria-label="Buscar canción del plan" className={`${input} flex-1`} placeholder="Buscar canción o artista" value={query} onChange={e => { setQuery(e.target.value); setSelected(new Set()); setPage(1); }} /><select aria-label="Filtrar plan" className={input} value={filter} onChange={e => { setFilter(e.target.value); setSelected(new Set()); setPage(1); }}>{[["all", "Todas las pendientes"], ["unknown", "Sin fondo asignado"], ["veo", "Veo"], ["photo_effect", "Foto + efecto"], ["ready", "Listas para generar"], ["review", "Pendiente de revisar letra"], ["pinned", "Elecciones fijadas"], ["generated", "Generados sin entregar"], ["delivered", "Entregados al portal"]].map(([id, label]) => <option key={id} value={id}>{label}</option>)}</select></fieldset>
          <div className="flex flex-wrap items-center gap-3"><button className={button} disabled={busy || loading || !ready.length} onClick={() => setSelected(new Set(ready.map(r => r.item_id)))}>Seleccionar listas ({ready.length})</button><button className={button} disabled={busy || !selected.size} onClick={() => setSelected(new Set())}>Limpiar</button><span className="text-sm text-ink-secondary">{eligible.length} seleccionadas · {selectedMix(eligible)}</span><button className={`${primary} ml-auto`} disabled={busy || loading || !eligible.length} onClick={() => setGeneration(eligible)}>Generar seleccionadas ({eligible.length})</button></div>
        </div>
        <div className="overflow-x-auto"><table className="w-full min-w-[780px] text-left text-sm"><thead className="bg-black/20 text-xs text-ink-secondary"><tr><th className="p-3">Elegir</th><th className="p-3">Canción / campaña</th><th className="p-3">Fondo</th><th className="p-3">Estado</th><th className="p-3">Acción</th></tr></thead><tbody>{visible.map(row => <tr key={row.item_id} className="border-t border-white/5"><td className="p-3"><input type="checkbox" aria-label={`Generar ${row.title}`} disabled={busy || loading || !row.ready} checked={selected.has(row.item_id)} onChange={e => setSelected(prev => { const next = new Set(prev); e.target.checked ? next.add(row.item_id) : next.delete(row.item_id); return next; })} /></td><td className="p-3"><strong className="font-medium">{row.title}</strong><p className="mt-1 text-xs text-ink-secondary">{row.artist}</p>{!campaignId && <p className="mt-1 text-xs text-ink-secondary">{row.campaign_name}</p>}</td><td className="p-3"><span className={`rounded-full px-2 py-1 text-xs ${row.type === "unknown" ? "bg-amber-500/15 text-amber-100" : row.type === "veo" ? "bg-violet-500/15 text-violet-200" : "bg-cyan-500/15 text-cyan-200"}`}>{row.type === "photo_effect" ? `Foto + ${row.effect || "efecto"}` : row.type === "unknown" ? "Sin asignar" : "Veo"}</span>{row.pinned && <p className="mt-2 text-xs text-ink-secondary">Elección fijada</p>}</td><td className="p-3 text-xs text-ink-secondary">{row.blocked_reason || (row.ready ? "Lista para generar" : row.type === "unknown" && row.status === "lyrics_approved" ? "Falta elegir fondo" : "Revisar letra y tiempos")}</td><td className="p-3">{["generated", "delivered"].includes(row.stage) ? <DeliveryReviewLink row={row} peerOrigin={data.peer_frontend_url}>{row.stage === "delivered" ? "Ver entregable" : "Revisar video"}</DeliveryReviewLink> : <><button className={button} disabled={busy || loading || !row.editable} onClick={() => setChoice({ ...row, after_type: row.type === "unknown" ? "photo_effect" : row.type, after_pinned: true })}>Elegir fondo</button>{row.job_id && !row.ready && row.editable && <Link className="mt-2 block text-xs text-brand-light underline" to={`/campaigns/${row.campaign_id}?view=lyrics&focus=${row.job_id}`}>Revisar canción</Link>}</>}</td></tr>)}{!visible.length && <tr><td colSpan="5" className="p-8 text-center text-ink-secondary">No hay canciones con este filtro.</td></tr>}</tbody></table></div>
        <div className="flex items-center justify-between gap-3 p-4 text-sm"><span>{filtered.length} canciones · Página {currentPage} de {pages}</span><div className="flex gap-2"><button className={button} disabled={busy || currentPage <= 1} onClick={() => setPage(currentPage - 1)}>Anterior</button><button className={button} disabled={busy || currentPage >= pages} onClick={() => setPage(currentPage + 1)}>Siguiente</button></div></div>
      </div>
    </>}
    {proposal && <Modal title="Revisar reparto de fondos" busy={busy} onClose={() => setProposal(null)}>{error && <p role="alert" className="mb-4 text-sm text-red-200">{error}</p>}<p className="text-sm">Asignar <strong>{proposal.suggestion.veo} Veo</strong> y <strong>{proposal.suggestion.photo_effect} foto + efecto</strong>. La proyección queda en {proposal.suggestion.projected_veo} Veo / {proposal.suggestion.projected_photo_effect} foto.</p><p className="mt-2 text-xs text-ink-secondary">Guardar sólo asigna los fondos. La generación se confirma por separado.</p><div className="my-4 max-h-80 overflow-y-auto"><table className="w-full text-left text-sm"><thead><tr><th className="p-2">Canción</th><th className="p-2">Actual</th><th className="p-2">Propuesto</th></tr></thead><tbody>{proposal.changes.map(row => <tr key={row.item_id} className="border-t border-white/10"><td className="p-2">{row.title}<p className="text-xs text-ink-secondary">{row.campaign_name}</p></td><td className="p-2">{labels[row.type]}</td><td className="p-2">{labels[row.after_type]}</td></tr>)}</tbody></table></div><button className={primary} disabled={busy} onClick={apply}>{busy ? "Guardando…" : "Aplicar asignaciones"}</button></Modal>}
    {choice && <Modal title={`Fondo de ${choice.title}`} busy={busy} onClose={() => setChoice(null)}>{error && <p role="alert" className="mb-4 text-sm text-red-200">{error}</p>}<label className="block text-sm">Fondo<select aria-label="Fondo de la canción" className={`${input} ml-3`} disabled={busy} value={choice.after_type} onChange={e => setChoice({ ...choice, after_type: e.target.value })}><option value="photo_effect">Foto fija + efecto</option><option value="veo">Veo</option></select></label><label className="my-4 flex items-center gap-2 text-sm"><input type="checkbox" disabled={busy} checked={choice.after_pinned} onChange={e => setChoice({ ...choice, after_pinned: e.target.checked })} />Mantener este fondo al equilibrar</label><p className="mb-4 text-xs text-ink-secondary">Foto conserva el efecto elegido, o usa Bokeh si todavía no hay uno. Podés ajustar el diseño en Generar videos.</p><button className={primary} disabled={busy} onClick={saveChoice}>Guardar fondo</button></Modal>}
    {generation && <Modal title="Confirmar generación" busy={busy} onClose={() => setGeneration(null)}>{error && <p role="alert" className="mb-4 text-sm text-red-200">{error}</p>}<p>Generar <strong>{generation.length} videos: {selectedMix(generation)}</strong>.</p><p className="mt-2 text-sm text-ink-secondary">Genera los fondos y videos y consume el cupo correspondiente.</p><ul className="my-4 max-h-60 overflow-y-auto text-sm">{generation.map(row => <li key={row.item_id} className="py-1">{row.title} · {labels[row.type]}</li>)}</ul>{progress && <p role="status" className="my-3 rounded-xl bg-brand/15 p-3">Enviados {progress.sent}/{progress.total} · {progress.title}</p>}<button className={primary} disabled={busy} onClick={generate}>{busy ? "Enviando…" : "Confirmar generación"}</button></Modal>}
  </section>;
}
