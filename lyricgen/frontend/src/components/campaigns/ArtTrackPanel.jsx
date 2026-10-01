import { useRef, useState } from "react";
import { campaignPost, campaignRequest } from "../../lib/campaignApi";
import { FILES_DESTINATION, artTrackPresetLabel } from "../../lib/campaignPipeline";
import { Banner, Button, ProgressBar } from "./ui";

async function digest(file) {
  const hash = await crypto.subtle.digest("SHA-256", await file.arrayBuffer());
  return [...new Uint8Array(hash)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function uploadAsset(asset) {
  const ticket = await campaignPost(`/batch/art-track-assets/${asset.id}/ticket`);
  if (ticket.complete) return;
  const file = asset.file;
  if (!ticket.use_multipart) {
    const response = await fetch(ticket.upload_url, { method: "PUT", headers: { "Content-Type": ticket.content_type }, body: file });
    if (!response.ok) throw new Error(`No se pudo subir ${file.name}`);
    await campaignPost(`/batch/art-track-assets/${asset.id}/complete`, { parts: [] });
    return;
  }
  const parts = [];
  const uploaded = new Map((ticket.uploaded_parts || []).map((part) => [
    Number(part.part_number || part.PartNumber), String(part.etag || part.ETag || "").replaceAll('"', ""),
  ]));
  for (const part of ticket.parts) {
    if (uploaded.get(part.part_number)) { parts.push({ part_number: part.part_number, etag: uploaded.get(part.part_number) }); continue; }
    const start = (part.part_number - 1) * ticket.part_size;
    const response = await fetch(part.url, { method: "PUT", body: file.slice(start, Math.min(start + ticket.part_size, file.size)) });
    if (!response.ok) throw new Error(`No se pudo subir la parte ${part.part_number} de ${file.name}`);
    parts.push({ part_number: part.part_number, etag: (response.headers.get("ETag") || "").replaceAll('"', "") });
  }
  await campaignPost(`/batch/art-track-assets/${asset.id}/complete`, { parts });
}

/** Art tracks: one folder with audios and covers, matched by name. */
export default function ArtTrackPanel({ campaign, onChanged }) {
  const id = campaign.id;
  const input = useRef(null);
  const [busy, setBusy] = useState("");
  const [progress, setProgress] = useState(null);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const run = async (label, fn) => {
    if (busy) return;
    setBusy(label); setError(""); setMessage("");
    try { await fn(); } catch (runError) { setError(runError.message); } finally { setBusy(""); setProgress(null); await onChanged?.(); }
  };
  const importFiles = (fileList) => run("upload", async () => {
    const files = [...fileList];
    const audios = [];
    const covers = [];
    for (const [index, file] of files.entries()) {
      setProgress({ label: `Leyendo ${index + 1} de ${files.length}`, value: index, max: files.length });
      const relative = file.webkitRelativePath || file.name;
      const sha = await digest(file);
      if (/\.(wav|mp3)$/i.test(file.name)) {
        const bits = file.name.replace(/\.[^.]+$/, "").split(" - ");
        audios.push({ client_id: sha, filename: file.name, relative_path: relative, artist: bits.length > 1 ? bits[0] : "", title: bits.length > 1 ? bits.slice(1).join(" - ") : bits[0], size_bytes: file.size, sha256: sha });
      } else if (/\.(jpg|jpeg|png)$/i.test(file.name)) {
        covers.push({ filename: file.name, relative_path: relative, size_bytes: file.size, sha256: sha, mime_type: file.type || undefined });
      }
    }
    const manifest = await campaignPost(`/batch/art-track-campaigns/${id}/manifest`, { audios, covers });
    const assets = await campaignRequest(`/batch/art-track-campaigns/${id}/assets`);
    const local = new Map(files.map((file) => [file.webkitRelativePath || file.name, file]));
    const pending = (assets.items || [])
      .map((asset) => ({ ...asset, file: local.get(asset.relative_path) || local.get(asset.filename) }))
      .filter((asset) => asset.file && asset.upload_state !== "uploaded");
    // Bounded parallelism: each asset is independently resumable, so a
    // retry with the same folder skips what already landed.
    let cursor = 0;
    let done = 0;
    const worker = async () => {
      while (cursor < pending.length) {
        const index = cursor++;
        await uploadAsset(pending[index]);
        done += 1;
        setProgress({ label: `Subiendo ${done} de ${pending.length}`, value: done, max: pending.length });
      }
    };
    await Promise.all(Array.from({ length: Math.min(4, pending.length) }, worker));
    setMessage(`${manifest.registered_count} audios registrados; ${manifest.matched_count} portadas asociadas. Confirmá las asociaciones antes de generar.`);
  });
  return <div className="space-y-4">
    <div className="flex flex-wrap items-start justify-between gap-4">
      <div className="max-w-xl">
        <h3 className="font-semibold">Audios y portadas</h3>
        <p className="mt-1 text-sm text-ink-secondary">Elegí una carpeta con hasta 500 audios y sus portadas (<span className="font-mono text-xs">Artista - Título</span>). Las que no se asocian quedan bloqueadas para corregirlas a mano.</p>
        <p className="mt-1 text-xs text-ink-secondary">Estilo: {artTrackPresetLabel(campaign.default_render_params?.art_track_preset).toLowerCase()}</p>
      </div>
      <Button variant="primary" disabled={!!busy} onClick={() => input.current?.click()}>{busy === "upload" ? "Subiendo…" : "Elegir carpeta"}</Button>
      <input ref={input} type="file" multiple webkitdirectory="" directory="" accept=".wav,.mp3,.jpg,.jpeg,.png" className="sr-only" aria-label="Carpeta de art tracks"
        onChange={(event) => { const files = [...(event.target.files || [])]; event.target.value = ""; if (files.length) void importFiles(files); }} />
    </div>
    {progress && <div role="status" className="space-y-1.5 text-sm"><span>{progress.label}</span><ProgressBar value={progress.value} max={progress.max} label="Progreso" /></div>}
    <div className="flex flex-wrap gap-2 border-t border-white/[0.06] pt-4">
      <Button disabled={!!busy} onClick={() => run("render", async () => {
        await campaignPost(`/batch/art-track-campaigns/${id}/associations/confirm`, { confirm_all_matched: true });
        const result = await campaignPost(`/batch/art-track-campaigns/${id}/start-rendering`);
        setMessage(`Generación iniciada: ${result.created_count} art tracks; ${result.blocked_item_ids?.length || 0} pendientes de asociación.`);
      })}>{busy === "render" ? "Confirmando…" : "Confirmar asociados y generar"}</Button>
      {campaign.destination_portal === FILES_DESTINATION
        ? <span className="self-center text-xs text-ink-secondary">Entrega por archivos: cada máster queda disponible por canción; la entrega del lote se coordina aparte.</span>
        : <>
      <Button variant="ghost" disabled={!!busy} onClick={() => run("preview", async () => {
        const result = await campaignPost(`/batch/art-track-campaigns/${id}/delivery-preview`);
        setMessage(`${result.eligible_count} art tracks aprobados para ${result.hostname}.`);
      })}>Previsualizar envíos</Button>
      <Button variant="ghost" disabled={!!busy} onClick={() => run("send", async () => {
        const result = await campaignPost(`/batch/art-track-campaigns/${id}/deliveries`, { idempotency_key: `ui-${id}-${Date.now()}` });
        setMessage(result.scheduled === false
          ? `Envío guardado (${result.total_count} canciones), pendiente de worker en ${result.hostname}.`
          : `Envío durable creado: ${result.total_count} canciones a ${result.hostname}.`);
      })}>Enviar aprobados</Button>
        </>}
    </div>
    {message && <Banner tone="success">{message}</Banner>}
    {error && <Banner tone="danger">{error}</Banner>}
  </div>;
}
