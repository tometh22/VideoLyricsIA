import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import useDialogA11y from "../../hooks/useDialogA11y";
import { campaignRequest } from "../../lib/campaignApi";
import { formatDuration, portalLabel, relativeDate, stageMeta, stagesFor, toneOf } from "../../lib/campaignPipeline";
import CampaignReviewWork from "../CampaignReviewWork";
import { CampaignReviewerRow } from "../CampaignReviewerStatus";
import { canDiscard, canSend, primaryAction, statusNote } from "./songModel";
import { Banner, Button, Chip, Field, StageBadge, inputClass } from "./ui";

const VERSION_STATUS = {
  queued: "En cola", processing: "Generando", rendering: "Renderizando", editing: "Nueva versión en curso",
  background_generating: "Generando fondo", pending_review: "Por revisar", done: "Aprobado", error: "Falló", rejected: "Rechazado",
};

function timestamp(seconds) {
  if (!Number.isFinite(Number(seconds))) return "—";
  const value = Math.max(0, Number(seconds));
  return `${Math.floor(value / 60)}:${String(Math.floor(value % 60)).padStart(2, "0")}`;
}

function Timeline({ song, kind }) {
  const stages = stagesFor(kind);
  const current = stages.findIndex((stage) => stage.key === song.stage);
  return <ol className="grid gap-0" aria-label="Recorrido de la canción">
    {stages.map((stage, index) => {
      const state = current < 0 ? "idle" : index < current ? "done" : index === current ? "current" : "todo";
      return <li key={stage.key} className="relative flex gap-3 pb-3 last:pb-0">
        {index < stages.length - 1 && <span aria-hidden="true" className={`absolute left-[7px] top-4 h-full w-px ${state === "done" ? "bg-white/30" : "bg-white/10"}`} />}
        <span aria-hidden="true" className={`relative z-10 mt-0.5 h-3.5 w-3.5 shrink-0 rounded-full ring-2 ${state === "current" ? `${toneOf(stage.key).dot} ring-white/40` : state === "done" ? "bg-white/50 ring-transparent" : "bg-surface-2 ring-white/15"}`} />
        <div className={`text-sm ${state === "current" ? "font-semibold text-white" : state === "done" ? "text-ink-secondary" : "text-ink-secondary/50"}`}>
          {stage.title}{state === "current" && <span className="block text-xs font-normal text-ink-secondary">{stage.hint}</span>}
        </div>
      </li>;
    })}
  </ol>;
}

function MetadataForm({ campaignId, song, onSaved }) {
  const [values, setValues] = useState({ title: song.title || "", artist: song.artist || "", technical_code: song.technical_code || "" });
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  useEffect(() => { setValues({ title: song.title || "", artist: song.artist || "", technical_code: song.technical_code || "" }); }, [song.id, song.title, song.artist, song.technical_code]);
  const dirty = values.title !== (song.title || "") || values.artist !== (song.artist || "") || values.technical_code !== (song.technical_code || "");
  const save = async (event) => {
    event.preventDefault();
    setBusy(true); setError(""); setMessage("");
    try {
      const result = await campaignRequest(`/batch/campaigns/${encodeURIComponent(campaignId)}/items/${encodeURIComponent(song.id)}`, { method: "PATCH", json: values });
      setMessage(result.metadata_error ? "Guardado. Todavía falta completar datos." : "Datos guardados.");
      onSaved?.();
    } catch (saveError) {
      setError(saveError.message || "No se pudieron guardar los datos.");
    } finally {
      setBusy(false);
    }
  };
  return <form onSubmit={save} className="space-y-3">
    <Field label="Título"><input className={inputClass} value={values.title} maxLength={500} onChange={(event) => setValues({ ...values, title: event.target.value })} /></Field>
    <Field label="Artista"><input className={inputClass} value={values.artist} maxLength={255} onChange={(event) => setValues({ ...values, artist: event.target.value })} /></Field>
    <Field label="Código ARF / ARUM"><input className={`${inputClass} font-mono uppercase`} value={values.technical_code} maxLength={64} onChange={(event) => setValues({ ...values, technical_code: event.target.value })} /></Field>
    {error && <Banner tone="danger">{error}</Banner>}
    {message && <p role="status" className="text-xs text-emerald-200">{message}</p>}
    <Button type="submit" variant="secondary" size="sm" disabled={busy || !dirty}>{busy ? "Guardando…" : "Guardar datos"}</Button>
  </form>;
}

export default function SongDrawer({ song, kind, campaignId, canManage, portalSends = true, reviewerEnabled, onClose, onAction, onNavigate, onChanged }) {
  const dialogRef = useDialogA11y({ onClose });
  const [editing, setEditing] = useState(false);
  useEffect(() => { setEditing(song?.metadata_error === "missing_metadata"); }, [song?.id, song?.metadata_error]);
  if (!song) return null;
  const action = primaryAction(song, { kind, canManage, portalSends });
  const note = statusNote(song);
  const videoJob = song.current_job_id;
  const lyricStagePassed = kind !== "art_track" && ["ready", "rendering", "qc", "approved", "delivered"].includes(song.stage);
  return createPortal(<div className="fixed inset-0 z-[85] flex justify-end bg-black/50 backdrop-blur-[2px]" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
    <aside ref={dialogRef} tabIndex={-1} role="dialog" aria-modal="true" aria-label={`Canción ${song.title}`}
      className="flex h-full w-full max-w-md flex-col overflow-hidden bg-surface-1 text-white shadow-depth-lg ring-1 ring-white/10 animate-[genly-slide-in_.24s_cubic-bezier(.2,.8,.2,1)]">
      <header className="border-b border-white/[0.06] p-5">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <p className="text-[11px] font-semibold uppercase tracking-[.18em] text-ink-secondary">Canción #{song.ordinal}</p>
            <h2 className="mt-1 break-words text-lg font-semibold leading-tight">{song.title}</h2>
            <p className="mt-0.5 text-sm text-ink-secondary">{song.artist || "Artista sin informar"}{song.technical_code ? ` · ${song.technical_code}` : ""}</p>
          </div>
          <Button variant="ghost" size="sm" onClick={onClose} aria-label="Cerrar detalle">✕</Button>
        </div>
        <div className="mt-3 flex flex-wrap items-center gap-2"><StageBadge stage={song.stage} />{note && <span className="text-xs text-ink-secondary">{note}</span>}</div>
        <div className="mt-4 flex flex-wrap gap-2">
          {action && <Button variant="primary" size="sm" onClick={() => onAction(song, action.key)}>{action.label}</Button>}
          {song.stage === "lyrics" && canDiscard(song) && <Button size="sm" variant="ghost" onClick={() => onAction(song, "discard")}>Descartar</Button>}
          {song.stage === "attention" && canDiscard(song) && <Button size="sm" variant="ghost" onClick={() => onAction(song, "discard")}>Descartar</Button>}
          {lyricStagePassed && song.job_id && <Button size="sm" variant="secondary" onClick={() => onAction(song, "edit-lyrics")}>Editar letra y tiempos</Button>}
          {videoJob && ["qc", "approved", "delivered", "rendering"].includes(song.stage) && <Button size="sm" variant="ghost" onClick={() => onNavigate(`/videos/${encodeURIComponent(videoJob)}`)}>Detalle del video</Button>}
          {canManage && portalSends && canSend(song) && song.stage === "delivered" && !song.portal_outdated && <Button size="sm" variant="ghost" onClick={() => onAction(song, "send")}>Enviar a otro portal</Button>}
          {canManage && canSend(song) && song.video?.evidence?.video_sha256 && <Button size="sm" variant="ghost" onClick={() => onAction(song, "record-delivery")}>Registrar entrega manual</Button>}
        </div>
      </header>
      <div className="flex-1 space-y-6 overflow-y-auto p-5">
        {song.stage === "attention" && song.error && <Banner tone="danger">{song.error}</Banner>}
        {song.stage === "discarded" && <Banner tone="warning">{song.review?.discard?.reason || song.discard?.reason || "Descartada"}</Banner>}
        {song.stage === "lyrics" && song.review && <section className="space-y-2">
          <h3 className="text-xs font-semibold uppercase tracking-wider text-ink-secondary">Qué revisar</h3>
          <div className="rounded-xl bg-black/20 p-3 ring-1 ring-white/[0.06]"><CampaignReviewWork row={song.review} timestamp={timestamp} /></div>
        </section>}
        {reviewerEnabled && song.review?.reviewer_campaign_status && <section className="space-y-2">
          <h3 className="text-xs font-semibold uppercase tracking-wider text-ink-secondary">Revisión asistida</h3>
          <CampaignReviewerRow status={song.review.reviewer_campaign_status} jobId={song.job_id} onOpen={onNavigate} />
        </section>}
        {(song.portals?.length > 0 || song.portal_outdated || song.pending_change_requests > 0) && <section className="space-y-2">
          <h3 className="text-xs font-semibold uppercase tracking-wider text-ink-secondary">Portal del cliente</h3>
          <div className="flex flex-wrap gap-1.5">
            {song.portals?.map((portal) => <Chip key={portal} tone="brand">En {portalLabel(portal)}</Chip>)}
            {song.portal_outdated && <Chip tone="danger">Portal desactualizado: entrega un corte anterior</Chip>}
            {song.pending_change_requests > 0 && <Chip tone="warning">{song.pending_change_requests} {song.pending_change_requests === 1 ? "cambio pedido" : "cambios pedidos"}</Chip>}
          </div>
        </section>}
        <section className="space-y-2">
          <h3 className="text-xs font-semibold uppercase tracking-wider text-ink-secondary">Recorrido</h3>
          {["attention", "discarded"].includes(song.stage)
            ? <p className="text-sm text-ink-secondary">{stageMeta(song.stage).hint}</p>
            : <Timeline song={song} kind={kind} />}
        </section>
        {song.versions?.length > 0 && <section className="space-y-2">
          <h3 className="text-xs font-semibold uppercase tracking-wider text-ink-secondary">Versiones de video</h3>
          <ul className="divide-y divide-white/[0.06] rounded-xl bg-black/20 ring-1 ring-white/[0.06]">
            {[...song.versions].reverse().map((version, index) => <li key={version.job_id} className="flex items-center justify-between gap-3 px-3 py-2.5 text-sm">
              <div className="min-w-0">
                <p className="font-medium">v{song.versions.length - index}{version.is_variant ? " · variante" : ""}{version.job_id === song.current_job_id ? <span className="ml-1 text-xs text-brand-light">actual</span> : null}</p>
                <p className="text-xs text-ink-secondary">{VERSION_STATUS[version.status] || version.status} · {relativeDate(version.created_at)}{version.portals?.length ? ` · en ${version.portals.map(portalLabel).join(" y ")}` : ""}</p>
              </div>
              <Button size="sm" variant="ghost" onClick={() => onNavigate(`/videos/${encodeURIComponent(version.job_id)}`)}>Abrir</Button>
            </li>)}
          </ul>
        </section>}
        <section className="space-y-2">
          <div className="flex items-center justify-between">
            <h3 className="text-xs font-semibold uppercase tracking-wider text-ink-secondary">Datos</h3>
            {canManage && !editing && <Button size="sm" variant="ghost" onClick={() => setEditing(true)}>Editar</Button>}
          </div>
          {editing && canManage ? <MetadataForm campaignId={campaignId} song={song} onSaved={onChanged} />
            : <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-1.5 text-sm">
              <dt className="text-ink-secondary">Archivo</dt><dd className="truncate" title={song.filename}>{song.filename}</dd>
              <dt className="text-ink-secondary">Duración</dt><dd className="tabular-nums">{formatDuration(song.duration_seconds)}</dd>
              <dt className="text-ink-secondary">Código</dt><dd className="font-mono">{song.technical_code || "—"}</dd>
              {song.uploaded_at && <><dt className="text-ink-secondary">Subida</dt><dd>{relativeDate(song.uploaded_at)}</dd></>}
              {song.approved_at && <><dt className="text-ink-secondary">Aprobada</dt><dd>{relativeDate(song.approved_at)}{song.approved_by_name ? ` · ${song.approved_by_name}` : ""}</dd></>}
            </dl>}
        </section>
      </div>
    </aside>
  </div>, document.body);
}
