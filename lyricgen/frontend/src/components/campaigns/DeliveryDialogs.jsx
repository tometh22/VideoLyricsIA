import { useEffect, useRef, useState } from "react";
import { campaignPost, campaignRequest } from "../../lib/campaignApi";
import { PORTALS, portalLabel } from "../../lib/campaignPipeline";
import { Banner, Button, Field, Modal, inputClass } from "./ui";

function newKey(campaignId) {
  return `campaign-${campaignId}-${globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`}`;
}

/**
 * Durable send of approved cuts to a client portal. A retry after a lost
 * response reuses the same idempotency key, so the backend never creates a
 * second batch for the same selection and portal.
 */
export function SendToPortalDialog({ campaignId, kind = "lyric_video", videos, defaultPortal = "", lockedPortal = "", idempotencyKeys, canClose = false, onClose, onStarted }) {
  const [portal, setPortal] = useState(lockedPortal || defaultPortal);
  const [candidates, setCandidates] = useState([]);
  const [ticked, setTicked] = useState(() => new Set());
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const localKeys = useRef(new Map());
  const keys = idempotencyKeys || localKeys.current;
  // Requests this send could close: the backend decides with the same rule the
  // worker re-checks. Nothing is ticked by default.
  useEffect(() => {
    if (!canClose || kind === "art_track") return undefined;
    const abort = new AbortController();
    campaignRequest(`/batch/campaigns/${encodeURIComponent(campaignId)}/change-requests?status=open&limit=200`, { signal: abort.signal })
      .then((result) => { if (!abort.signal.aborted) setCandidates((result.items || []).filter((item) => item.closable_on_publish)); })
      .catch(() => { if (!abort.signal.aborted) setCandidates([]); });
    return () => abort.abort();
  }, [campaignId, canClose, kind]);
  const songIds = new Set(videos.map((video) => video.item_id));
  const closable = candidates.filter((item) => songIds.has(item.song_id) && item.portal_id === portal);
  const chosen = closable.filter((item) => ticked.has(item.id));
  const toggle = (id) => setTicked((old) => { const copy = new Set(old); if (copy.has(id)) copy.delete(id); else copy.add(id); return copy; });
  const send = async () => {
    if (busy || !portal) return;
    setBusy(true); setError("");
    // Art-track sends select campaign items; lyric sends select jobs. Either
    // way the backend only publishes exactly this list.
    const selection = kind === "art_track"
      ? { item_ids: videos.map((video) => video.item_id) }
      : { job_ids: videos.map((video) => video.job_id) };
    const ids = Object.values(selection)[0];
    const resolve = {};
    chosen.forEach((item) => {
      const video = videos.find((entry) => entry.item_id === item.song_id);
      if (video) resolve[video.job_id] = [...(resolve[video.job_id] || []), item.id].sort((a, b) => a - b);
    });
    const closing = Object.keys(resolve).length > 0;
    const signature = JSON.stringify([kind, portal, [...ids].sort(), resolve, closing ? note.trim() : ""]);
    if (!keys.has(signature)) keys.set(signature, newKey(campaignId));
    try {
      const operation = await campaignPost(`/batch/campaigns/${encodeURIComponent(campaignId)}/deliveries`, {
        ...selection, destination_portal: portal, idempotency_key: keys.get(signature),
        ...(closing ? { resolve_requests: resolve, ...(note.trim() ? { resolution_note: note.trim() } : {}) } : {}),
      });
      // The key exists to make a LOST response safe to retry. Once the operation
      // is confirmed, forget it: sending the same selection later (after a partial
      // failure, or a re-render of the same job) is a new send, not a replay.
      keys.delete(signature);
      onStarted?.(operation, portal, ids.length);
    } catch (sendError) {
      setError(sendError.code === "change_request_not_closable"
        ? "Uno de los pedidos elegidos ya no se puede cerrar con este envío (cambió o lo cerró otra persona). Volvé a abrir el envío para ver la lista actual."
        : sendError.message || "No se pudo iniciar el envío.");
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
      {closable.length > 0 && <fieldset className="space-y-2 rounded-xl bg-amber-400/[0.06] p-3 ring-1 ring-amber-300/20">
        <legend className="px-1 text-sm font-medium text-amber-100">Pedidos del cliente que este envío puede resolver</legend>
        <p className="text-xs text-ink-secondary">Solo se cierran los que marques. Los demás siguen abiertos.</p>
        <ul className="space-y-1.5">
          {closable.map((item) => <li key={item.id}><label className="flex cursor-pointer items-start gap-2 text-sm">
            <input type="checkbox" className="mt-1" checked={ticked.has(item.id)} disabled={busy} onChange={() => toggle(item.id)} aria-label={`Resolver el pedido de ${item.song}`} />
            <span><span className="text-white">{item.song}</span> · {portalLabel(item.portal_id)}<span className="block text-xs text-ink-secondary">{item.comment}</span></span>
          </label></li>)}
        </ul>
        {chosen.length > 0 && <label className="block text-xs text-ink-secondary">Qué cambió (lo ve el cliente; opcional)
          <textarea aria-label="Qué cambió" className={`${inputClass} mt-1 min-h-[64px]`} maxLength={2000} value={note} disabled={busy} onChange={(event) => setNote(event.target.value)} placeholder="Ej.: Corregimos la palabra del segundo verso." />
        </label>}
      </fieldset>}
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
