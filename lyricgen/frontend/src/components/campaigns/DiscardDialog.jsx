import { useState } from "react";
import { campaignPost } from "../../lib/campaignApi";
import { Banner, Button, Field, Modal, ProgressBar, inputClass } from "./ui";

/**
 * Discard or restore one or many songs. Audio and draft are kept; each
 * change is audited server-side with the reason. Songs are processed one by
 * one so a lock on one song never blocks the rest.
 */
export default function DiscardDialog({ campaignId, songs, mode = "discard", onClose, onDone }) {
  const restoring = mode === "restore";
  const [reason, setReason] = useState("Instrumental · solicitud del cliente");
  const [progress, setProgress] = useState(null);
  const [failures, setFailures] = useState([]);
  const busy = Boolean(progress);
  const run = async () => {
    const failed = [];
    let done = 0;
    for (const [index, song] of songs.entries()) {
      setProgress({ index, total: songs.length, title: song.title });
      try {
        await campaignPost(`/batch/campaigns/${encodeURIComponent(campaignId)}/items/${encodeURIComponent(song.id)}/${restoring ? "restore" : "discard"}`,
          restoring ? {} : { reason: reason.trim() });
        done += 1;
      } catch (error) {
        failed.push({ title: song.title, message: error.message });
      }
    }
    setProgress(null);
    setFailures(failed);
    if (!failed.length) onDone?.({ done, failed });
    else if (done) onDone?.({ done, failed, keepOpen: true });
  };
  const single = songs.length === 1 ? songs[0] : null;
  const verb = restoring ? "Recuperar" : "Descartar";
  return <Modal label={single ? `${verb} canción` : `${verb} canciones`} busy={busy} onClose={onClose}>
    <div className="space-y-4">
      <h2 className="text-xl font-semibold">{verb} · {single ? single.title : `${songs.length} canciones`}</h2>
      <p className="text-sm text-ink-secondary">{single?.artist ? `${single.artist} · ` : ""}El audio y el borrador se conservan. El cambio queda registrado{restoring ? "" : " con el motivo"}.</p>
      {!restoring && <Field label="Motivo">
        <input autoFocus aria-label="Motivo" className={inputClass} value={reason} maxLength={500} onChange={(event) => setReason(event.target.value)} />
      </Field>}
      {!single && <ul className="max-h-36 overflow-auto rounded-xl bg-black/20 p-3 text-sm text-ink-secondary">{songs.map((song) => <li key={song.id} className="truncate">{song.title}</li>)}</ul>}
      {progress && <div role="status" className="space-y-2 text-sm"><span>Procesando {progress.index + 1} de {progress.total} · {progress.title}</span><ProgressBar value={progress.index} max={progress.total} label="Progreso" /></div>}
      {failures.length > 0 && <Banner tone="danger">No se pudo {restoring ? "recuperar" : "descartar"} {failures.length === 1 ? "1 canción" : `${failures.length} canciones`}: {failures.map((failure) => `${failure.title}: ${failure.message}`).join("; ")}</Banner>}
      <div className="flex justify-end gap-2">
        <Button variant="ghost" disabled={busy} onClick={onClose}>Cancelar</Button>
        <Button variant={restoring ? "primary" : "danger"} autoFocus={restoring} disabled={busy || (!restoring && reason.trim().length < 3)} onClick={run}>
          {busy ? "Guardando…" : restoring ? "Confirmar recuperación" : "Confirmar descarte"}
        </Button>
      </div>
    </div>
  </Modal>;
}
