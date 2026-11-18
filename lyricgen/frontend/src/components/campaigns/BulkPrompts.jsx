import { useEffect, useRef, useState } from "react";
import { campaignPost } from "../../lib/campaignApi";
import { Banner, Button, Field, inputClass } from "./ui";

const EDITABLE = new Set(["waiting", "transcribed_pending", "transcribed", "lyrics_approved", "transcribing", "transcribing_queued", "separation_ready", "separating", "separation_queued"]);
export const composePrompt = (common, scene) => [common.trim(), (scene || "").trim()].filter(Boolean).join("\n\n");

export default function BulkPrompts({ campaignId, creative, itemIds, onSaved, onBusyChange }) {
  const [common, setCommon] = useState("");
  const [scenes, setScenes] = useState({});
  const [sceneList, setSceneList] = useState("");
  const [source, setSource] = useState("prompt_improved");
  const [replace, setReplace] = useState(false);
  const [preview, setPreview] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState("");
  const running = useRef(false);
  const selected = new Set(itemIds);
  const eligible = (creative.items || []).filter(item => selected.has(item.id) && !item.discarded && EDITABLE.has(item.status))
    .sort((a, b) => a.ordinal - b.ordinal || a.id.localeCompare(b.id));
  const pinned = eligible.filter(item => item.assignment?.pinned);
  const active = eligible.filter(item => replace || !item.assignment?.pinned);
  const lines = sceneList.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
  const selectionKey = [...itemIds].sort().join(",");
  useEffect(() => { setPreview(null); }, [selectionKey, creative.plan.revision]);
  useEffect(() => { onBusyChange?.(busy); }, [busy, onBusyChange]);
  const change = (fn) => { setPreview(null); setError(""); setSaved(""); fn(); };
  const run = async (fn) => {
    if (running.current) return;
    running.current = true; setBusy(true); setError("");
    try { await fn(); } catch (e) { setError(e.message); } finally { running.current = false; setBusy(false); }
  };
  const prompts = active.map(item => composePrompt(common, scenes[item.id]));
  const invalid = !active.length || eligible.length > 512 || prompts.some(prompt => !prompt || prompt.length > 4000);
  const repeated = prompts.filter(Boolean).length - new Set(prompts.filter(Boolean)).size;
  const prepare = () => run(async () => {
    setPreview(await campaignPost(`/batch/campaigns/${encodeURIComponent(campaignId)}/creative/prompts-preview`, {
      revision: creative.plan.revision, common, source, replace_exceptions: replace,
      songs: eligible.map(item => ({ item_id: item.id, scene: scenes[item.id] || "" })),
    }));
  });
  const save = () => run(async () => {
    const frozen = preview;
    await campaignPost(`/batch/campaigns/${encodeURIComponent(campaignId)}/creative/apply`, { preview_id: frozen.preview_id });
    setPreview(null);
    setSaved(`Prompts guardados para ${frozen.changes.length} canciones. Ya podés volver a la lista y generar el lote.`);
    await onSaved?.();
  });
  return <fieldset disabled={busy} className="space-y-5" aria-label="Prompts de la selección">
    <p className="text-sm text-ink-secondary">Una dirección visual común y un fondo diferente para cada canción. Se conservan el tipo de fondo, la tipografía y los efectos asignados. En foto + efecto, cada canción mantiene una sola foto fija. Los prompts reemplazan la inspiración o el fondo de biblioteca; el nuevo fondo se crea al generar.</p>
    {itemIds.length > eligible.length && <Banner tone="warning">{itemIds.length - eligible.length} seleccionadas ya no admiten cambios de diseño y quedan fuera.</Banner>}
    {pinned.length > 0 && <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={replace} onChange={e => change(() => setReplace(e.target.checked))} />También cambiar los prompts de {pinned.length} diseños fijados</label>}
    <Field label="Dirección visual común"><textarea aria-label="Dirección visual común" rows={3} maxLength={3000} className={inputClass} value={common} onChange={e => change(() => setCommon(e.target.value))} placeholder="Atmósfera, lugares, luz y estilo compartidos por el lote" /></Field>
    <div className="space-y-2 rounded-xl bg-black/20 p-4 ring-1 ring-white/10">
      <Field label="Pegar descripciones de fondo · una por canción"><textarea aria-label="Lista de fondos" rows={4} className={inputClass} value={sceneList} onChange={e => change(() => setSceneList(e.target.value))} placeholder="Una avenida nocturna con un mural iluminado…&#10;Una ruta al amanecer bajo un cielo abierto…" /></Field>
      <div className="flex flex-wrap items-center justify-between gap-2"><p className="text-xs text-ink-secondary">{lines.length} fondos para {active.length} canciones. Se asignan en orden del calendario.</p>
        <Button size="sm" disabled={!active.length || lines.length !== active.length} onClick={() => change(() => setScenes(current => ({ ...current, ...Object.fromEntries(active.map((item, index) => [item.id, lines[index]])) })))}>Repartir fondos</Button></div>
    </div>
    <div className="space-y-3" aria-label="Fondos por canción">{active.map(item => <article key={item.id} className="rounded-xl bg-surface-2/60 p-4 ring-1 ring-white/10">
      <h3 className="font-semibold">{item.ordinal}. {item.title || item.filename}</h3><p className="mb-2 text-xs text-ink-secondary">{item.artist}{item.technical_code ? ` · ${item.technical_code}` : ""}</p>
      <textarea aria-label={`Fondo para ${item.title || item.filename}`} rows={2} maxLength={4000} className={inputClass} value={scenes[item.id] || ""} onChange={e => change(() => setScenes(current => ({ ...current, [item.id]: e.target.value })))} placeholder="Descripción de la foto o fondo de esta canción" />
      <p className={`mt-1 text-xs ${composePrompt(common, scenes[item.id]).length > 4000 ? "text-red-200" : "text-ink-secondary"}`}>{composePrompt(common, scenes[item.id]).length} / 4000 caracteres con la dirección común</p>
      {item.settings?.background_hint && <details className="mt-2 text-xs text-ink-secondary"><summary className="cursor-pointer">Ver prompt actual</summary><p className="mt-2 whitespace-pre-wrap">{item.settings.background_hint}</p></details>}
    </article>)}</div>
    <Field label="Cómo usar el prompt"><select aria-label="Cómo usar el prompt" className={inputClass} value={source} onChange={e => change(() => setSource(e.target.value))}><option value="prompt_improved">Mejorar la redacción con IA al generar</option><option value="prompt_literal">Usar el prompt exacto</option></select></Field>
    {repeated > 0 && <Banner tone="warning">Hay {repeated} {repeated === 1 ? "prompt repetido" : "prompts repetidos"}. Agregá descripciones diferentes si querés variedad visual.</Banner>}
    {eligible.length > 512 && <Banner tone="warning">Podés editar hasta 512 canciones por vez.</Banner>}
    {error && <Banner tone="danger">{error}</Banner>}
    {saved && <Banner tone="success">{saved}</Banner>}
    {!preview && !saved && <Button variant="primary" disabled={invalid || busy} onClick={prepare}>{busy ? "Preparando…" : `Revisar ${active.length} prompts`}</Button>}
    {preview && <section aria-label="Revisar prompts" className="space-y-4 rounded-xl bg-brand/10 p-4 ring-1 ring-brand/30">
      <h3 className="font-semibold">Así quedará cada prompt</h3>
      {preview.skipped.length > 0 && <p className="text-sm text-amber-200">{preview.skipped.length} diseños fijados se conservan.</p>}
      <ol className="space-y-3">{preview.changes.map(item => <li key={item.item_id}><strong className="text-sm">{item.title}</strong><p className="mt-1 whitespace-pre-wrap rounded-lg bg-black/20 p-3 text-sm text-ink-secondary">{item.after.background_hint}</p></li>)}</ol>
      <p className="text-xs text-ink-secondary">Guardar cambia los prompts. La generación se inicia desde la lista de canciones.</p>
      <Button variant="primary" onClick={save}>Guardar {preview.changes.length} prompts</Button>
    </section>}
  </fieldset>;
}
