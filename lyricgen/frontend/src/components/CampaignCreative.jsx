import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useI18n } from "../i18n";
import { MOVEMENT_LABELS, EFFECT_LABELS, FONT_LABELS, AXIS_VALUE_LABELS, dynamicAxisLabel } from "../lib/optionLabels";
import { campaignGenerateForm } from "../lib/campaignCreative";
import WizardLivePreview from "./WizardLivePreview";
import useBackgroundPreviewTokens, { backgroundPreviewUrl } from "../hooks/useBackgroundPreviewTokens";
import { useLazyMediaUrl } from "../mediaUrl";

const API = import.meta.env.VITE_API_URL || "";
const input = "w-full rounded-lg bg-black/30 px-3 py-2 text-sm text-white ring-1 ring-white/15";
const button = "rounded-lg bg-brand/20 px-4 py-2 text-sm text-brand-light disabled:opacity-40";
const statusLabels = { waiting: "Esperando audio", transcribing: "Transcribiendo", transcribing_queued: "Transcripción en cola", transcribed: "Lista para revisar", transcribed_pending: "Lista para revisar", lyrics_approved: "Letra aprobada", queued: "En cola de generación", processing: "Generando", rendering: "Renderizando", editing: "Generando nueva versión", pending_review: "Pendiente de revisión final", done: "Aprobado", error: "Requiere atención", rejected: "Rechazado", discarded: "Descartada" };
const sourceLabels = { lyrics: "Inspirado en la letra", auto: "Automático", prompt_literal: "Prompt exacto", prompt_improved: "Prompt mejorado con IA", as_is: "Usar tal cual", variation: "Crear variación" };

async function request(path, options = {}) {
  const response = await fetch(`${API}${path}`, { cache: "no-store", ...options,
    headers: { Authorization: `Bearer ${localStorage.getItem("genly_token") || ""}`, ...options.headers } });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : data.detail?.message || data.detail?.code || data.code || `Error ${response.status}`);
  return data;
}
const post = (path, value) => request(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(value) });

function StylePreview({ settings: s, assets }) {
  const asset = assets.find(a => a.id === s.background_id);
  const tokens = useBackgroundPreviewTokens(asset ? [asset.id] : [], API);
  const photo = asset ? asset.file_type !== "mp4" : s.movement_style === "foto-parallax";
  const source = asset ? backgroundPreviewUrl(API, asset.id, tokens[asset.id]) : photo ? "/movement_samples/foto-fija.jpg" : `/movement_samples/${s.movement_style || "estandar"}.mp4`;
  return <div className="max-w-xl"><WizardLivePreview placeholderBg={!asset} clipSrc={source || ""} clipIsVideo={!photo}
    operatorPhoto={!!asset && photo} photoAnimated={s.animate_image} style={s.style} customColors={s.custom_colors}
    movementStyle={s.movement_style} effect={s.effect} font={s.font} fontScale={s.font_scale} textCase={s.text_case}
    textContrast={s.text_contrast} lyricsAnimation={s.lyrics_animation} lineTransition={s.line_transition}
    lyricColor={s.lyric_color} lyricSungColor={s.lyric_sung_color} frameFormat={s.frame_format}
    lyric="Así se verá la letra" /><p className="mt-2 text-xs text-ink-secondary">Muestra visual del estilo. El fondo IA definitivo se genera después de aprobar letra y tiempos.</p></div>;
}

function VideoThumbnail({ video }) {
  const { ref, url } = useLazyMediaUrl(video.video_url ? video.job_id : null, "thumbnail", "preview", { version: video.evidence?.video_sha256 || "" });
  return <div ref={ref} className="aspect-video overflow-hidden rounded-lg bg-black/30">{url ? <img src={url} alt={`Video de ${video.title}`} className="h-full w-full object-cover" /> : <div className="grid h-full place-items-center text-sm text-ink-secondary">{video.video_url ? "Vista previa" : "Generación en curso"}</div>}</div>;
}

function Controls({ fields, values, update, assets, label }) {
  const { t } = useI18n();
  const labels = (key, value) => (key === "effect" ? EFFECT_LABELS(t)[value] : key === "movement_style" ? MOVEMENT_LABELS(t)[value]
    : key.includes("font") ? FONT_LABELS(t)[value] : AXIS_VALUE_LABELS(t)[key]?.[value] || sourceLabels[value] || dynamicAxisLabel(t, key, value)) || value || "Auto / ninguno";
  return <div className="space-y-3">{[...new Set(Object.values(fields).map(f => f.group))].map(group => <details key={group} className="rounded-lg bg-black/15 p-3">
    <summary className="cursor-pointer text-sm font-semibold">{group}</summary>
    <div className="mt-3 grid gap-3 md:grid-cols-2">{Object.entries(fields).filter(([, f]) => f.group === group).map(([key, f]) => {
      const enabled = Object.hasOwn(values, key);
      const id = `${label}-${key}`;
      return <div key={key}><label className="mb-2 flex gap-2 text-xs"><input type="checkbox" checked={enabled} onChange={e => update(key, e.target.checked ? f.kind === "boolean" ? false : f.kind === "number" ? 1 : f.kind === "asset" ? null : f.kind === "color" ? "#FFFFFF" : f.options?.[0] ?? "" : undefined)} />Cambiar {f.label}</label>
        {enabled && (f.kind === "select" || f.kind === "asset" ? <select id={id} aria-label={`${label}: ${f.label}`} className={input} value={values[key] ?? ""} onChange={e => update(key, f.kind === "asset" ? e.target.value ? Number(e.target.value) : null : e.target.value)}>
          {f.kind === "asset" ? <><option value="">Generar con IA</option>{assets.map(a => <option key={a.id} value={a.id}>{a.name} · {a.file_type}</option>)}</> : f.options.map(v => <option key={v} value={v}>{labels(key, v)}</option>)}
        </select> : f.kind === "boolean" ? <label className="flex gap-2 text-sm"><input aria-label={`${label}: ${f.label}`} type="checkbox" checked={values[key]} onChange={e => update(key, e.target.checked)} />Activado</label>
          : f.kind === "textarea" ? <textarea aria-label={`${label}: ${f.label}`} className={input} maxLength={f.max_length} value={values[key]} onChange={e => update(key, e.target.value)} />
          : <input aria-label={`${label}: ${f.label}`} className={input} type={f.kind === "color" ? "color" : f.kind === "number" ? "number" : "text"} min={f.min} max={f.max} step={f.step} maxLength={f.max_length} value={values[key]} onChange={e => update(key, f.kind === "number" ? Number(e.target.value) : e.target.value)} />)}
      </div>;
    })}</div>
  </details>)}</div>;
}

export default function CampaignCreative({ campaignId, view = "creative" }) {
  const navigate = useNavigate();
  const base = `/batch/campaigns/${campaignId}`;
  const [data, setData] = useState(null), [assets, setAssets] = useState([]), [report, setReport] = useState(null);
  const [selected, setSelected] = useState(new Set()), [query, setQuery] = useState(""), [page, setPage] = useState(1);
  const [groups, setGroups] = useState([]), [mode, setMode] = useState("percent"), [contract, setContract] = useState(false);
  const [agreement, setAgreement] = useState(""), [rounding, setRounding] = useState(""), [reason, setReason] = useState("");
  const [replace, setReplace] = useState(false), [pin, setPin] = useState(false), [preview, setPreview] = useState(null);
  const [seed, setSeed] = useState("campaign"), [busy, setBusy] = useState(false), [error, setError] = useState(""), [message, setMessage] = useState("");
  const [delivery, setDelivery] = useState(null), [destination, setDestination] = useState("");
  const [generation, setGeneration] = useState(null);
  const [previewStyle, setPreviewStyle] = useState(null);
  const load = useCallback(async (initialize = false) => {
    const [head, backgrounds, receipt] = await Promise.all([request(`${base}/creative`), request("/backgrounds"), request(`${base}/creative/report`)]);
    setData(head); setAssets(backgrounds); setReport(receipt);
    if (initialize) {
      setGroups(head.plan.groups?.length ? head.plan.groups : [{ id: "estilo-1", name: "Estilo 1", weight: 100, requirement: "creative", model: "", settings: {} }]);
      setMode(head.plan.mode || "percent");
      setAgreement(head.plan.contract?.agreement || ""); setRounding(head.plan.contract?.rounding_note || "");
    }
  }, [base]);
  useEffect(() => { let alive = true; load(true).catch(e => { if (alive) setError(e.message); }); return () => { alive = false; }; }, [load]);
  useEffect(() => { if (view === "history") { const timer = setInterval(() => load().catch(e => setError(e.message)), 15000); return () => clearInterval(timer); } }, [load, view]);
  const run = async fn => { setBusy(true); setError(""); setMessage(""); try { await fn(); } catch (e) { setError(e.message); } finally { setBusy(false); } };
  const change = fn => { setPreview(null); fn(); };
  const updateGroup = (i, patch) => change(() => setGroups(old => old.map((g, n) => n === i ? { ...g, ...patch } : g)));
  const chooseRequirement = (i, value) => {
    const patch = value === "photo_effect" ? { movement_style: "foto-parallax", effect: "bokeh", animate_image: false, enable_scenes: false, background_mode: "as_is" }
      : value === "veo" ? { movement_style: "estandar", background_id: null, animate_image: false, effect: "" } : {};
    updateGroup(i, { requirement: value, model: value === "veo" ? data.veo_model : "", settings: { ...groups[i].settings, ...patch } });
  };
  const download = async ext => {
    const response = await fetch(`${API}${base}/creative/export.${ext}`, { headers: { Authorization: `Bearer ${localStorage.getItem("genly_token") || ""}` } });
    if (!response.ok) throw new Error(`No se pudo exportar (${response.status})`);
    const url = URL.createObjectURL(await response.blob()); const a = document.createElement("a"); a.href = url; a.download = `campana-${campaignId}.${ext}`; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  if (!data) return <div role={error ? "alert" : "status"}>{error || "Cargando configuración…"}</div>;
  const filtered = data.items.filter(i => `${i.artist} ${i.title}`.toLowerCase().includes(query.toLowerCase()));
  const visible = filtered.slice((page - 1) * 20, page * 20);
  const eligible = data.items.filter(i => selected.has(i.id) && i.status === "lyrics_approved");
  return <section className="space-y-5" aria-label="Configuración de campaña">
    {error && <p role="alert" className="rounded-xl bg-red-500/15 p-3 text-red-200">{error}</p>}
    {message && <p role="status" className="rounded-xl bg-emerald-500/15 p-3 text-emerald-200">{message}</p>}
    <div className="flex flex-wrap items-center justify-between gap-3"><p className="text-sm text-ink-secondary">Configuración guardada · versión {data.plan.revision}</p><button className={button} disabled={busy} onClick={() => run(() => load())}>Actualizar estado</button></div>
    {view === "creative" && <>
      <div className="rounded-xl bg-surface-2/40 p-4"><h2 className="font-semibold">Seleccioná las canciones</h2><div className="my-3 flex flex-wrap gap-3">
        <input className={input + " max-w-xs"} aria-label="Buscar para asignar" placeholder="Canción o artista" value={query} onChange={e => { setQuery(e.target.value); setPage(1); }} />
        <button className={button} onClick={() => change(() => setSelected(new Set(filtered.filter(i => !i.discarded).map(i => i.id))))}>Seleccionar todas las coincidencias ({filtered.filter(i => !i.discarded).length})</button>
        <button className={button} onClick={() => change(() => setSelected(new Set()))}>Limpiar selección</button><span>{selected.size} seleccionadas</span>
      </div><div className="overflow-x-auto"><table className="w-full text-left text-sm"><thead><tr><th>Elegir</th><th>Canción</th><th>Grupo / excepción</th><th>Estado</th></tr></thead><tbody>{visible.map(i => <tr key={i.id} className="border-t border-white/10"><td className="p-2"><input type="checkbox" aria-label={`Seleccionar ${i.title}`} disabled={busy || i.discarded} checked={selected.has(i.id)} onChange={e => change(() => setSelected(old => { const next = new Set(old); if (e.target.checked) next.add(i.id); else next.delete(i.id); return next; }))} /></td><td>{i.title}<div className="text-xs text-ink-secondary">{i.artist}</div></td><td>{i.assignment?.group_name || "Sin asignar"}{i.assignment?.pinned ? " · Fijada" : ""}</td><td>{statusLabels[i.status] || "En preparación"}</td></tr>)}</tbody></table></div>
      <div className="mt-3 flex gap-3"><button className={button} disabled={page <= 1} onClick={() => setPage(p => p - 1)}>Anterior</button><span>Página {page} de {Math.max(1, Math.ceil(filtered.length / 20))}</span><button className={button} disabled={page * 20 >= filtered.length} onClick={() => setPage(p => p + 1)}>Siguiente</button></div></div>
      {data.can_manage && <fieldset disabled={busy} className="space-y-4">
        <div className="flex flex-wrap gap-4"><label>Repartir por <select aria-label="Modo de reparto" className={input} value={mode} onChange={e => change(() => setMode(e.target.value))}><option value="percent">Porcentaje</option><option value="count">Cantidad</option></select></label>
          <label className="flex items-center gap-2"><input type="checkbox" checked={replace} onChange={e => change(() => setReplace(e.target.checked))} />Reemplazar excepciones fijadas</label>
          <label className="flex items-center gap-2"><input type="checkbox" checked={pin} onChange={e => change(() => setPin(e.target.checked))} />Fijar estas asignaciones</label></div>
        <p className="text-sm text-ink-secondary">Activá sólo los ajustes que querés cambiar. El resto se conserva. Los grupos guardados pueden reutilizarse para otro reparto.</p>
        {groups.map((g, i) => <article key={g.id} className="space-y-3 rounded-2xl bg-surface-2/50 p-5 ring-1 ring-white/10">
          <div className="grid gap-3 md:grid-cols-3"><label>Nombre del estilo<input aria-label={`Nombre del grupo ${i + 1}`} className={input} value={g.name} onChange={e => updateGroup(i, { name: e.target.value })} /></label>
            <label>{mode === "percent" ? "Porcentaje" : "Canciones"}<input aria-label={`Cantidad del grupo ${i + 1}`} className={input} type="number" min="0" value={g.weight} onChange={e => updateGroup(i, { weight: Number(e.target.value) })} /></label>
            <label>Requisito<select aria-label={`Requisito del grupo ${i + 1}`} className={input} value={g.requirement} onChange={e => chooseRequirement(i, e.target.value)}><option value="creative">Ajustes creativos</option><option value="photo_effect">Foto fija + efecto</option><option value="veo">Fondo generado con Veo</option></select></label></div>
          {g.requirement === "veo" && <p className="text-xs text-ink-secondary">Modelo comprometido: {g.model}. Se contrasta con la evidencia del fondo generado.</p>}
          <Controls fields={data.fields} values={g.settings} assets={assets} label={g.name} update={(k, v) => { const settings = { ...g.settings }; if (v === undefined) delete settings[k]; else settings[k] = v; updateGroup(i, { settings }); }} />
          <button className={button} onClick={() => setPreviewStyle(old => old === g.id ? null : g.id)}>Ver muestra del estilo</button>
          {previewStyle === g.id && <StylePreview settings={g.settings} assets={assets} />}
          {groups.length > 1 && <button className={button} onClick={() => change(() => setGroups(old => old.filter((_, n) => n !== i)))}>Quitar grupo</button>}
        </article>)}
        <div className="flex flex-wrap gap-3"><button className={button} onClick={() => change(() => setGroups(old => [...old, { id: crypto.randomUUID(), name: `Estilo ${old.length + 1}`, weight: 0, requirement: "creative", model: "", settings: {} }]))}>Agregar estilo</button>
          <button className={button} onClick={() => change(() => setSeed(crypto.randomUUID()))}>Redistribuir canciones</button>
          <label className={button + " cursor-pointer"}>Subir fondo propio<input className="sr-only" type="file" accept="image/jpeg,image/png,video/mp4,video/quicktime" aria-label="Subir fondo propio" onChange={e => { const f = e.target.files?.[0]; if (!f) return; run(async () => { const form = new FormData(); form.set("file", f); form.set("name", f.name); await request(`${base}/creative/assets`, { method: "POST", body: form }); await load(); setMessage("Fondo guardado en la biblioteca de esta cuenta; ya podés seleccionarlo."); }); e.target.value = ""; }} /></label></div>
        <label className="flex gap-2"><input type="checkbox" checked={contract} onChange={e => change(() => setContract(e.target.checked))} />Registrar este reparto como acuerdo contractual</label>
        {contract && <div className="grid gap-3 md:grid-cols-2"><label>Acuerdo y referencia del documento<textarea aria-label="Acuerdo contractual" className={input} value={agreement} onChange={e => change(() => setAgreement(e.target.value))} /></label><label>Aceptación del redondeo, si corresponde<textarea aria-label="Aceptación del redondeo" className={input} placeholder="Quién acordó 20/19 y a qué grupo se asigna el video extra" value={rounding} onChange={e => change(() => setRounding(e.target.value))} /></label></div>}
        <label className="block">Motivo del cambio<input aria-label="Motivo del cambio" className={input} value={reason} onChange={e => change(() => setReason(e.target.value))} /></label>
        <button className={button} disabled={!selected.size || reason.trim().length < 3} onClick={() => run(async () => setPreview(await post(`${base}/creative/preview`, { revision: data.plan.revision, item_ids: [...selected], mode, groups, seed, replace_exceptions: replace, pin, contract, agreement, rounding_note: rounding, reason }))) }>Ver reparto antes de guardar</button>
        {preview && <div className="space-y-3 rounded-xl bg-brand/10 p-4"><h3 className="font-semibold">Vista previa · {preview.changes.length} canciones · {preview.skipped.length} excepciones conservadas</h3>
          <p>{groups.map((g, i) => `${g.name}: ${preview.counts[i]} (${(100 * preview.counts[i] / preview.changes.length).toFixed(2)}%)`).join(" · ")}</p>
          {preview.rounded && <p className="text-amber-200">Hubo redondeo: las cantidades anteriores son el reparto efectivo.</p>}
          <div className="max-h-80 space-y-2 overflow-auto">{preview.changes.map(c => <details key={c.item_id}><summary>{c.artist} — {c.title} → {c.group}</summary><dl className="ml-4 text-xs">{Object.entries(c.after).filter(([k, v]) => c.before[k] !== v).map(([k, v]) => <div key={k}>{data.fields[k]?.label || k}: {String(c.before[k] ?? "Heredado")} → {String(v)}</div>)}</dl></details>)}</div>
          <button className={button} onClick={() => run(async () => { await post(`${base}/creative/apply`, { preview_id: preview.preview_id }); setPreview(null); await load(); setMessage("Asignación guardada. No se generaron videos."); })}>Guardar esta asignación</button>
        </div>}
        {data.operations[0]?.revision === data.plan.revision && <button className={button} disabled={reason.trim().length < 3} onClick={() => run(async () => { await post(`${base}/creative/undo`, { revision: data.plan.revision, operation_id: data.operations[0].id, reason }); setPreview(null); await load(); setMessage("Última asignación deshecha."); })}>Deshacer última asignación</button>}
      </fieldset>}
      <div className="rounded-xl bg-surface-2/40 p-4"><h3 className="font-semibold">Generación después de revisar letras</h3><p className="my-2 text-sm text-ink-secondary">Sólo se generan canciones con letra y tiempos aprobados. Se usan sus ajustes guardados; los videos aparecerán en el historial de esta campaña.</p><button className={button} disabled={busy || !eligible.length} onClick={() => setGeneration(eligible)}>Preparar generación de {eligible.length} aprobadas seleccionadas</button></div>
    </>}
    {generation && <div role="dialog" aria-modal="true" aria-label="Confirmar generación" className="fixed inset-0 z-50 grid place-items-center bg-black/80 p-5"><div className="max-w-lg space-y-4 rounded-2xl bg-surface-2 p-6"><h2 className="text-xl font-semibold">Generar {generation.length} videos</h2><p>Esta acción genera fondos y videos y consume el cupo correspondiente. La configuración y el grupo de cada canción quedarán registrados.</p><ul className="max-h-40 overflow-auto">{generation.map(i => <li key={i.id}>{i.title} · {i.assignment?.group_name || "Configuración actual"}</li>)}</ul><button className={button} disabled={busy} onClick={() => run(async () => { let completed = 0; try { for (const item of generation) { const job = await request(`/status/${item.job_id}`); await request("/generate", { method: "POST", body: campaignGenerateForm(item, job) }); completed++; } } finally { setGeneration(null); await load(); setMessage(`${completed} trabajos enviados; consultá el historial de esta campaña.`); } })}>Confirmar generación</button><button className={button} disabled={busy} onClick={() => setGeneration(null)}>Cancelar</button></div></div>}
    {view === "contract" && report && <div id="campaign-contract-report" className="space-y-4">
      <style>{"@media print { body * { visibility: hidden; } #campaign-contract-report, #campaign-contract-report * { visibility: visible; color: black !important; } #campaign-contract-report { position: absolute; inset: 0; background: white; } .campaign-no-print { display: none !important; } }"}</style>
      <h2 className="text-xl font-semibold">Contrato y cumplimiento · {report.name}</h2><p className="text-sm">Informe: {new Date(report.at).toLocaleString()} · Acuerdo versión {report.contract.revision || "Sin registrar"}</p>
      <p className="whitespace-pre-wrap">{report.contract.agreement || "Todavía no se registró un compromiso contractual."}</p><p>{report.contract.rounding_note}</p>
      <p>Universo: {report.contract.item_ids?.length || 0} entregas principales. Los reintentos y variantes no aumentan la cuota.</p>
      <div className="overflow-x-auto"><table className="w-full text-left text-sm"><thead><tr>{["Grupo", "Compromiso", "Objetivo", "Asignadas", "Generadas", "Aprobadas", "Verificadas", "Entregadas"].map(h => <th className="p-2" key={h}>{h}</th>)}</tr></thead><tbody>{report.groups.map(g => <tr key={g.id}>{[g.name, `${g.target_weight}${report.contract.mode === "percent" ? "%" : " videos"}`, g.target, ...[g.assigned, g.generated, g.approved, g.verified, g.delivered || 0].map(n => `${n} (${(100 * n / Math.max(1, report.contract.item_ids?.length || 0)).toFixed(2)}%)`)].map((v, i) => <td className="p-2" key={i}>{v}</td>)}</tr>)}</tbody></table></div>
      <p className="text-sm text-ink-secondary">La asignación no prueba cumplimiento: «Verificadas» requiere evidencia del archivo generado. La entrega se registra después de la aprobación final.</p>
      <div className="campaign-no-print flex gap-3"><button className={button} onClick={() => run(() => download("csv"))}>Exportar CSV</button><button className={button} onClick={() => run(() => download("xlsx"))}>Exportar Excel</button><button className={button} onClick={() => { const nodes = [...document.querySelectorAll("#campaign-contract-report details")]; const closed = nodes.filter(n => !n.open); closed.forEach(n => { n.open = true; }); window.addEventListener("afterprint", () => closed.forEach(n => { n.open = false; }), { once: true }); window.print(); }}>Imprimir / guardar PDF</button></div>
      <h3 className="font-semibold">Registro por video</h3>{report.videos.map(v => <details key={v.job_id}><summary>{v.artist} — {v.title} · {v.assignment.group_name || "Sin clasificación"} · {v.compliance === "verified" ? "Verificado" : v.compliance === "deviation" ? "Desviación" : "Evidencia pendiente"}</summary><p className="text-xs break-all">Efecto aplicado: {v.evidence.effect_applied || "No acreditado"} · Modelos: {v.evidence.models?.join(", ") || "No acreditados"} · Huella: {v.evidence.video_sha256 || "Pendiente"}</p></details>)}
      <h3 className="font-semibold">Historial de cambios</h3>{report.history.map((h, i) => <details key={i}><summary>{new Date(h.at).toLocaleString()} · Usuario {h.actor} · {h.detail.reason || h.detail.destination || "Cambio registrado"}</summary><p>Versión {h.detail.revision || h.detail.after?.revision || "—"}</p>{(h.detail.changes || (h.detail.item_id ? [h.detail] : [])).map(c => <div key={c.item_id} className="ml-4 text-sm"><p>{c.artist} · {c.title || data.items.find(item => item.id === c.item_id)?.title || c.item_id}</p>{Object.entries(c.after || {}).filter(([k, value]) => data.fields[k] && JSON.stringify(c.before?.[k]) !== JSON.stringify(value)).map(([k, value]) => <p key={k}>{data.fields[k].label}: {String(c.before?.[k] ?? "Heredado")} → {String(value)}</p>)}</div>)}</details>)}
    </div>}
    {view === "history" && report && <div className="space-y-4"><h2 className="text-xl font-semibold">Videos de esta campaña ({report.videos.length})</h2><p className="text-sm text-ink-secondary">Incluye generaciones, reintentos y variantes vinculadas a esta campaña.</p>
      {!report.videos.length && <p className="rounded-xl bg-surface-2/40 p-8">Todavía no hay videos generados. Primero aprobá letras y tiempos, y luego iniciá la generación desde Estilo y fondos.</p>}
      <div className="grid gap-4 md:grid-cols-2">{report.videos.map(v => <article key={v.job_id} className="space-y-3 rounded-xl bg-surface-2/40 p-5 ring-1 ring-white/10"><VideoThumbnail video={v} /><div><h3 className="font-semibold">{v.title}</h3><p className="text-sm text-ink-secondary">{v.artist} · {new Date(v.created_at).toLocaleString()}</p></div><p>{statusLabels[v.status] || "En preparación"}{v.parent_job_id ? " · Variante" : ""}</p><p className="text-sm">{v.assignment.group_name || "Sin clasificación contractual"}</p><button className={button} onClick={() => navigate(v.open_path)}>Abrir video y versiones</button>
        {data.can_manage && v.status === "done" && v.approved_at && v.evidence.video_sha256 && <button className={button} onClick={() => { setDelivery(v); setDestination(""); }}>Registrar entrega</button>}
      </article>)}</div></div>}
    {delivery && <div role="dialog" aria-modal="true" aria-label="Registrar entrega" className="fixed inset-0 z-50 grid place-items-center bg-black/80 p-5"><div className="max-w-lg space-y-4 rounded-xl bg-surface-2 p-6"><h2>Registrar entrega de {delivery.title}</h2><p>Confirmá dónde entregaste esta versión aprobada. Este registro no envía el archivo.</p><input aria-label="Destino de entrega" className={input} value={destination} onChange={e => setDestination(e.target.value)} placeholder="Portal, carpeta o destinatario y referencia" /><button className={button} disabled={busy || destination.trim().length < 3} onClick={() => run(async () => { await post(`${base}/creative/deliveries`, { job_id: delivery.job_id, video_sha256: delivery.evidence.video_sha256, destination }); setDelivery(null); await load(); setMessage("Entrega registrada."); })}>Confirmar entrega realizada</button><button className={button} disabled={busy} onClick={() => setDelivery(null)}>Cancelar</button></div></div>}
  </section>;
}
