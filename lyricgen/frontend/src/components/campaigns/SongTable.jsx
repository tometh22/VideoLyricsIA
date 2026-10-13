import { useEffect, useMemo, useRef, useState } from "react";
import { useLazyMediaUrl } from "../../mediaUrl";
import { displayCode, formatDuration, portalLabel, stageMeta } from "../../lib/campaignPipeline";
import { isApprovedStage, primaryAction, statusNote } from "./songModel";
import { Button, Chip, EmptyState, Skeleton, StageBadge } from "./ui";

const PAGE = 60;
const VIDEO_STAGES = new Set(["qc", "approved", "delivered"]);

function Thumbnail({ song }) {
  const jobId = VIDEO_STAGES.has(song.stage) && song.has_video ? song.current_job_id : null;
  const { ref, url } = useLazyMediaUrl(jobId, "thumbnail", "preview", { version: song.video?.evidence?.video_sha256 || song.approved_at || "" });
  const [failed, setFailed] = useState(false);
  return <div ref={ref} className="relative hidden h-10 w-[4.5rem] shrink-0 overflow-hidden rounded-lg bg-gradient-to-br from-brand/25 via-surface-3 to-cyan-500/10 ring-1 ring-white/10 sm:block">
    {url && !failed
      ? <img src={url} alt="" loading="lazy" onError={() => setFailed(true)} className="h-full w-full object-cover" />
      : <span aria-hidden="true" className="grid h-full place-items-center text-sm text-white/40">♪</span>}
  </div>;
}

function StageDetail({ song }) {
  const chips = [];
  if (song.stage === "lyrics" && song.review) {
    const priority = song.review.review_priority;
    const windows = song.review.timing_evidence?.length || 0;
    if (priority === "manual_full") chips.push(<Chip key="p" tone="danger">Revisión completa</Chip>);
    else if (windows) chips.push(<Chip key="p" tone="warning">{windows} {windows === 1 ? "fragmento" : "fragmentos"} a comprobar</Chip>);
    else chips.push(<Chip key="p">Sin alertas concretas</Chip>);
    if (song.review.reference && !song.review.reference.available) chips.push(<Chip key="r" title="No hay referencia válida para comparar la letra">Sin referencia</Chip>);
    if (song.review.version === "live") chips.push(<Chip key="v" tone="info">Vivo</Chip>);
  }
  if (song.stage === "ready" && song.creative) {
    chips.push(<Chip key="g" tone="info">{song.creative.assignment?.group_name || "Estilo base"}</Chip>);
  }
  if (song.portals?.length) chips.push(<Chip key="portal" tone="brand">En {song.portals.map(portalLabel).join(" y ")}</Chip>);
  if (song.portal_outdated && !isApprovedStage(song)) chips.push(<Chip key="old" tone="warning" title="Hay un corte nuevo, pero todavía falta aprobarlo para poder enviarlo al portal.">Falta aprobar el corte nuevo</Chip>);
  else if (song.portal_outdated && song.portal_serves_latest) chips.push(<Chip key="old" tone="warning" title="Se volvió a renderizar después de la última publicación. El cliente ya descarga el archivo nuevo, pero falta registrar la versión y reiniciar su aprobación.">Corte nuevo sin registrar</Chip>);
  else if (song.portal_outdated) chips.push(<Chip key="old" tone="success" title={song.portal_hidden ? "Aprobado y listo: el cliente no ve el video hasta que lo publiques. Enviá la canción para publicarlo." : "Aprobado y listo: el portal todavía muestra el corte anterior. Enviá la canción para actualizarlo."}>Listo para reenviar</Chip>);
  if (song.portal_hidden && !song.portal_outdated) chips.push(<Chip key="hidden" tone="warning" title="El cliente no ve este video ahora: está oculto (a mano o mientras tiene cambios sin publicar). Se cambia en Admin > Cambios UMG > Qué ve el cliente.">Oculto para el cliente</Chip>);
  if (song.published_other_version) chips.push(<Chip key="other" tone="info">Otra versión publicada</Chip>);
  if (song.pending_change_requests) chips.push(<Chip key="cr" tone="warning">{song.pending_change_requests} {song.pending_change_requests === 1 ? "cambio pedido" : "cambios pedidos"}</Chip>);
  if (song.current_is_variant) chips.push(<Chip key="var">Variante</Chip>);
  if (song.video_count > 1) chips.push(<Chip key="vc">{song.video_count} versiones</Chip>);
  return chips.length ? <div className="flex flex-wrap gap-1.5">{chips}</div> : <span className="hidden text-xs text-ink-secondary/60 lg:inline">—</span>;
}

export default function SongTable({
  songs, view, kind, canManage, loading, selectable, selected, onToggle, onToggleAll,
  onAction, onOpen, highlightedId, cursor, onCursor, focusRequest, emptyState, busyIds, portalSends = true,
}) {
  const [limit, setLimit] = useState(PAGE);
  const sentinel = useRef(null);
  const rowRefs = useRef(new Map());

  useEffect(() => { setLimit(PAGE); }, [view]);
  // Returning from the editor: make sure the song we came from is rendered.
  useEffect(() => {
    if (!highlightedId) return;
    const index = songs.findIndex((song) => song.id === highlightedId || song.job_id === highlightedId || song.current_job_id === highlightedId);
    if (index >= 0 && index >= limit) setLimit(index + 20);
  }, [highlightedId, songs, limit]);
  // Keyboard navigation asks for focus explicitly; background refreshes of
  // the list must never steal focus from the search box.
  const pendingFocus = useRef(null);
  useEffect(() => {
    if (!focusRequest) return;
    pendingFocus.current = focusRequest.index;
    if (focusRequest.index >= limit) setLimit(focusRequest.index + 20);
  }, [focusRequest]);
  useEffect(() => {
    if (pendingFocus.current == null) return;
    const node = rowRefs.current.get(songs[pendingFocus.current]?.id);
    if (!node) return;
    pendingFocus.current = null;
    node.focus();
    node.scrollIntoView?.({ block: "nearest" });
  });
  useEffect(() => {
    const node = sentinel.current;
    if (!node || typeof IntersectionObserver === "undefined") return undefined;
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) setLimit((value) => value + PAGE);
    }, { rootMargin: "600px" });
    observer.observe(node);
    return () => observer.disconnect();
  }, [songs.length, limit]);

  const visible = useMemo(() => songs.slice(0, limit), [songs, limit]);
  const selectableVisible = selectable ? songs.filter(selectable) : [];
  const allSelected = selectableVisible.length > 0 && selectableVisible.every((song) => selected.has(song.id));
  const showDuration = !["qc", "approved", "delivered"].includes(view);
  const showThumbs = ["all", "qc", "approved", "delivered"].includes(view);

  if (loading) {
    return <div className="divide-y divide-white/[0.05]" aria-busy="true">
      <p role="status" className="sr-only">Cargando canciones…</p>
      {Array.from({ length: 8 }, (_, index) => <div key={index} className="flex items-center gap-4 px-4 py-3.5">
        <Skeleton className="h-4 w-4" /><Skeleton className="h-4 w-1/3" /><Skeleton className="ml-auto h-6 w-28 rounded-full" /><Skeleton className="h-8 w-20" />
      </div>)}
    </div>;
  }
  if (!songs.length) return emptyState || <EmptyState title="No hay canciones en esta etapa" />;

  return <div>
    {selectable && selectableVisible.length > 0 && <label className="flex items-center gap-3 border-b border-white/[0.06] px-4 py-2.5 text-xs text-ink-secondary md:hidden">
      <input type="checkbox" checked={allSelected} onChange={() => onToggleAll(allSelected ? [] : selectableVisible.map((song) => song.id))} className="h-4 w-4 accent-[#7557FF]" />
      {allSelected ? "Quitar selección" : `Seleccionar todas (${selectableVisible.length})`}
    </label>}
    <table className="block w-full text-left text-sm md:table">
      <thead className="hidden border-b border-white/[0.06] text-[11px] uppercase tracking-wider text-ink-secondary md:table-header-group">
        <tr>
          <th className="w-10 py-2.5 pl-4">{selectable && <input type="checkbox" aria-label={allSelected ? "Quitar selección" : `Seleccionar ${selectableVisible.length} canciones`}
            checked={allSelected} disabled={!selectableVisible.length} onChange={() => onToggleAll(allSelected ? [] : selectableVisible.map((song) => song.id))}
            className="h-4 w-4 accent-[#7557FF]" />}</th>
          <th className="py-2.5 pr-3 font-medium">Canción</th>
          <th className="py-2.5 pr-3 font-medium">Etapa</th>
          <th className="hidden py-2.5 pr-3 font-medium lg:table-cell">Detalle</th>
          {showDuration && <th className="hidden py-2.5 pr-3 font-medium xl:table-cell">Duración</th>}
          <th className="py-2.5 pr-4 text-right font-medium"><span className="sr-only">Acciones</span></th>
        </tr>
      </thead>
      <tbody className="block md:table-row-group">
        {visible.map((song, index) => {
          const action = primaryAction(song, { kind, canManage, portalSends });
          const isSelectable = selectable?.(song);
          const highlighted = highlightedId && (song.id === highlightedId || song.job_id === highlightedId || song.current_job_id === highlightedId);
          const note = statusNote(song);
          const busy = busyIds?.has(song.id);
          return <tr key={song.id} ref={(node) => { if (node) rowRefs.current.set(song.id, node); else rowRefs.current.delete(song.id); }}
            tabIndex={cursor === index ? 0 : -1} data-song={song.id} data-review-job={song.job_id || undefined}
            onFocus={() => { if (cursor !== index) onCursor?.(index); }}
            className={`group grid grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-x-3 gap-y-1 border-b border-white/[0.045] px-4 py-3 outline-none transition-colors md:table-row md:px-0 md:py-0 ${highlighted ? "bg-brand/[0.12]" : selected.has(song.id) ? "bg-brand/[0.07]" : "hover:bg-white/[0.025]"} focus-visible:bg-white/[0.04] focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-brand/60`}>
            <td className="row-span-2 md:w-10 md:py-3 md:pl-4">
              {isSelectable ? <input type="checkbox" aria-label={`Seleccionar ${song.title}`} checked={selected.has(song.id)} onChange={() => onToggle(song.id)} className="h-4 w-4 accent-[#7557FF]" />
                : <span className="block w-4 text-center text-[11px] tabular-nums text-ink-secondary/60">{selectable ? "" : song.ordinal}</span>}
            </td>
            <td className="min-w-0 md:py-3 md:pr-3">
              <div className="flex min-w-0 items-center gap-3">
                {showThumbs && <Thumbnail song={song} />}
                <div className="min-w-0">
                  <button type="button" onClick={() => onOpen(song)} className="block max-w-full truncate text-left font-medium text-white hover:underline focus-visible:underline focus-visible:outline-none">{song.title}</button>
                  <p className="truncate text-xs text-ink-secondary">{song.artist || "Artista sin informar"}{displayCode(song.technical_code) ? <span className="text-ink-secondary/60"> · {displayCode(song.technical_code)}</span> : null}</p>
                </div>
              </div>
            </td>
            <td className="col-start-2 row-start-2 md:py-3 md:pr-3">
              <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                <StageBadge stage={song.stage} label={view === song.stage && view !== "all" ? stageMeta(song.stage).label : undefined} />
                {note && <span className="max-w-[16rem] truncate text-xs text-ink-secondary" title={note}>{note}</span>}
              </div>
            </td>
            <td className="col-span-2 col-start-2 row-start-3 md:hidden md:py-3 md:pr-3 lg:table-cell"><StageDetail song={song} /></td>
            {showDuration && <td className="hidden tabular-nums text-ink-secondary md:py-3 md:pr-3 xl:table-cell">{formatDuration(song.duration_seconds)}</td>}
            <td className="col-start-3 row-span-2 row-start-1 md:py-3 md:pr-4">
              <div className="flex items-center justify-end gap-1.5">
                {action && <Button size="sm" variant={["review", "play", "generate", "send"].includes(action.key) ? "soft" : "secondary"}
                  disabled={busy} onClick={() => onAction(song, action.key)} aria-label={`${action.label} · ${song.title}`}>{action.label}</Button>}
                <Button size="sm" variant="ghost" onClick={() => onOpen(song)} aria-label={`Detalle de ${song.title}`} className="!px-2">
                  <span aria-hidden="true">›</span>
                </Button>
              </div>
            </td>
          </tr>;
        })}
      </tbody>
    </table>
    {visible.length < songs.length && <div ref={sentinel} className="flex justify-center p-4">
      <Button size="sm" variant="ghost" onClick={() => setLimit((value) => value + PAGE)}>Mostrar más ({songs.length - visible.length})</Button>
    </div>}
  </div>;
}
