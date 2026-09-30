import { useRef, useState } from "react";
import { campaignPost } from "../../lib/campaignApi";
import { PORTALS } from "../../lib/campaignPipeline";
import { Banner, Button, Field, Modal, inputClass } from "./ui";

function newKey(campaignId) {
  return `campaign-${campaignId}-${globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`}`;
}

/**
 * Durable send of approved cuts to a client portal. A retry after a lost
 * response reuses the same idempotency key, so the backend never creates a
 * second batch for the same selection and portal.
 */
export function SendToPortalDialog({ campaignId, kind = "lyric_video", videos, defaultPortal = "", lockedPortal = "", idempotencyKeys, onClose, onStarted }) {
  const [portal, setPortal] = useState(lockedPortal || defaultPortal);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const localKeys = useRef(new Map());
  const keys = idempotencyKeys || localKeys.current;
  const send = async () => {
    if (busy || !portal) return;
    setBusy(true); setError("");
    // Art-track sends select campaign items; lyric sends select jobs. Either
    // way the backend only publishes exactly this list.
    const selection = kind === "art_track"
      ? { item_ids: videos.map((video) => video.item_id) }
      : { job_ids: videos.map((video) => video.job_id) };
    const ids = Object.values(selection)[0];
    const signature = JSON.stringify([kind, portal, [...ids].sort()]);
    if (!keys.has(signature)) keys.set(signature, newKey(campaignId));
    try {
      const operation = await campaignPost(`/batch/campaigns/${encodeURIComponent(campaignId)}/deliveries`, {
        ...selection, destination_portal: portal, idempotency_key: keys.get(signature),
      });
      onStarted?.(operation, portal, ids.length);
    } catch (sendError) {
      setError(sendError.message || "No se pudo iniciar el envío.");
    } finally {
      setBusy(false);
    }
  };
  return <Modal label="Enviar videos aprobados" busy={busy} onClose={onClose}>
    <div className="space-y-4">
      <div>
        <p className="text-[11px] font-semibold uppercase tracking-[.18em] text-brand-light">Entrega</p>
        <h2 className="mt-1 text-xl font-semibold">Enviar {videos.length} {videos.length === 1 ? "video aprobado" : "videos aprobados"}</h2>
        <p className="mt-2 text-sm text-ink-secondary">Se publican sólo los videos de esta lista. El envío sigue en segundo plano y podés ver su avance en la campaña.</p>
      </div>
      <Field label="Portal de destino">
        <select aria-label="Portal de destino" className={inputClass} value={portal} disabled={Boolean(lockedPortal)} onChange={(event) => setPortal(event.target.value)}>
          <option value="">Elegí un portal</option>
          {Object.entries(PORTALS).map(([id, value]) => <option key={id} value={id}>{value.label} · {value.host}</option>)}
        </select>
      </Field>
      {lockedPortal && <p className="-mt-2 text-xs text-ink-secondary">Portal fijo de esta campaña.</p>}
      <ul className="max-h-44 space-y-1 overflow-auto rounded-xl bg-black/20 p-3 text-sm text-ink-secondary">
        {videos.map((video) => <li key={video.job_id} className="truncate"><span className="text-white">{video.title}</span> · {video.artist}</li>)}
      </ul>
      {error && <Banner tone="danger">{error}</Banner>}
      <div className="flex flex-wrap justify-end gap-2">
        <Button variant="ghost" disabled={busy} onClick={onClose}>Cancelar</Button>
        <Button variant="primary" disabled={busy || !portal || !videos.length} onClick={send}>{busy ? "Enviando…" : "Confirmar envío"}</Button>
      </div>
    </div>
  </Modal>;
}

/** Record a delivery made outside Genly (does not move any file). */
export function RecordDeliveryDialog({ campaignId, song, onClose, onDone }) {
  const [destination, setDestination] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const sha = song.video?.evidence?.video_sha256;
  const save = async () => {
    setBusy(true); setError("");
    try {
      await campaignPost(`/batch/campaigns/${encodeURIComponent(campaignId)}/creative/deliveries`, {
        job_id: song.current_job_id, video_sha256: sha, destination: destination.trim(),
      });
      onDone?.();
    } catch (saveError) {
      setError(saveError.message || "No se pudo registrar la entrega.");
    } finally {
      setBusy(false);
    }
  };
  return <Modal label="Registrar entrega" busy={busy} onClose={onClose}>
    <div className="space-y-4">
      <h2 className="text-xl font-semibold">Registrar entrega de {song.title}</h2>
      <p className="text-sm text-ink-secondary">Dejá constancia de dónde entregaste esta versión aprobada fuera de Genly. No envía ningún archivo.</p>
      <Field label="Destino y referencia">
        <input aria-label="Destino de entrega" autoFocus className={inputClass} value={destination} onChange={(event) => setDestination(event.target.value)} placeholder="Portal, carpeta o destinatario y referencia" />
      </Field>
      {!sha && <Banner tone="warning">Todavía no hay huella del archivo aprobado; recargá cuando termine de verificarse.</Banner>}
      {error && <Banner tone="danger">{error}</Banner>}
      <div className="flex justify-end gap-2">
        <Button variant="ghost" disabled={busy} onClick={onClose}>Cancelar</Button>
        <Button variant="primary" disabled={busy || !sha || destination.trim().length < 3} onClick={save}>Confirmar entrega realizada</Button>
      </div>
    </div>
  </Modal>;
}
