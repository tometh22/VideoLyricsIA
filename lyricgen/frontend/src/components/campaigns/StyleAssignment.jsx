import { useEffect, useRef, useState } from "react";
import { useI18n } from "../../i18n";
import { EFFECT_LABELS } from "../../lib/optionLabels";
import { photoEffectGroups } from "../../lib/campaignCreative";
import { campaignPost, campaignRequest } from "../../lib/campaignApi";
import StyleControls, { StylePreview, SOURCE_LABELS } from "./StyleControls";
import { Banner, Button, Field, inputClass } from "./ui";

export const VEO_LITE = "veo-3.1-lite-generate-001";

export function initialGroups(plan) {
  return plan?.groups?.length
    ? plan.groups.map((group) => (group.requirement === "veo" ? { ...group, model: VEO_LITE } : group))
    : [{ id: "estilo-1", name: "Estilo 1", weight: 100, requirement: "creative", model: "", settings: {} }];
}

const photoSource = settings => settings.scene_source || (settings.background_hint ? settings.bg_verbatim ? "prompt_literal" : "prompt_improved" : settings.match_lyrics === false ? "auto" : "lyrics");

const trim = (value) => (Number.isInteger(value) ? value : Number(value.toFixed(2)));

/**
 * Split a set of songs across one or more styles. Nothing is saved until the
 * operator reviews the frozen preview; nothing is ever generated here.
 */
export default function StyleAssignment({ campaignId, creative, itemIds, onSaved, onBusyChange, onPrepareGeneration }) {
  const { t } = useI18n();
  const base = `/batch/campaigns/${encodeURIComponent(campaignId)}`;
  const plan = creative.plan || { revision: 0 };
  const [groups, setGroups] = useState(() => initialGroups(plan));
  const [mode, setMode] = useState(plan.mode || "percent");
  const [replace, setReplace] = useState(false);
  const [pin, setPin] = useState(false);
  const [contract, setContract] = useState(false);
  const [agreement, setAgreement] = useState(plan.contract?.agreement || "");
  const [rounding, setRounding] = useState(plan.contract?.rounding_note || "");
  const [reason, setReason] = useState("");
  const [seed, setSeed] = useState("campaign");
  const [preview, setPreview] = useState(null);
  const [previewStyle, setPreviewStyle] = useState(null);
  const [assets, setAssets] = useState([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const running = useRef(false);
  const previewRef = useRef(null);
  useEffect(() => { if (preview) previewRef.current?.scrollIntoView?.({ behavior: "smooth", block: "start" }); }, [preview]);
  useEffect(() => { onBusyChange?.(busy); }, [busy, onBusyChange]);
  useEffect(() => {
    const controller = new AbortController();
    campaignRequest("/backgrounds", { signal: controller.signal }).then(setAssets).catch(() => {});
    return () => controller.abort();
  }, []);

  const run = async (fn) => {
    if (running.current) return;
    running.current = true; setBusy(true); setError(""); setMessage("");
    try { await fn(); } catch (runError) { setError(runError.message); } finally { running.current = false; setBusy(false); }
  };
  const change = (fn) => { setPreview(null); fn(); };
  const updateGroup = (index, patch) => change(() => setGroups((old) => old.map((group, n) => (n === index ? { ...group, ...patch } : group))));
  const chooseRequirement = (index, value) => {
    const patch = value === "photo_effect" ? photoEffectGroups(["bokeh"])[0].settings
      : value === "veo" ? { movement_style: "estandar", background_id: null, animate_image: false, effect: "" } : {};
    updateGroup(index, { requirement: value, model: value === "veo" ? VEO_LITE : "", settings: { ...groups[index].settings, ...patch } });
  };
  const count = itemIds.length;
  const weightSum = groups.reduce((total, group) => total + (Number(group.weight) || 0), 0);
  const target = mode === "percent" ? 100 : count;
  const balanced = groups.length > 0 && Math.abs(weightSum - target) < 1e-6;
  const lastOperation = creative.operations?.[0];
  const readyCount = (creative.items || []).filter(item => itemIds.includes(item.id) && item.status === "lyrics_approved" && !item.discarded).length;
  const issues = [
    !count && "Seleccioná las canciones a las que querés aplicar el diseño",
    !balanced && (mode === "percent" ? "Los porcentajes deben sumar 100%" : "Las cantidades deben sumar las canciones seleccionadas"),
    reason.trim().length > 0 && reason.trim().length < 3 && "Escribí al menos 3 caracteres en la nota o dejala vacía",
    ...groups.filter(group => ["prompt_literal", "prompt_improved"].includes(group.settings.scene_source) && !group.settings.background_hint?.trim()).map(group => `Escribí la descripción del fondo en «${group.name}»`),
  ].filter(Boolean);
  const previewAssignment = () => run(async () => {
    setPreview(await campaignPost(`${base}/creative/preview`, {
      revision: plan.revision, item_ids: itemIds, mode, groups, seed, replace_exceptions: replace, pin, contract, agreement, rounding_note: rounding,
      reason: reason.trim() || "Configuración de estilos y reparto desde la campaña",
    }));
  });
  const choosePhotoPreset = effects => change(() => { setGroups(photoEffectGroups(effects)); setMode("percent"); });
  const save = prepareGeneration => run(async () => {
    await campaignPost(`${base}/creative/apply`, { preview_id: preview.preview_id });
    setPreview(null); setMessage("Asignación guardada. No se generaron videos.");
    await onSaved?.();
    if (prepareGeneration) await onPrepareGeneration?.({ startImmediately: preview.skipped.length === 0 });
  });

  return <fieldset disabled={busy} className="space-y-5">
    <div className="space-y-4">
      <h3 className="font-semibold">¿Qué fondo querés?</h3>
      <div role="group" aria-label="Tipo de fondo" className="grid grid-cols-2 gap-3 [&_button[aria-pressed=true]]:bg-brand/20 [&_button[aria-pressed=true]]:ring-brand [&_button[aria-pressed=true]]:text-white">
        <Button aria-pressed={groups.every(group => group.requirement === "photo_effect")} className="!h-auto min-w-0 flex-col items-start gap-1 !whitespace-normal p-4 text-left" onClick={() => { if (!groups.every(group => group.requirement === "photo_effect")) choosePhotoPreset(["bokeh"]); }}>
          <span className="text-sm font-semibold">Foto fija</span><span className="text-xs font-normal text-ink-secondary">Imagen con un efecto suave</span>
        </Button>
        <Button aria-pressed={groups.every(group => group.requirement === "veo")} className="!h-auto min-w-0 flex-col items-start gap-1 !whitespace-normal p-4 text-left" onClick={() => change(() => { setMode("percent"); setGroups([{ id: "video-lite", name: "Video · Veo Lite", weight: 100, requirement: "veo", model: VEO_LITE, settings: { background_id: null, background_mode: "as_is", scene_source: "lyrics", movement_style: "estandar", effect: "", animate_image: false, enable_scenes: false } }]); })}>
          <span className="text-sm font-semibold">Video</span><span className="text-xs font-normal text-ink-secondary">Fondo en movimiento · Veo Lite</span>
        </Button>
      </div>
      {groups.every(group => group.requirement === "photo_effect") && <div className="space-y-2">
        <p className="text-sm font-medium">Elegí el efecto</p>
        <div role="group" aria-label="Efecto de la foto" className="flex flex-wrap gap-2 [&_button[aria-pressed=true]]:bg-brand [&_button[aria-pressed=true]]:text-white">
          <Button aria-pressed={groups.length === 1 && groups[0].settings.effect === "bokeh"} onClick={() => choosePhotoPreset(["bokeh"])}>Bokeh</Button>
          <Button aria-pressed={groups.length === 1 && groups[0].settings.effect === "embers"} onClick={() => choosePhotoPreset(["embers"])}>Chispas</Button>
          <Button aria-pressed={mode === "percent" && groups.length === 2 && groups.every(group => group.weight === 50) && groups.some(group => group.settings.effect === "bokeh") && groups.some(group => group.settings.effect === "embers")} onClick={() => choosePhotoPreset(["bokeh", "embers"])}>Mitad y mitad</Button>
        </div>
        <p className="text-xs text-ink-secondary">Mitad y mitad reparte las canciones entre Bokeh y Chispas.</p>
      </div>}
      <p className="text-sm text-ink-secondary">{groups.every(group => group.settings.scene_source === "lyrics" && !group.settings.background_id) ? "El fondo se inspira en la letra." : "Se conserva el diseño guardado hasta que elijas otro."}</p>
    </div>
    <details className="rounded-xl bg-black/15 ring-1 ring-white/[0.06]">
      <summary className="cursor-pointer px-4 py-3 text-sm text-ink-secondary">Opciones avanzadas</summary>
      <div className="space-y-5 border-t border-white/[0.06] p-4">
    <div className="flex flex-wrap items-end gap-4">
      <Field label="Repartir por" className="w-44">
        <select aria-label="Modo de reparto" className={inputClass} value={mode} onChange={(event) => change(() => setMode(event.target.value))}>
          <option value="percent">Porcentaje</option><option value="count">Cantidad</option>
        </select>
      </Field>
      <label className="flex items-center gap-2 pb-2.5 text-sm"><input type="checkbox" className="accent-[#7557FF]" checked={replace} onChange={(event) => change(() => setReplace(event.target.checked))} />Reemplazar excepciones fijadas</label>
      <label className="flex items-center gap-2 pb-2.5 text-sm"><input type="checkbox" className="accent-[#7557FF]" checked={pin} onChange={(event) => change(() => setPin(event.target.checked))} />Fijar estas asignaciones</label>
    </div>
    <p role="status" className={`text-sm ${balanced ? "text-emerald-200" : "text-amber-200"}`}>{mode === "percent"
      ? `Suma de porcentajes: ${trim(weightSum)}% de 100%${balanced ? " · listo para repartir" : weightSum < 100 ? ` · faltan ${trim(100 - weightSum)}%` : ` · sobran ${trim(weightSum - 100)}%`}`
      : `Suma de canciones: ${trim(weightSum)} de ${count} seleccionadas${balanced ? " · listo para repartir" : weightSum < count ? ` · faltan ${trim(count - weightSum)}` : ` · sobran ${trim(weightSum - count)}`}`}</p>

    {groups.map((group, index) => <article key={group.id} className="space-y-4 rounded-card bg-surface-2/60 p-5 ring-1 ring-white/10">
      <div className="grid gap-3 md:grid-cols-[1fr_180px_220px]">
        <Field label="Nombre del estilo"><input aria-label={`Nombre del grupo ${index + 1}`} className={inputClass} value={group.name} onChange={(event) => updateGroup(index, { name: event.target.value })} /></Field>
        <Field label={mode === "percent" ? "Porcentaje de canciones" : "Cantidad de canciones"}>
          <input aria-label={`Cantidad del grupo ${index + 1}`} className={inputClass} type="number" min="0" value={group.weight} onChange={(event) => updateGroup(index, { weight: Number(event.target.value) })} />
        </Field>
        <Field label="Tipo de fondo">
          <select aria-label={`Requisito del grupo ${index + 1}`} className={inputClass} value={group.requirement} onChange={(event) => chooseRequirement(index, event.target.value)}>
            <option value="creative">Ajustes creativos</option><option value="photo_effect">Foto fija + efecto</option><option value="veo">Fondo generado con Veo</option>
          </select>
        </Field>
      </div>
      {group.requirement === "veo" && <p className="rounded-lg bg-black/20 px-3 py-2 text-xs text-ink-secondary"><strong className="text-white">Modelo de fondo: Veo Lite.</strong> Todos los fondos nuevos usan Veo Lite; nunca se cambia solo a un modelo más caro.</p>}
      {group.requirement === "photo_effect" && <div className="space-y-3 rounded-xl bg-black/20 p-4">
        <p className="text-sm font-medium">Foto fija · efecto sobre la imagen</p>
        <div className="grid gap-3 md:grid-cols-2">
          <Field label="Efecto"><select aria-label={`Efecto de foto del grupo ${index + 1}`} className={inputClass} value={group.settings.effect || "bokeh"} onChange={event => updateGroup(index, { settings: { ...group.settings, effect: event.target.value } })}>
            {[...new Set(["bokeh", "embers", ...(creative.fields?.effect?.options || [])])].filter(effect => effect && !["none", "foto_viva"].includes(effect)).map(effect => <option key={effect} value={effect}>{EFFECT_LABELS(t)[effect] || (effect === "embers" ? "Chispas" : effect === "bokeh" ? "Bokeh" : effect)}</option>)}
          </select></Field>
          <Field label="Inspiración del fondo"><select aria-label={`Inspiración de foto del grupo ${index + 1}`} className={inputClass} value={photoSource(group.settings)} onChange={event => updateGroup(index, { settings: { ...group.settings, scene_source: event.target.value } })}>
            {Object.entries(SOURCE_LABELS).filter(([key]) => ["lyrics", "auto", "prompt_literal", "prompt_improved"].includes(key)).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
          </select></Field>
        </div>
        {["prompt_literal", "prompt_improved"].includes(photoSource(group.settings)) && <Field label="Descripción de la foto"><textarea aria-label={`Descripción de foto del grupo ${index + 1}`} className={inputClass} maxLength={4000} value={group.settings.background_hint || ""} onChange={event => updateGroup(index, { settings: { ...group.settings, background_hint: event.target.value } })} /></Field>}
      </div>}
      <StyleControls fields={Object.fromEntries(Object.entries(creative.fields || {}).filter(([key]) => group.requirement !== "photo_effect" || !["effect", "scene_source", "background_hint", "movement_style", "animate_image", "enable_scenes", "background_mode"].includes(key)))} values={group.settings} assets={assets} label={group.name}
        update={(key, value) => { const settings = { ...group.settings }; if (value === undefined) delete settings[key]; else settings[key] = value; updateGroup(index, { settings }); }} />
      <div className="flex flex-wrap gap-2">
        <Button size="sm" variant="ghost" onClick={() => setPreviewStyle((old) => (old === group.id ? null : group.id))}>{previewStyle === group.id ? "Ocultar muestra" : "Ver muestra del estilo"}</Button>
        {groups.length > 1 && <Button size="sm" variant="ghost" onClick={() => change(() => setGroups((old) => old.filter((_, n) => n !== index)))}>Quitar grupo</Button>}
      </div>
      {previewStyle === group.id && <StylePreview settings={group.settings} assets={assets} />}
    </article>)}

    <div className="flex flex-wrap gap-2">
      <Button size="sm" onClick={() => change(() => setGroups((old) => [...old, { id: crypto.randomUUID(), name: `Estilo ${old.length + 1}`, weight: 0, requirement: "creative", model: "", settings: {} }]))}>Agregar estilo</Button>
      <Button size="sm" variant="ghost" onClick={() => change(() => setSeed(crypto.randomUUID()))}>Redistribuir canciones</Button>
      <label className="inline-flex h-8 cursor-pointer items-center rounded-button px-3 text-xs font-semibold text-ink-secondary hover:bg-white/[0.06] hover:text-white">Subir fondo propio
        <input className="sr-only" type="file" accept="image/jpeg,image/png,video/mp4,video/quicktime" aria-label="Subir fondo propio" onChange={(event) => {
          const file = event.target.files?.[0]; event.target.value = "";
          if (!file) return;
          void run(async () => {
            const form = new FormData(); form.set("file", file); form.set("name", file.name);
            await campaignRequest(`${base}/creative/assets`, { method: "POST", body: form });
            setAssets(await campaignRequest("/backgrounds"));
            setMessage("Fondo guardado en la biblioteca de la cuenta; ya podés elegirlo.");
          });
        }} /></label>
    </div>

    <div className="space-y-3 rounded-card bg-black/20 p-4 ring-1 ring-white/[0.06]">
      <label className="flex items-center gap-2 text-sm"><input type="checkbox" className="accent-[#7557FF]" checked={contract} onChange={(event) => change(() => setContract(event.target.checked))} />Registrar este reparto como acuerdo contractual</label>
      {contract && <div className="grid gap-3 md:grid-cols-2">
        <Field label="Acuerdo y referencia del documento"><textarea aria-label="Acuerdo contractual" rows={3} className={inputClass} value={agreement} onChange={(event) => change(() => setAgreement(event.target.value))} /></Field>
        <Field label="Aceptación del redondeo, si corresponde"><textarea aria-label="Aceptación del redondeo" rows={3} className={inputClass} placeholder="Quién acordó 20/19 y a qué grupo va el video extra" value={rounding} onChange={(event) => change(() => setRounding(event.target.value))} /></Field>
      </div>}
      <Field label="Nota del cambio (opcional)"><input aria-label="Motivo del cambio" aria-describedby="campaign-style-note-help" maxLength={1000} placeholder="Por ejemplo: reparto acordado con Chile" className={inputClass} value={reason} onChange={(event) => change(() => setReason(event.target.value))} /></Field>
    </div>

    <p id="campaign-style-note-help" className="text-xs text-ink-secondary">Si dejás la nota vacía, guardamos un motivo automático en el historial.</p>
      <div className="flex flex-wrap gap-2">
      {lastOperation?.revision === plan.revision && <Button variant="ghost" disabled={reason.trim().length > 0 && reason.trim().length < 3} onClick={() => run(async () => {
        await campaignPost(`${base}/creative/undo`, { revision: plan.revision, operation_id: lastOperation.id, reason: reason.trim() || "Deshacer asignación desde la campaña" });
        setPreview(null); setMessage("Última asignación deshecha."); await onSaved?.();
      })}>Deshacer última asignación</Button>}
      </div>
      </div>
    </details>
    {issues.length > 0 && <ul id="campaign-style-issues" className="list-inside list-disc space-y-1 text-sm text-amber-200">{issues.map(issue => <li key={issue}>{issue}</li>)}</ul>}
    {error && <Banner tone="danger">{error}</Banner>}
    {message && <Banner tone="success">{message}</Banner>}

    {!preview && <Button variant="primary" disabled={issues.length > 0} aria-describedby={issues.length ? "campaign-style-issues" : undefined} onClick={previewAssignment}>Continuar</Button>}

    {preview && <div ref={previewRef} className="scroll-mt-4 space-y-3 rounded-card bg-brand/10 p-5 ring-1 ring-brand/30">
      <h3 className="font-semibold">Así se generarán tus videos</h3>
      <p className="text-sm text-ink-secondary">{preview.changes.length} canciones{preview.skipped.length ? ` · ${preview.skipped.length} conservan su diseño fijado` : ""}. Generar consume el cupo correspondiente.</p>
      <div className="flex flex-wrap gap-2">{groups.map((group, index) => <span key={group.id} className="rounded-lg bg-black/25 px-2.5 py-1 text-sm">{preview.counts[index]} · {group.requirement === "photo_effect" ? EFFECT_LABELS(t)[group.settings.effect] || (group.settings.effect === "embers" ? "Chispas" : group.settings.effect === "bokeh" ? "Bokeh" : group.name) : group.name}</span>)}</div>
      {preview.rounded && <p className="text-sm text-amber-200">El reparto se ajustó a canciones enteras. Estas son las cantidades finales.</p>}
      <details><summary className="cursor-pointer text-sm text-ink-secondary">Ver canciones y ajustes</summary><div className="mt-3 max-h-72 space-y-1 overflow-auto text-sm">{preview.changes.map((item) => <details key={item.item_id} className="rounded-lg px-2 py-1 hover:bg-black/20">
        <summary className="cursor-pointer">{item.artist} — {item.title} → <strong>{item.group}</strong></summary>
        <dl className="ml-4 mt-1 text-xs text-ink-secondary">{Object.entries(item.after).filter(([key, value]) => item.before[key] !== value).map(([key, value]) => <div key={key}>{creative.fields?.[key]?.label || key}: {String(item.before[key] ?? "Heredado")} → {String(value)}</div>)}</dl>
      </details>)}</div></details>
      <div className="flex flex-wrap gap-2">
        {onPrepareGeneration
          ? <Button variant="primary" disabled={!readyCount} onClick={() => save(true)}>{preview.skipped.length ? "Revisar" : "Generar"} {readyCount} {readyCount === 1 ? "video" : "videos"}</Button>
          : <Button variant="primary" onClick={() => save(false)}>Guardar diseño</Button>}
      </div>
      <p className="text-xs text-ink-secondary">{onPrepareGeneration ? preview.skipped.length ? "Hay diseños fijados: vas a revisar el reparto final antes de iniciar." : "Al confirmar, guardamos el diseño e iniciamos la generación." : "Guardar el diseño no genera videos."}</p>
    </div>}
  </fieldset>;
}
