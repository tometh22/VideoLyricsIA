import { useState } from "react";
import { campaignPost } from "../../lib/campaignApi";
import { Button, Modal } from "./ui";

/** Approve only the selected, still pending video jobs through the normal QC gate. */
export default function BulkApproveDialog({ songs, onClose, onDone }) {
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState(null);

  const approve = async () => {
    if (busy) return;
    setBusy(true);
    const approvedIds = [];
    const failed = [];
    for (const [index, song] of songs.entries()) {
      setProgress({ current: index + 1, total: songs.length, title: song.title });
      try {
        await campaignPost(`/approve/${encodeURIComponent(song.current_job_id)}`, {
          notes: "Aprobación final masiva desde la campaña, tras revisión humana",
        });
        approvedIds.push(song.id);
      } catch (approvalError) {
        failed.push({ id: song.id, title: song.title, reason: approvalError.message || "Error desconocido" });
      }
    }
    setBusy(false);
    setProgress(null);
    onDone?.({ approvedIds, failed });
  };

  return <Modal label="Aprobar videos seleccionados" busy={busy} onClose={onClose} width="max-w-lg">
    <div className="space-y-4">
      <h2 className="text-xl font-semibold">Aprobar {songs.length} {songs.length === 1 ? "video" : "videos"}</h2>
      <p className="text-sm text-ink-secondary">Confirmá que ya revisaste estos videos. La aprobación no los publica: después vas a elegir el portal y confirmar el envío.</p>
      <ul className="max-h-40 space-y-1 overflow-auto rounded-xl bg-black/20 p-3 text-sm text-ink-secondary">
        {songs.map((song) => <li key={song.id} className="truncate"><span className="text-white">{song.title}</span> · {song.artist}</li>)}
      </ul>
      {progress && <div role="status" aria-live="polite" className="space-y-2 rounded-xl bg-brand/10 p-3 text-sm ring-1 ring-brand/30">
        <p>Aprobando {progress.current} de {progress.total}: {progress.title}</p>
        <div className="h-2 overflow-hidden rounded-full bg-black/30"><div className="h-full bg-brand transition-all" style={{ width: `${100 * (progress.current - 1) / progress.total}%` }} /></div>
        <p className="text-xs text-ink-secondary">Mantené esta pestaña abierta hasta que termine.</p>
      </div>}
      <div className="flex justify-end gap-2">
        <Button variant="ghost" disabled={busy} onClick={onClose}>Cancelar</Button>
        <Button variant="primary" disabled={busy || !songs.length} onClick={() => void approve()}>{busy ? "Aprobando…" : "Confirmar aprobación"}</Button>
      </div>
    </div>
  </Modal>;
}
