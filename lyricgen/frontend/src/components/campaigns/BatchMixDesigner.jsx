import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useI18n } from "../../i18n";
import { lyricLookOptions, LYRIC_LOOK_LABELS } from "../../lib/lyricLooks";
import { EFFECT_LABELS } from "../../lib/optionLabels";
import { campaignPost, campaignRequest } from "../../lib/campaignApi";
import StyleAssignment from "./StyleAssignment";
import { Banner, Button, inputClass } from "./ui";

const toggle = (values, value) => values.includes(value) ? values.filter((v) => v !== value) : [...values, value];

export function projectBatchBalance(report, campaignId, changes) {
  const after = { ...report.projected };
  const pending = new Map(report.items.filter((r) => r.campaign_id === campaignId && r.stage === "pending").map((r) => [r.item_id, r]));
  for (const change of changes) {
    const row = pending.get(change.item_id);
    if (!row || !Object.hasOwn(after, row.type) || after[row.type] < 1) return null;
    after[row.type] -= 1;
    after[change.after.movement_style === "foto-parallax" ? "photo_effect" : "veo"] += 1;
  }
  return after;
}

export function BatchAccountBalance({ campaignId, creative, changes, disabled = false }) {
  const [state, setState] = useState({ loading: true });
  const account = creative.can_view_account_balance && ["argentina", "chile"].includes(creative.portal_id);
  const period = creative.delivery_plan_period || "";
  useEffect(() => {
    const controller = new AbortController();
    setState({ loading: true });
    const query = new URLSearchParams(account ? { portal_id: creative.portal_id, period } : { campaign_id: campaignId });
    campaignRequest(`/batch/delivery-plan?${query}`, { signal: controller.signal }).then((data) => {
      if (!controller.signal.aborted) setState({ data });
    }).catch(() => { if (!controller.signal.aborted) setState({ error: true }); });
    return () => controller.abort();
  }, [account, campaignId, creative.portal_id, period]);
  const report = state.data;
  const projection = report && projectBatchBalance(report, campaignId, changes);
  return <aside className="rounded-xl bg-black/20 p-4 ring-1 ring-white/10" aria-label="Saldo contractual">
    <h4 className="font-semibold">{account ? "Saldo de la cuenta" : "Saldo de la campaña"} · objetivo 50/50</h4>
    <p className="mt-1 text-xs text-ink-secondary">{account ? `${report?.name || (creative.portal_id === "chile" ? "Universal Chile" : "Universal Argentina")} · ${period || "histórico completo"}` : "La vista completa de cuenta requiere un administrador."}</p>
    {state.loading && <p role="status" className="mt-2 text-sm">Consultando saldo…</p>}
    {(state.error || (report && !projection)) && <p role="status" className="mt-2 text-sm text-amber-200">No pudimos verificar el impacto de este lote. No se puede confirmar el saldo 50/50 de la cuenta.</p>}
    {projection && <>
      <div className="mt-3 overflow-x-auto"><table className="w-full text-left text-sm"><thead className="text-xs text-ink-secondary"><tr><th className="py-2">Proyección</th><th>Foto + efecto</th><th>Video</th><th>Sin definir</th></tr></thead><tbody>
        <tr><td className="py-2">Antes del lote</td><td>{report.projected.photo_effect}</td><td>{report.projected.veo}</td><td>{report.projected.unknown}</td></tr>
        <tr className="font-semibold text-white"><td className="py-2">Con este lote</td><td>{projection.photo_effect}</td><td>{projection.veo}</td><td>{projection.unknown}</td></tr>
      </tbody></table></div>
      <p className="mt-2 text-xs text-ink-secondary">Incluye entregados, generados, en proceso y pendientes asignados. Es una proyección, no un conteo de entregas confirmadas.</p>
      <p className="mt-2 text-sm text-amber-200">{projection.unknown ? "Hay fondos sin definir: todavía no se puede verificar el 50/50." : Math.abs(projection.photo_effect - projection.veo) > 1 ? "Repartir este lote 50/50 no corrige necesariamente el saldo acumulado." : "Las cantidades proyectadas quedan equilibradas; el cumplimiento se verifica con las entregas finales."}</p>
      {report.warnings?.map((warning) => <p className="mt-2 text-xs text-amber-200" key={warning}>{warning}</p>)}
    </>}
    <Link aria-disabled={disabled} tabIndex={disabled ? -1 : undefined} onClick={(e) => { if (disabled) e.preventDefault(); }} className="mt-3 inline-block text-sm text-brand-light underline" to={`/campaigns/${encodeURIComponent(campaignId)}?view=plan`}>Ver Plan de entrega</Link>
  </aside>;
}

/** Rolling deploys can use the existing editor until the API exposes the new catalogue. */
export default function BatchDesign(props) {
  return props.creative.mix_catalog ? <BatchMixDesigner {...props} /> : <StyleAssignment {...props} />;
}

export function BatchMixDesigner({ campaignId, creative, itemIds, onSaved, onBusyChange, onPrepareGeneration }) {
  const { t } = useI18n();
  const catalog = creative.mix_catalog;
  const lookLabels = LYRIC_LOOK_LABELS(t), effectLabels = EFFECT_LABELS(t);
  const options = lyricLookOptions(t).filter((look) => catalog.looks.some((v) => v.id === look.code));
  const availableLooks = catalog.looks.filter((look) => look.compatible).map((look) => look.id);
  const [looks, setLooks] = useState(() => ["pincel", "cosmico", "cine"].filter((look) => availableLooks.includes(look)));
  const [effects, setEffects] = useState(() => ["bokeh", "embers"].filter((effect) => catalog.effects.includes(effect)));
  const [photoPercent, setPhotoPercent] = useState(50);
  const [replace, setReplace] = useState(false);
  const [seed, setSeed] = useState(() => crypto.randomUUID());
  const [preview, setPreview] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState("");
  const [advanced, setAdvanced] = useState(false);
  const [step, setStep] = useState(1);
  const running = useRef(false), review = useRef(null);
  const requested = new Set(itemIds);
  const eligible = creative.items.filter((item) => requested.has(item.id) && !item.discarded && catalog.editable_statuses.includes(item.status));
  const fixed = eligible.filter((item) => item.assignment?.pinned);
  const count = eligible.length - (replace ? 0 : fixed.length);
  const photoCount = Math.floor((count * photoPercent + 50) / 100);
  const readyCount = preview?.ready_item_ids?.length || 0;
  const readyIds = new Set(preview?.ready_item_ids || []);
  const readyPhotos = preview?.changes.filter((change) => readyIds.has(change.item_id) && change.after.movement_style === "foto-parallax").length || 0;
  const disabled = !count || !looks.length || (photoCount > 0 && !effects.length);
  const selectionKey = [...itemIds].sort().join(",");
  useEffect(() => { setPreview(null); setSaved(""); setStep(1); }, [selectionKey, creative.plan.revision]);
  useEffect(() => { onBusyChange?.(busy); }, [busy, onBusyChange]);
  useEffect(() => { if (preview) review.current?.scrollIntoView?.({ behavior: "smooth", block: "start" }); }, [preview]);
  const change = (fn) => { setPreview(null); setSaved(""); setError(""); if (step === 3) setStep(2); fn(); };
  const run = async (fn) => {
    if (running.current) return;
    running.current = true; setBusy(true); setError("");
    try { await fn(); } catch (e) { setError(e.message); } finally { running.current = false; setBusy(false); }
  };
  const calculate = () => run(async () => {
    setPreview(await campaignPost(`/batch/campaigns/${campaignId}/creative/mix-preview`, {
      revision: creative.plan.revision, item_ids: eligible.map((item) => item.id),
      looks, effects, photo_percent: photoPercent, seed, replace_exceptions: replace,
    }));
    setStep(3);
  });
  const confirm = (generate) => run(async () => {
    const frozen = preview;
    const applied = await campaignPost(`/batch/campaigns/${campaignId}/creative/apply`, { preview_id: frozen.preview_id });
    await onSaved?.();
    if (generate) await onPrepareGeneration?.({ startImmediately: true, itemIds: frozen.ready_item_ids, expectedRevision: applied.revision });
    setPreview(null); setStep(1); setSaved(generate ? "Diseño guardado. Preparando el envío…" : `Reparto guardado para ${frozen.mix.total} canciones. Todavía no se generaron videos.`);
  });
  if (advanced) return <div className="space-y-4"><Button disabled={busy} variant="ghost" onClick={() => { setAdvanced(false); setPreview(null); }}>← Volver al reparto simple</Button>
    <StyleAssignment campaignId={campaignId} creative={creative} itemIds={itemIds} onSaved={onSaved} onBusyChange={(value) => { setBusy(value); onBusyChange?.(value); }} onPrepareGeneration={onPrepareGeneration} />
  </div>;
  return <fieldset disabled={busy} className="space-y-6" aria-label="Diseño del lote">
    <div className="rounded-xl bg-brand/10 p-4 ring-1 ring-brand/25">
      <p className="text-lg font-semibold">{count} canciones para tu nuevo lote</p>
      <p className="mt-1 text-sm text-ink-secondary">Elegí los diseños. Repartimos las cantidades y mezclamos qué canción recibe cada combinación.</p>
      {itemIds.length > eligible.length && <p className="mt-2 text-sm text-amber-200">{itemIds.length - eligible.length} seleccionadas no admiten una nueva asignación. Los videos ya generados se cambian desde Editar.</p>}
      {fixed.length > 0 && <div className="mt-3 text-sm"><p>{fixed.length} diseños fijados: {replace ? "se incluirán en el nuevo reparto." : "se conservan y no se enviarán en este lote."}</p>
        <label className="mt-2 flex items-center gap-2"><input type="checkbox" checked={replace} onChange={(e) => change(() => setReplace(e.target.checked))} />También cambiar los diseños fijados</label></div>}
    </div>
    <nav aria-label="Pasos del lote" className="grid grid-cols-3 gap-2">{[[1, "Fondos y efectos"], [2, "Looks"], [3, "Revisar"]].map(([number, label]) => <button type="button" key={number} disabled={busy || (number === 3 && !preview)} aria-current={step === number ? "step" : undefined} onClick={() => setStep(number)} className={`rounded-lg px-2 py-3 text-sm font-semibold ${step === number ? "bg-brand/20 text-brand-light ring-1 ring-brand/40" : "bg-white/5 text-ink-secondary disabled:opacity-40"}`}>{number}. {label}</button>)}</nav>
    {step === 1 && <section aria-label="Reparto de fondos" className="space-y-3">
      <h3 className="text-lg font-semibold"><span className="mr-2 text-brand-light">1.</span>¿Cuántos con cada fondo?</h3>
      <div className="grid grid-cols-3 gap-2">{[[50, "Mitad foto / mitad video"], [100, "Todas con foto"], [0, "Todas con video"]].map(([value, label]) => <Button key={value} className="!h-auto !whitespace-normal py-3" variant={photoPercent === value ? "primary" : "secondary"} aria-pressed={photoPercent === value} onClick={() => change(() => setPhotoPercent(value))}>{label}</Button>)}</div>
      <div className="flex flex-wrap items-center gap-3"><label htmlFor={`mix-percent-${campaignId}`} className="text-sm">Foto + efecto (%)</label><input id={`mix-percent-${campaignId}`} className={`${inputClass} !w-24`} type="number" min="0" max="100" step="1" value={photoPercent} onChange={(e) => change(() => setPhotoPercent(Math.min(100, Math.max(0, Math.round(Number(e.target.value) || 0)))))} /><span className="text-sm text-ink-secondary">Video: {100 - photoPercent}%</span></div>
      <div className="flex overflow-hidden rounded-xl text-center text-sm font-semibold" aria-label={`${photoCount} fotos y ${count - photoCount} videos`}>
        {photoCount > 0 && <div className="min-w-fit bg-violet-500/25 px-4 py-3" style={{ flex: photoCount }}>{photoCount} foto + efecto</div>}
        {count - photoCount > 0 && <div className="min-w-fit bg-cyan-500/20 px-4 py-3" style={{ flex: count - photoCount }}>{count - photoCount} video · Veo Lite</div>}
      </div>
      {count * photoPercent % 100 !== 0 && <p className="text-xs text-ink-secondary">El porcentaje se redondea a canciones enteras; ante un empate, la extra va a foto.</p>}
      <p className="text-xs text-ink-secondary">Este reparto corresponde al lote seleccionado. El saldo de la cuenta se muestra antes de confirmar.</p>
    </section>}
    {step === 2 && <section aria-label="Looks del nuevo pack" className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2"><h3 className="text-lg font-semibold"><span className="mr-2 text-brand-light">2.</span>¿Qué looks querés mezclar?</h3><Button size="sm" variant="ghost" onClick={() => change(() => setLooks(looks.length === availableLooks.length ? [] : availableLooks))}>{looks.length === availableLooks.length ? "Quitar todos" : "Elegir todos los compatibles"}</Button></div>
      <p className="text-sm text-ink-secondary">{looks.length} elegidos · rotan con cantidades iguales o una canción de diferencia. Las variantes de cada look quedan en automático.</p>
      <div className="grid max-h-[42vh] grid-cols-2 gap-3 overflow-y-auto p-1 sm:grid-cols-3 lg:grid-cols-4">{options.map((look) => {
        const rule = catalog.looks.find((v) => v.id === look.code), chosen = looks.includes(look.code);
        return <button key={look.code} type="button" disabled={!rule.compatible || busy} aria-pressed={chosen} aria-label={`Look ${look.label}`} title={rule.reason || look.desc}
          onClick={() => change(() => setLooks(toggle(looks, look.code)))} className={`overflow-hidden rounded-xl text-left ring-2 transition ${chosen ? "ring-brand bg-brand/10" : "ring-white/10 bg-white/[0.02]"} ${!rule.compatible ? "opacity-45" : "hover:ring-brand/70"}`}>
          <span aria-hidden="true" className="relative flex h-24 items-center justify-center px-2 text-center text-xl" style={{ background: look.preview?.thumbBg || "#151528", fontFamily: look.css, fontWeight: look.weight, color: look.preview?.color || "white", textShadow: look.preview?.textShadow }}><span>Tu canción</span><span className="absolute right-2 top-1 font-sans text-sm">{chosen ? "✓" : "+"}</span></span>
          <span className="block px-3 py-2 text-sm font-semibold">{look.label}</span><span className="block px-3 pb-3 text-xs text-ink-secondary">{rule.compatible ? look.desc : rule.reason}</span>
        </button>;
      })}</div>
      {!looks.length && <p className="text-sm text-amber-200">Elegí al menos un look.</p>}
      <p className="text-xs text-ink-secondary">Muestras orientativas. Los looks que pueden reemplazar el fondo están deshabilitados para conservar el reparto foto/video.</p>
    </section>}
    {step === 1 && photoCount > 0 && <section aria-label="Efectos para las fotos" className="space-y-3">
      <h3 className="text-lg font-semibold">Elegí los efectos de las fotos</h3>
      <p className="text-sm text-ink-secondary">{effects.length} elegidos · se reparten entre las {photoCount} fotos, con una diferencia máxima de una canción. Cada foto recibe un efecto.</p>
      <div className="flex max-h-60 flex-wrap gap-2 overflow-y-auto p-1">{catalog.effects.map((effect) => <Button key={effect} size="sm" variant={effects.includes(effect) ? "primary" : "secondary"} aria-pressed={effects.includes(effect)} onClick={() => change(() => setEffects(toggle(effects, effect)))}>{effectLabels[effect] || effect}</Button>)}</div>
      {!effects.length && <p className="text-sm text-amber-200">Elegí al menos un efecto.</p>}
    </section>}
    {error && <Banner tone="danger">{error}</Banner>}
    {saved && <Banner tone="success">{saved}</Banner>}
    {step !== 3 && <div className="sticky bottom-0 z-10 flex flex-wrap items-center justify-between gap-3 rounded-xl bg-surface-2 p-3 ring-1 ring-white/10">
      {step === 1 ? <Button variant="ghost" onClick={() => change(() => setAdvanced(true))}>Configuración avanzada</Button> : <Button variant="ghost" onClick={() => setStep(1)}>← Fondos y efectos</Button>}
      {step === 1 ? <Button variant="primary" disabled={!count || (photoCount > 0 && !effects.length)} onClick={() => setStep(2)}>Elegir looks →</Button>
        : <Button variant="primary" disabled={disabled || busy} onClick={calculate}>{busy ? "Calculando…" : `Revisar reparto de ${count} canciones`}</Button>}
    </div>}
    {step === 3 && preview && <section ref={review} aria-label="Revisar lote" className="space-y-4 rounded-2xl bg-brand/[0.08] p-4 ring-1 ring-brand/30">
      <div className="flex flex-wrap items-center justify-between gap-2"><h3 className="text-xl font-semibold">Tu lote, antes de generar</h3><Button variant="ghost" size="sm" onClick={() => setStep(1)}>Ajustar lote</Button></div>
      <p className="text-lg"><strong>{preview.mix.photo_effect}</strong> foto + efecto <span className="mx-2 text-ink-secondary">+</span><strong>{preview.mix.veo}</strong> video = <strong>{preview.mix.total} canciones</strong></p>
      <div className="grid gap-4 sm:grid-cols-2"><div><h4 className="mb-2 font-semibold">Looks</h4>{Object.entries(preview.mix.looks).map(([look, n]) => <p key={look} className="flex justify-between gap-4 text-sm"><span>{lookLabels[look] || look}</span><strong>{n}</strong></p>)}</div><div><h4 className="mb-2 font-semibold">Efectos en las fotos</h4>{Object.entries(preview.mix.effects).map(([effect, n]) => <p key={effect} className="flex justify-between gap-4 text-sm"><span>{effectLabels[effect] || effect}</span><strong>{n}</strong></p>)}{!preview.mix.photo_effect && <p className="text-sm text-ink-secondary">Este lote no incluye fotos.</p>}</div></div>
      {preview.skipped.length > 0 && <p className="text-sm text-amber-200">{preview.skipped.length} diseños fijados quedan fuera de este reparto y de la generación.</p>}
      {readyCount < preview.mix.total && <div className="space-y-1 rounded-xl bg-amber-400/10 p-3 text-sm text-amber-200">
        <p>{readyCount} listas para generar. Las otras {preview.mix.total - readyCount} conservarán el diseño y necesitan terminar la revisión de letra y timing.</p>
        <p>Se enviarán ahora: <strong>{readyPhotos} foto + efecto y {readyCount - readyPhotos} video.</strong> El reparto total se completa cuando generes las canciones restantes.</p>
      </div>}
      <details><summary className="cursor-pointer text-sm text-brand-light">Ver diseño por canción</summary><ul className="mt-2 max-h-64 space-y-2 overflow-auto text-sm">{preview.changes.map((c) => <li key={c.item_id} className="border-b border-white/5 pb-2"><strong>{c.artist} · {c.title}</strong><span className="block text-ink-secondary">{lookLabels[c.after.lyric_look] || c.after.lyric_look} · {c.after.movement_style === "foto-parallax" ? `Foto + ${effectLabels[c.after.effect] || c.after.effect}` : "Video · Veo Lite"}</span></li>)}</ul></details>
      <BatchAccountBalance campaignId={campaignId} creative={creative} changes={preview.changes} disabled={busy} />
      <p className="text-xs text-ink-secondary">Los fondos se inspiran en cada letra. Generar consume el cupo y los recursos correspondientes. La cola puede enviar una parte y dejar el resto pendiente si alcanza su límite.</p>
      <div className="flex flex-wrap items-center gap-2"><Button variant="ghost" onClick={() => change(() => setSeed(crypto.randomUUID()))}>Mezclar de nuevo</Button><Button variant="secondary" onClick={() => confirm(false)}>Guardar reparto</Button>{onPrepareGeneration && <Button variant="primary" disabled={!readyCount || busy} onClick={() => confirm(true)}>{busy ? "Preparando…" : `Generar ${readyCount} videos`}</Button>}</div>
      <p className="text-xs text-ink-secondary">Mezclar de nuevo conserva las cantidades por fondo y vuelve a repartir los diseños antes de confirmar.</p>
    </section>}
  </fieldset>;
}
