import { useCallback, useEffect, useRef, useState } from "react";
import { useMediaUrl } from "../../mediaUrl";
import { campaignPost } from "../../lib/campaignApi";
import { displayCode, portalLabel } from "../../lib/campaignPipeline";
import { Button, Chip, Kbd, Modal } from "./ui";

function isTyping(target) {
  return target?.closest?.("input, textarea, select, [contenteditable=true]");
}

/**
 * Focus mode for final QC: one video at a time, approve and continue with
 * the next one of the list the operator opened it from. Approval always
 * targets the video on screen; nothing is approved in bulk.
 */
export default function VideoReviewPlayer({ queue, startId, onClose, onApproved, onEdit }) {
  const [index, setIndex] = useState(() => Math.max(0, queue.findIndex((song) => song.id === startId)));
  const [approvedIds, setApprovedIds] = useState(() => new Set());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const videoRef = useRef(null);
  const song = queue[index];
  const jobId = song?.current_job_id;
  const url = useMediaUrl(jobId, "video", "preview", song?.video?.evidence?.video_sha256 || song?.approved_at || "");
  const reviewable = song?.stage === "qc" && song?.current_status === "pending_review" && !approvedIds.has(song.id);
  const remaining = queue.filter((item) => item.stage === "qc" && !approvedIds.has(item.id)).length;

  const go = useCallback((delta) => {
    setError(""); setNotice("");
    setIndex((value) => Math.min(queue.length - 1, Math.max(0, value + delta)));
  }, [queue.length]);

  const approve = useCallback(async () => {
    if (!reviewable || busy || !url) return;
    setBusy(true); setError(""); setNotice("");
    try {
      await campaignPost(`/approve/${encodeURIComponent(jobId)}`, { notes: "Revisión final desde el reproductor de campaña" });
      const nextApproved = new Set(approvedIds).add(song.id);
      setApprovedIds(nextApproved);
      onApproved?.(song);
      const next = queue.findIndex((item, position) => position > index && item.stage === "qc" && !nextApproved.has(item.id));
      const wrap = next >= 0 ? next : queue.findIndex((item) => item.stage === "qc" && !nextApproved.has(item.id));
      if (wrap >= 0) { setIndex(wrap); setNotice(`${song.title} quedó aprobado.`); }
      else onClose?.({ finished: true, lastTitle: song.title });
    } catch (approvalError) {
      setError(approvalError.message || "No se pudo aprobar el video.");
    } finally {
      setBusy(false);
    }
  }, [approvedIds, busy, index, jobId, onApproved, onClose, queue, reviewable, song, url]);

  useEffect(() => {
    const onKey = (event) => {
      if (event.metaKey || event.ctrlKey || event.altKey || isTyping(event.target)) return;
      const key = event.key.toLowerCase();
      if (key === "a") { event.preventDefault(); void approve(); }
      else if (key === "arrowright" || key === "j") { event.preventDefault(); go(1); }
      else if (key === "arrowleft" || key === "k") { event.preventDefault(); go(-1); }
      else if (key === "e" && song) { event.preventDefault(); onEdit?.(song); }
      else if (key === " " && videoRef.current && event.target?.tagName !== "BUTTON" && event.target?.tagName !== "VIDEO") {
        event.preventDefault();
        if (videoRef.current.paused) void videoRef.current.play?.(); else videoRef.current.pause?.();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [approve, go, onEdit, song]);

  if (!song) return null;
  return <Modal label={`Reproducir ${song.title}`} busy={busy} onClose={() => onClose?.({ finished: false })} width="max-w-5xl">
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-[11px] font-semibold uppercase tracking-[.18em] text-brand-light">Revisión de video · {index + 1} de {queue.length}{remaining ? ` · ${remaining} por aprobar` : ""}</p>
          <h2 className="mt-1 truncate text-xl font-semibold">{song.title}</h2>
          <p className="text-sm text-ink-secondary">{song.artist}{displayCode(song.technical_code) ? ` · ${displayCode(song.technical_code)}` : ""}</p>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {approvedIds.has(song.id) && <Chip tone="success">Aprobado</Chip>}
            {song.portals?.length > 0 && <Chip tone="brand">En {song.portals.map(portalLabel).join(" y ")}</Chip>}
            {song.pending_change_requests > 0 && <Chip tone="warning">{song.pending_change_requests} {song.pending_change_requests === 1 ? "cambio pedido" : "cambios pedidos"}</Chip>}
            {song.current_is_variant && <Chip>Variante</Chip>}
          </div>
        </div>
        <Button variant="ghost" size="sm" disabled={busy} onClick={() => onClose?.({ finished: false })}>Cerrar <Kbd>Esc</Kbd></Button>
      </div>
      <div className="flex max-h-[62vh] min-h-52 items-center justify-center overflow-hidden rounded-xl bg-black ring-1 ring-white/10">
        {url ? <video ref={videoRef} key={jobId} className="max-h-[62vh] w-full" src={url} controls autoPlay playsInline preload="metadata">Tu navegador no puede reproducir este video.</video>
          : <p role="status" className="p-8 text-sm text-ink-secondary">Preparando el reproductor…</p>}
      </div>
      {error && <p role="alert" className="rounded-lg bg-red-500/10 px-3 py-2 text-sm text-red-200">{error}</p>}
      {notice && <p role="status" className="text-sm text-emerald-200">{notice}</p>}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-1.5">
          <Button size="sm" variant="ghost" disabled={index === 0 || busy} onClick={() => go(-1)} aria-label="Video anterior">← <Kbd>K</Kbd></Button>
          <Button size="sm" variant="ghost" disabled={index >= queue.length - 1 || busy} onClick={() => go(1)} aria-label="Video siguiente">
            <Kbd>J</Kbd> →</Button>
          <Button size="sm" variant="secondary" disabled={busy} onClick={() => onEdit?.(song)}>Corregir letra o tiempos <Kbd>E</Kbd></Button>
        </div>
        {reviewable && <div className="flex items-center gap-3">
          <p className="hidden text-xs text-ink-secondary sm:block">Al aprobar confirmás que viste el video completo.</p>
          <Button variant="primary" size="lg" disabled={busy || !url} onClick={approve}>{busy ? "Aprobando…" : "Aprobar y siguiente"} <Kbd>A</Kbd></Button>
        </div>}
      </div>
    </div>
  </Modal>;
}
