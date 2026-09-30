import { useRef, useState } from "react";
import { campaignRequest } from "../../lib/campaignApi";
import { campaignGenerateForm } from "../../lib/campaignCreative";
import { Button, Modal, ProgressBar } from "./ui";

const CAPACITY_CODES = new Set(["batch_render_window_full", "batch_final_review_full"]);

/**
 * Sends one /generate per song, sequentially, so the backend's render window
 * is respected. When the window fills (429), the rest stays selected for a
 * later retry instead of being reported as failures. Every song is sent at
 * most once per confirmation.
 */
export async function submitGeneration(batch, { onProgress } = {}) {
  let sent = 0;
  const failures = [];
  const sentIds = new Set();
  let deferred = [];
  let capacityMessage = "";
  onProgress?.({ attempted: 0, sent: 0, total: batch.length, current: batch[0]?.title || "" });
  for (const [index, item] of batch.entries()) {
    onProgress?.({ attempted: index, sent, total: batch.length, current: item.title });
    try {
      const job = await campaignRequest(`/status/${encodeURIComponent(item.job_id)}`);
      await campaignRequest("/generate", { method: "POST", body: campaignGenerateForm(item, job) });
      sent += 1;
      sentIds.add(item.id);
    } catch (error) {
      if (error.status === 429 && CAPACITY_CODES.has(error.code)) {
        deferred = batch.slice(index);
        capacityMessage = error.message;
        break;
      }
      failures.push({ id: item.id, title: item.title, message: error.message });
    }
    onProgress?.({ attempted: index + 1, sent, total: batch.length, current: batch[index + 1]?.title || "" });
  }
  return { sent, sentIds, failures, deferred, capacityMessage };
}

export function generationSummary({ sent, failures, deferred, capacityMessage }) {
  const message = `${sent} ${sent === 1 ? "trabajo enviado" : "trabajos enviados"}${failures.length ? ` · ${failures.length} no se enviaron` : ""}; seguí el avance en la etapa Generando.`;
  const notice = deferred.length
    ? `${capacityMessage} ${deferred.length} ${deferred.length === 1 ? "video quedó sin enviar y sigue seleccionado" : "videos quedaron sin enviar y siguen seleccionados"}. Volvé a generar los pendientes cuando haya lugar.`
    : "";
  const error = failures.length
    ? `No se pudieron enviar ${failures.length} ${failures.length === 1 ? "video" : "videos"}: ${failures.map((failure) => `${failure.title}: ${failure.message}`).join("; ")}.`
    : "";
  return { message, notice, error };
}

export default function GenerateDialog({ items, onClose, onDone }) {
  const [progress, setProgress] = useState(null);
  const running = useRef(false);
  const busy = Boolean(progress);
  const confirm = async () => {
    if (running.current) return;
    running.current = true;
    try {
      const result = await submitGeneration(items, { onProgress: setProgress });
      onDone?.(result);
    } finally {
      running.current = false;
      setProgress(null);
    }
  };
  const groups = items.reduce((map, item) => {
    const name = item.assignment?.group_name || "Estilo base";
    map.set(name, (map.get(name) || 0) + 1);
    return map;
  }, new Map());
  return <Modal label="Confirmar generación" busy={busy} onClose={onClose}>
    <div className="space-y-4">
      <div>
        <p className="text-[11px] font-semibold uppercase tracking-[.18em] text-brand-light">Generar videos</p>
        <h2 className="mt-1 text-xl font-semibold">Generar {items.length} {items.length === 1 ? "video" : "videos"}</h2>
        <p className="mt-2 text-sm text-ink-secondary">Genera fondos y videos con el estilo asignado a cada canción y consume el cupo correspondiente.</p>
      </div>
      <div className="flex flex-wrap gap-2">{[...groups].map(([name, count]) => <span key={name} className="rounded-lg bg-white/[0.05] px-2.5 py-1 text-xs text-ink-secondary ring-1 ring-white/10"><strong className="text-white">{count}</strong> · {name}</span>)}</div>
      <ul className="max-h-44 space-y-1 overflow-auto rounded-xl bg-black/20 p-3 text-sm">{items.map((item) => <li key={item.id} className="flex justify-between gap-3"><span className="truncate">{item.title}</span><span className="shrink-0 text-xs text-ink-secondary">{item.assignment?.group_name || "Estilo base"}</span></li>)}</ul>
      {progress && <div role="status" aria-live="polite" className="space-y-2 rounded-xl bg-brand/10 p-4 ring-1 ring-brand/30">
        <div className="flex items-center gap-3">
          <span className="h-5 w-5 shrink-0 animate-spin rounded-full border-2 border-brand-light/30 border-t-brand-light" aria-hidden="true" />
          <div className="min-w-0"><strong className="block">{progress.attempted < progress.total ? `Enviando ${progress.attempted + 1} de ${progress.total}` : `Finalizando · ${progress.sent} enviados`}</strong>
            <span className="block truncate text-sm text-ink-secondary">{progress.current || "Actualizando el estado de la campaña…"}</span></div>
        </div>
        <ProgressBar value={Math.max(5, progress.attempted)} max={progress.total} label="Progreso del envío" />
        <p className="text-xs text-ink-secondary">No cierres esta ventana. Cada video se envía una sola vez.</p>
      </div>}
      <div className="flex flex-wrap justify-end gap-2">
        <Button variant="ghost" disabled={busy} onClick={onClose}>Cancelar</Button>
        <Button variant="primary" disabled={busy || !items.length} onClick={confirm}>
          {progress ? progress.attempted < progress.total ? `Enviando ${progress.attempted + 1} de ${progress.total}…` : "Actualizando estado…" : "Confirmar generación"}
        </Button>
      </div>
    </div>
  </Modal>;
}
