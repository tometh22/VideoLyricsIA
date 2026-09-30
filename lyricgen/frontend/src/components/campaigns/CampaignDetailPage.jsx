import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";
import useCampaignResource, { invalidateCampaignResources, mutateCampaignResource } from "../../hooks/useCampaignResource";
import { campaignPost, campaignRequest, loadReviewQueue } from "../../lib/campaignApi";
import {
  FILES_DESTINATION, campaignNextStep, doneCount, relativeDate, resolveView, stageMeta,
} from "../../lib/campaignPipeline";
import CampaignDeliveryProgress from "../CampaignDeliveryProgress";
import CampaignSearch from "../CampaignSearch";
import BulkActionBar from "./BulkActionBar";
import CampaignSettings from "./CampaignSettings";
import { RecordDeliveryDialog, SendToPortalDialog } from "./DeliveryDialogs";
import DiscardDialog from "./DiscardDialog";
import GenerateDialog, { generationSummary } from "./GenerateDialog";
import PipelineBar from "./PipelineBar";
import SongDrawer from "./SongDrawer";
import SongTable from "./SongTable";
import StyleAssignment from "./StyleAssignment";
import VideoReviewPlayer from "./VideoReviewPlayer";
import {
  CLASSIFICATIONS, PORTAL_FILTERS, buildSongs, canDiscard, canGenerate, canSend, classificationCounts, filterSongs, nextLyricSong, primaryAction, sortSongs,
} from "./songModel";
import { Banner, Button, Chip, EmptyState, InfoTip, Kbd, Modal, Skeleton, inputClass } from "./ui";

const STATUS_LABEL = { active: "Activa", paused: "Pausada", completed: "Completada", cancelled: "Cancelada" };
const FILTER_KEYS = ["q", "drafts", "mine", "version", "cls", "portal"];
const CONTEXT_DROP = ["song", "delivery_op", "approved", "focus", "scroll"];
const EMPTY = {
  all: ["Todavía no hay canciones", "Subí los audios de la campaña para empezar."],
  audio: ["No hay audios en proceso", "Las canciones pasan solas a Letra cuando termina la transcripción."],
  lyrics: ["No quedan letras por revisar", "Cuando se transcriba una canción nueva, aparece acá."],
  ready: ["No hay canciones listas para generar", "Aprobá letras para poder generar sus videos."],
  rendering: ["No hay videos generándose", ""],
  qc: ["No hay videos por revisar", "Los videos generados aparecen acá para su control final."],
  approved: ["No hay aprobadas sin enviar", "Todo lo aprobado ya está en el portal."],
  delivered: ["Todavía no hay entregas", "Las canciones enviadas al portal del cliente aparecen acá."],
  attention: ["Nada requiere atención", ""],
  discarded: ["No hay canciones descartadas", ""],
};

function isTyping(target) {
  return Boolean(target?.closest?.("input, textarea, select, [contenteditable=true]"));
}

function SongToolbar({ view, params, setParam, songs, lyricSongs, searchRef, onHelp }) {
  const q = params.get("q") || "";
  const counts = classificationCounts(lyricSongs);
  const drafts = lyricSongs.filter((song) => song.review?.is_draft).length;
  const toggle = (key) => setParam(key, params.get(key) ? "" : "1");
  const chip = (active) => `inline-flex h-8 items-center gap-1.5 rounded-full px-3 text-xs font-medium ring-1 transition ${active ? "bg-brand/20 text-white ring-brand/50" : "bg-white/[0.03] text-ink-secondary ring-white/10 hover:text-white"}`;
  return <div className="flex flex-col gap-3 border-b border-white/[0.06] p-4 lg:flex-row lg:items-center">
    <div className="flex min-w-0 flex-1 flex-wrap items-center gap-2">
      <div className="w-full sm:w-80" ref={searchRef}><CampaignSearch value={q} placeholder="Buscar canción, artista o código" onChange={(value) => setParam("q", value)} /></div>
      {view === "lyrics" && <>
        <button type="button" aria-pressed={!!params.get("drafts")} onClick={() => toggle("drafts")} className={chip(!!params.get("drafts"))}>Con cambios guardados <span className="tabular-nums opacity-70">{drafts}</span></button>
        <button type="button" aria-pressed={!!params.get("mine")} onClick={() => toggle("mine")} className={chip(!!params.get("mine"))}>Mis revisiones</button>
        {CLASSIFICATIONS.map((item) => <button key={item.key} type="button" aria-pressed={params.get("cls") === item.key}
          onClick={() => setParam("cls", params.get("cls") === item.key ? "" : item.key)} className={chip(params.get("cls") === item.key)}>{item.label} <span className="tabular-nums opacity-70">{counts[item.key]}</span></button>)}
        <select aria-label="Versión" value={params.get("version") || ""} onChange={(event) => setParam("version", event.target.value)} className={`${inputClass} !h-8 !w-auto !py-0 text-xs`}>
          <option value="">Estudio y vivo</option><option value="studio">Estudio</option><option value="live">Vivo</option>
        </select>
        <select aria-label="Orden" value={params.get("order") === "learning" ? "learning" : "effort"} onChange={(event) => setParam("order", event.target.value === "learning" ? "learning" : "")} className={`${inputClass} !h-8 !w-auto !py-0 text-xs`}>
          <option value="effort">Menos alertas primero</option><option value="learning">Aprendizaje (20 %)</option>
        </select>
        <InfoTip label="Cómo leer las alertas">Las alertas orientan dónde escuchar primero; no son un porcentaje de acierto ni una confianza calibrada. Toda canción requiere escuchar y confirmar letra y tiempos. La falta de referencia externa no significa por sí sola que la letra esté mal.</InfoTip>
      </>}
      {["all", "qc", "approved", "delivered"].includes(view) && <select aria-label="Filtrar por envío al portal" value={params.get("portal") || ""} onChange={(event) => setParam("portal", event.target.value)} className={`${inputClass} !h-8 !w-auto !py-0 text-xs`}>
        {PORTAL_FILTERS.map((item) => <option key={item.key} value={item.key}>{item.label}</option>)}
      </select>}
    </div>
    <div className="flex items-center gap-3 text-xs text-ink-secondary">
      <span className="tabular-nums" aria-live="polite">{songs.length} {songs.length === 1 ? "canción" : "canciones"}</span>
      <Button size="sm" variant="ghost" onClick={onHelp} aria-label="Atajos de teclado" className="hidden md:inline-flex"><Kbd>?</Kbd> Atajos</Button>
    </div>
  </div>;
}

function ShortcutHelp({ onClose }) {
  const rows = [["J / ↓", "Canción siguiente"], ["K / ↑", "Canción anterior"], ["Enter", "Acción principal de la canción"], ["X", "Seleccionar"], ["/", "Buscar"], ["N", "Próximo paso de la campaña"], ["Esc", "Limpiar selección / cerrar"], ["A", "Aprobar y siguiente (en el reproductor)"], ["E", "Corregir letra (en el reproductor)"]];
  return <Modal label="Atajos de teclado" onClose={onClose} width="max-w-md">
    <h2 className="mb-4 text-lg font-semibold">Atajos de teclado</h2>
    <dl className="grid grid-cols-[auto_1fr] gap-x-6 gap-y-2 text-sm">{rows.map(([keys, label]) => <div key={keys} className="contents"><dt className="font-mono text-xs text-brand-light">{keys}</dt><dd className="text-ink-secondary">{label}</dd></div>)}</dl>
    <div className="mt-5 flex justify-end"><Button onClick={onClose}>Listo</Button></div>
  </Modal>;
}

export default function CampaignDetailPage({ id }) {
  const navigate = useNavigate();
  const location = useLocation();
  const [params, setParams] = useSearchParams();
  const base = `/batch/campaigns/${encodeURIComponent(id)}`;
  const prefix = `campaign:${id}:`;

  const head = useCampaignResource(`${prefix}head`, ({ signal }) => campaignRequest(base, { signal }));
  const pipe = useCampaignResource(`${prefix}pipeline`, ({ signal }) => campaignRequest(`${base}/pipeline`, { signal }));
  const campaign = head.data;
  const kind = campaign?.kind || pipe.data?.kind || "lyric_video";
  const known = Boolean(campaign || pipe.data);
  const lyric = kind !== "art_track";
  const view = resolveView(params, kind);
  // `?view=contract` predates the settings tab; it still lands on the report.
  const section = params.get("section") || (params.get("view") === "contract" ? "contract" : "general");
  const order = params.get("order") === "learning" ? "learning" : "effort";
  const [styleTargets, setStyleTargets] = useState(null);
  const needs = (views, settingsSections = []) => known && lyric && (views.includes(view) || (view === "config" && settingsSections.includes(section)));
  const lyrics = useCampaignResource(`${prefix}lyrics:${order}`, ({ signal }) => loadReviewQueue(id, { order, signal }), { enabled: needs(["all", "lyrics", "discarded"], ["activity"]) });
  const creative = useCampaignResource(`${prefix}creative`, ({ signal }) => campaignRequest(`${base}/creative`, { signal }), { enabled: needs(["all", "ready"], ["style", "contract"]) || Boolean(styleTargets) });
  const report = useCampaignResource(`${prefix}report`, ({ signal }) => campaignRequest(`${base}/creative/report`, { signal }), { enabled: needs(["all", "qc", "approved", "delivered"], ["contract"]) });

  const canManage = Boolean(pipe.data?.can_manage);
  // Art-track campaigns delivered as files have no portal to send to.
  const portalSends = campaign?.destination_portal !== FILES_DESTINATION;
  const counts = pipe.data?.counts || {};
  const songs = useMemo(() => buildSongs(pipe.data?.items || [], {
    reviewRows: lyrics.data?.items, creativeItems: creative.data?.items, videos: report.data?.videos,
  }), [pipe.data, lyrics.data, creative.data, report.data]);
  const filters = Object.fromEntries(FILTER_KEYS.map((key) => [key, params.get(key) || ""]));
  const visible = useMemo(() => sortSongs(filterSongs(songs, {
    view, q: filters.q, drafts: !!filters.drafts, mine: !!filters.mine, version: filters.version, classification: filters.cls, portal: filters.portal,
  }), view), [songs, view, filters.q, filters.drafts, filters.mine, filters.version, filters.cls, filters.portal]);
  const lyricSongs = useMemo(() => songs.filter((song) => song.stage === "lyrics"), [songs]);

  const [selected, setSelected] = useState(() => new Set());
  // The keyboard cursor follows a song, not a row index: a background
  // refresh that reorders the list must never retarget Enter or X.
  const [cursorId, setCursorId] = useState(null);
  const deliveryKeys = useRef(new Map());
  const cursor = cursorId ? visible.findIndex((song) => song.id === cursorId) : -1;
  const [focusRequest, setFocusRequest] = useState(null);
  const [dialog, setDialog] = useState(null);
  const [flash, setFlash] = useState(null);
  const [help, setHelp] = useState(false);
  const [busyIds, setBusyIds] = useState(() => new Set());
  const searchRef = useRef(null);
  const drawerId = params.get("song");
  const drawerSong = drawerId ? songs.find((song) => song.id === drawerId) : null;
  const focusId = params.get("focus");
  const approvedJob = params.get("approved");

  useEffect(() => { try { localStorage.setItem("genly:last-campaign", id); } catch { /* best effort */ } }, [id]);

  const refresh = useCallback(() => invalidateCampaignResources(prefix), [prefix]);
  const updateParams = useCallback((patch, { push = false } = {}) => {
    setParams((previous) => {
      const next = new URLSearchParams(previous);
      Object.entries(patch).forEach(([key, value]) => { if (value == null || value === "") next.delete(key); else next.set(key, String(value)); });
      return next;
    }, { replace: !push });
  }, [setParams]);
  const setParam = useCallback((key, value) => updateParams({ [key]: value, focus: null }), [updateParams]);
  const setView = useCallback((next) => {
    setSelected(new Set());
    setCursorId(null);
    const patch = { view: next === "all" ? null : next, tab: null, stage: null, song: null, focus: null, scroll: null };
    if (next !== "lyrics") Object.assign(patch, { drafts: null, mine: null, cls: null, version: null, order: null });
    if (!["all", "qc", "approved", "delivered"].includes(next)) patch.portal = null;
    updateParams(patch, { push: true });
  }, [updateParams]);

  // Coming back from the editor: bring the song we left into view once.
  const scrolledFor = useRef(null);
  useEffect(() => {
    if (!focusId || !songs.length || scrolledFor.current === focusId) return undefined;
    scrolledFor.current = focusId;
    const frame = requestAnimationFrame(() => {
      document.querySelector(`[data-review-job="${CSS.escape(focusId)}"], [data-song="${CSS.escape(focusId)}"]`)?.scrollIntoView?.({ block: "center" });
    });
    const timer = setTimeout(() => updateParams({ focus: null, scroll: null }), 6000);
    return () => { cancelAnimationFrame(frame); clearTimeout(timer); };
  }, [focusId, songs.length, updateParams]);

  // Back from approving in the editor: the cached snapshot predates it.
  useEffect(() => { if (approvedJob) void invalidateCampaignResources(prefix); }, [approvedJob, prefix]);

  // The editor's "Descartar canción" returns here with ?discard=<item>.
  const discardId = params.get("discard");
  useEffect(() => {
    if (!discardId || !pipe.data || (lyric && !lyrics.data && !lyrics.error)) return;
    const target = songs.find((song) => song.id === discardId);
    if (target && canDiscard(target)) setDialog({ type: "discard", songs: [target], mode: "discard" });
    else setFlash({ tone: "warning", text: "La canción cambió de estado y no se puede descartar desde esta revisión." });
    updateParams({ discard: null });
  }, [discardId, lyric, lyrics.data, lyrics.error, pipe.data, songs, updateParams]);

  const returnPath = useCallback((song) => {
    const context = new URLSearchParams(location.search);
    CONTEXT_DROP.forEach((key) => context.delete(key));
    if (song?.job_id) context.set("focus", song.job_id);
    const query = context.toString();
    return `/campaigns/${encodeURIComponent(id)}${query ? `?${query}` : ""}`;
  }, [id, location.search]);
  const openReview = useCallback((song) => {
    if (!song?.job_id) return;
    navigate(`/review/${encodeURIComponent(song.job_id)}?return_to=${encodeURIComponent(returnPath(song))}`);
  }, [navigate, returnPath]);
  const openEditLyrics = useCallback((song) => {
    const videoStage = ["qc", "approved", "delivered"].includes(song.stage) && song.current_job_id;
    navigate(videoStage
      ? `/videos/${encodeURIComponent(song.current_job_id)}/edit-lyrics?return_to=${encodeURIComponent(returnPath(song))}`
      : `/review/${encodeURIComponent(song.job_id)}?return_to=${encodeURIComponent(returnPath(song))}`);
  }, [navigate, returnPath]);

  const videosFor = (list) => list.map((song) => ({ item_id: song.id, job_id: song.current_job_id, title: song.title, artist: song.artist }));
  const runRetry = async (list) => {
    setBusyIds(new Set(list.map((song) => song.id)));
    const failed = [];
    for (const song of list) {
      try { await campaignPost(`${base}/items/${encodeURIComponent(song.id)}/retry`); } catch (error) { failed.push(`${song.title}: ${error.message}`); }
    }
    setBusyIds(new Set());
    setFlash(failed.length ? { tone: "danger", text: `No se pudieron reintentar ${failed.length}: ${failed.join("; ")}` } : { tone: "success", text: `${list.length} ${list.length === 1 ? "canción reintentada" : "canciones reintentadas"}.` });
    await refresh();
  };

  const onAction = useCallback((song, key) => {
    switch (key) {
      case "review": openReview(song); break;
      case "edit-lyrics": openEditLyrics(song); break;
      case "play": {
        const queue = song.stage === "qc" ? visible.filter((item) => item.stage === "qc" && (item.has_video || item.video?.video_url)) : [song];
        setDialog({ type: "player", queue: queue.some((item) => item.id === song.id) ? queue : [song], startId: song.id });
        break;
      }
      case "generate":
        if (!song.creative) { setFlash({ tone: "warning", text: "Cargando el estilo de la canción; probá de nuevo en un instante." }); break; }
        if (!canGenerate(song)) { setFlash({ tone: "warning", text: "Esta canción ya no está lista para generar." }); break; }
        setDialog({ type: "generate", items: [song.creative] });
        break;
      case "send": setDialog({ type: "send", songs: [song] }); break;
      case "record-delivery": setDialog({ type: "record", song }); break;
      case "retry": void runRetry([song]); break;
      case "discard": setDialog({ type: "discard", songs: [song], mode: "discard" }); break;
      case "restore": setDialog({ type: "discard", songs: [song], mode: "restore" }); break;
      case "metadata":
      case "detail": updateParams({ song: song.id }, { push: true }); break;
      default: break;
    }
  }, [openEditLyrics, openReview, updateParams, visible]);

  const next = campaignNextStep(counts, kind, { portalSends });
  const runNextStep = useCallback(() => {
    if (!next || next.passive) return;
    if (next.stage === "lyrics") {
      const pool = view === "lyrics" ? visible : sortSongs(lyricSongs, "lyrics");
      const song = nextLyricSong(pool);
      if (song) openReview(song); else setView("lyrics");
      return;
    }
    if (next.stage === "qc") {
      const pool = (view === "qc" ? visible : sortSongs(songs.filter((song) => song.stage === "qc"), "qc")).filter((song) => song.has_video || song.video?.video_url);
      if (pool.length) setDialog({ type: "player", queue: pool, startId: pool[0].id }); else setView("qc");
      return;
    }
    if (next.stage === "ready") {
      const items = songs.filter(canGenerate).map((song) => song.creative);
      if (items.length) setDialog({ type: "generate", items }); else setView("ready");
      return;
    }
    if (next.stage === "approved" && canManage && portalSends) {
      const list = songs.filter((song) => song.stage === "approved" && canSend(song));
      if (list.length) setDialog({ type: "send", songs: list }); else setView("approved");
      return;
    }
    setView(next.stage);
  }, [canManage, lyricSongs, next, openReview, setView, songs, view, visible]);

  // Keyboard: J/K move, Enter acts, X selects, / searches, N next step.
  useEffect(() => {
    const onKey = (event) => {
      if (event.metaKey || event.ctrlKey || event.altKey || dialog || drawerSong || help || view === "config") return;
      if (document.querySelector("[role=dialog]")) return;
      const typing = isTyping(event.target);
      if (event.key === "Escape" && selected.size && !typing) { setSelected(new Set()); return; }
      if (typing) return;
      const key = event.key.toLowerCase();
      if (key === "/") { event.preventDefault(); searchRef.current?.querySelector("input")?.focus(); return; }
      if (key === "?") { event.preventDefault(); setHelp(true); return; }
      if (key === "n") { event.preventDefault(); runNextStep(); return; }
      if (!visible.length) return;
      if (key === "j" || key === "arrowdown") {
        event.preventDefault();
        const index = cursor < 0 ? 0 : Math.min(visible.length - 1, cursor + 1);
        setCursorId(visible[index].id); setFocusRequest({ index, at: Date.now() });
      } else if (key === "k" || key === "arrowup") {
        event.preventDefault();
        const index = cursor < 0 ? 0 : Math.max(0, cursor - 1);
        setCursorId(visible[index].id); setFocusRequest({ index, at: Date.now() });
      } else if (key === "x" && cursor >= 0) {
        event.preventDefault();
        const songId = visible[cursor].id;
        setSelected((old) => { const copy = new Set(old); if (copy.has(songId)) copy.delete(songId); else copy.add(songId); return copy; });
      } else if (key === "enter" && cursor >= 0 && event.target?.tagName === "TR" && event.target.dataset.song === visible[cursor].id) {
        event.preventDefault();
        const song = visible[cursor];
        const chosen = primaryAction(song, { kind, canManage, portalSends });
        onAction(song, chosen ? chosen.key : "detail");
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [canManage, cursor, dialog, drawerSong, help, kind, onAction, portalSends, runNextStep, selected.size, view, visible]);

  const selectedVisible = visible.filter((song) => selected.has(song.id));
  const hiddenSelected = selected.size - selectedVisible.length;
  const onBulk = (action, list) => {
    if (action === "generate") setDialog({ type: "generate", items: list.map((song) => song.creative) });
    else if (action === "send") setDialog({ type: "send", songs: list });
    else if (action === "discard" || action === "restore") setDialog({ type: "discard", songs: list, mode: action });
    else if (action === "retry") void runRetry(list);
    else if (action === "style") setStyleTargets(list.map((song) => song.id));
  };

  const approvedSong = approvedJob ? songs.find((song) => song.job_id === approvedJob) : null;
  // Back from the editor, the song may have moved on (approved, discarded…).
  const movedSong = focusId && !approvedSong && visible.length && !visible.some((song) => song.job_id === focusId || song.id === focusId)
    ? songs.find((song) => song.job_id === focusId || song.id === focusId) : null;
  // The cached snapshot may still list the song just approved as "Letra"
  // until the refresh lands; never offer it again as the next one.
  const nextAfterApproval = approvedSong ? nextLyricSong(sortSongs(lyricSongs.filter((song) => song.job_id !== approvedJob), "lyrics")) : null;

  if (!known && (head.error || pipe.error)) {
    return <div className="mx-auto max-w-3xl space-y-4 py-10">
      <button type="button" className="text-sm text-ink-secondary hover:text-white" onClick={() => navigate("/campaigns")}>← Campañas</button>
      <Banner tone="danger" action={<Button size="sm" onClick={refresh}>Reintentar</Button>}>{(head.error || pipe.error).message || "No se pudo cargar la campaña."}</Banner>
    </div>;
  }

  const active = pipe.data?.active_total ?? 0;
  const finished = doneCount(counts);
  const percent = active ? Math.round((100 * finished) / active) : 0;
  const emptyCopy = EMPTY[view] || EMPTY.all;
  const filtered = FILTER_KEYS.some((key) => params.get(key));

  return <div className="mx-auto max-w-[1500px] space-y-5 pb-28">
    <button type="button" onClick={() => navigate("/campaigns")} className="text-sm text-ink-secondary transition hover:text-white">← Campañas</button>

    <header className="flex flex-col gap-5 lg:flex-row lg:items-end lg:justify-between">
      <div className="min-w-0 space-y-2">
        <div className="flex flex-wrap items-center gap-2">
          <Chip tone={campaign?.status === "active" ? "success" : campaign?.status === "cancelled" ? "danger" : "neutral"}>{STATUS_LABEL[campaign?.status] || "…"}</Chip>
          <Chip>{lyric ? "Lyric videos" : "Art tracks"}</Chip>
          {pipe.refreshing && <span className="text-xs text-ink-secondary" role="status">Actualizando…</span>}
        </div>
        {campaign ? <h1 className="truncate text-2xl font-bold tracking-tight text-white sm:text-3xl">{campaign.name}</h1> : <Skeleton className="h-9 w-80" />}
        <p className="text-sm text-ink-secondary">
          {pipe.data ? <><strong className="text-white tabular-nums">{finished}</strong> de <span className="tabular-nums">{active}</span> {active === 1 ? "canción terminada" : "canciones terminadas"} · {percent}%
            {counts.delivered ? <> · <span className="tabular-nums">{counts.delivered}</span> entregadas</> : null}
            {pipe.updatedAt ? <> · actualizado {relativeDate(pipe.updatedAt)}</> : null}</> : <Skeleton className="inline-block h-4 w-64 align-middle" />}
          <button type="button" onClick={refresh} className="ml-2 rounded-md px-1.5 text-ink-secondary hover:bg-white/[0.06] hover:text-white" aria-label="Actualizar ahora" title="Actualizar ahora">↻</button>
        </p>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        {canManage && lyric && <Button variant="ghost" onClick={() => updateParams({ view: "config", section: "upload", song: null }, { push: true })}>Subir audios</Button>}
        <Button variant={view === "config" ? "secondary" : "ghost"} onClick={() => (view === "config" ? setView("all") : updateParams({ view: "config", song: null }, { push: true }))} aria-pressed={view === "config"}>Configuración</Button>
        {next && !next.passive && <Button variant="primary" size="lg" onClick={runNextStep}>{next.label} <Kbd className="hidden sm:inline-flex">N</Kbd></Button>}
      </div>
    </header>

    <PipelineBar counts={counts} kind={kind} value={view === "config" ? null : view} onChange={setView} loading={!pipe.data} />

    {approvedSong && <Banner tone="success" action={<div className="flex gap-2">
      {nextAfterApproval && <Button size="sm" variant="primary" onClick={() => openReview(nextAfterApproval)}>Revisar siguiente: {nextAfterApproval.title}</Button>}
      <Button size="sm" variant="ghost" onClick={() => updateParams({ approved: null })}>Cerrar</Button></div>}>
      Letra aprobada: <strong>{approvedSong.title}</strong>. Quedó lista para generar.
    </Banner>}
    {movedSong && view !== "config" && <Banner tone="info" action={<Button size="sm" onClick={() => updateParams({ view: movedSong.stage, focus: movedSong.job_id || movedSong.id, tab: null, stage: null, ...Object.fromEntries(FILTER_KEYS.map((key) => [key, null])) }, { push: true })}>Ver en {stageMeta(movedSong.stage).label}</Button>}>
      <strong>{movedSong.title}</strong> ahora está en «{stageMeta(movedSong.stage).title}».
    </Banner>}
    {params.get("delivery_op") && <CampaignDeliveryProgress operationId={params.get("delivery_op")} request={campaignRequest} onSettled={refresh}
      describe={(jobId) => { const song = songs.find((item) => item.current_job_id === jobId || item.job_id === jobId || item.versions?.some((version) => version.job_id === jobId)); return song ? `${song.title} · ${song.artist}` : ""; }}
      onSelectFailed={(jobIds) => {
        const ids = songs.filter((song) => jobIds.includes(song.current_job_id)).map((song) => song.id);
        setView("approved"); setSelected(new Set(ids));
      }} />}
    {pipe.data && pipe.data.portal_status_available === false && <Banner tone="warning">No pudimos consultar el portal del cliente: por ahora las entregas figuran como aprobadas.</Banner>}
    {pipe.error && known && <Banner tone="danger" action={<Button size="sm" onClick={refresh}>Reintentar</Button>}>No se pudo actualizar el estado: {pipe.error.message}</Banner>}
    {flash && <Banner tone={flash.tone} role={flash.tone === "danger" ? "alert" : "status"} action={<Button size="sm" variant="ghost" onClick={() => setFlash(null)}>Cerrar</Button>}>{flash.text}</Banner>}

    {view === "config" && campaign
      ? <CampaignSettings campaign={campaign} section={section} onSection={(value) => updateParams({ section: value })} canManage={canManage} isAdmin={canManage}
        creative={creative.data} report={report.data} lyrics={lyrics.data} onChanged={refresh} remaining={Math.max(0, 1000 - (pipe.data?.total || 0))}
        onUploaded={(summary) => { void refresh(); if (summary?.uploaded) setFlash({ tone: "success", text: `${summary.uploaded} audios subidos. La transcripción arranca sola.` }); }} />
      : <section className="overflow-hidden rounded-card bg-surface-2/40 ring-1 ring-white/[0.06]">
        {view !== "all" && <div className="flex flex-wrap items-center justify-between gap-2 px-4 pt-4">
          <div><h2 className="text-base font-semibold">{stageMeta(view).title}</h2><p className="text-xs text-ink-secondary">{stageMeta(view).hint}</p></div>
          {view === "lyrics" && lyrics.error && <Banner tone="warning">No se cargaron las alertas: {lyrics.error.message}</Banner>}
        </div>}
        <SongToolbar view={view} params={params} setParam={setParam} songs={visible} lyricSongs={lyricSongs} searchRef={searchRef} onHelp={() => setHelp(true)} />
        <SongTable songs={visible} view={view} kind={kind} canManage={canManage} portalSends={portalSends} loading={!pipe.data}
          selectable={() => true} selected={selected}
          onToggle={(songId) => setSelected((old) => { const copy = new Set(old); if (copy.has(songId)) copy.delete(songId); else copy.add(songId); return copy; })}
          onToggleAll={(ids) => setSelected(new Set(ids))}
          onAction={onAction} onOpen={(song) => updateParams({ song: song.id }, { push: true })}
          highlightedId={focusId || approvedJob} cursor={cursor} onCursor={(index) => setCursorId(visible[index]?.id || null)} focusRequest={focusRequest} busyIds={busyIds}
          emptyState={<EmptyState title={filtered ? "No hay coincidencias con los filtros" : emptyCopy[0]} description={filtered ? "Probá con otra búsqueda o limpiá los filtros." : emptyCopy[1]}
            action={filtered ? <Button size="sm" onClick={() => updateParams(Object.fromEntries(FILTER_KEYS.map((key) => [key, null])))}>Limpiar filtros</Button>
              : view === "all" && canManage && lyric ? <Button size="sm" variant="primary" onClick={() => updateParams({ view: "config", section: "upload" }, { push: true })}>Subir audios</Button> : null} />} />
      </section>}

    {view !== "config" && <BulkActionBar songs={selectedVisible} kind={kind} canManage={canManage} portalSends={portalSends} hiddenCount={hiddenSelected} onClear={() => setSelected(new Set())} onAction={onBulk} />}

    {drawerSong && <SongDrawer song={drawerSong} kind={kind} campaignId={id} canManage={canManage} portalSends={portalSends}
      reviewerEnabled={campaign?.reviewer_campaign_status?.enabled === true}
      onClose={() => updateParams({ song: null })} onAction={(song, key) => { if (!["detail", "metadata"].includes(key)) updateParams({ song: null }); onAction(song, key); }}
      onNavigate={(path) => navigate(path.includes("return_to") ? path : `${path}${path.includes("?") ? "&" : "?"}return_to=${encodeURIComponent(returnPath(drawerSong))}`)}
      onChanged={refresh} />}

    {dialog?.type === "player" && <VideoReviewPlayer queue={dialog.queue} startId={dialog.startId}
      onApproved={(song) => {
        mutateCampaignResource(`${prefix}pipeline`, (data) => ({
          ...data,
          counts: { ...data.counts, qc: Math.max(0, (data.counts.qc || 0) - 1), approved: (data.counts.approved || 0) + 1 },
          items: data.items.map((item) => (item.item_id === song.id ? { ...item, stage: "approved", current_status: "done", approved_at: new Date().toISOString() } : item)),
        }));
      }}
      onEdit={(song) => { setDialog(null); openEditLyrics(song); }}
      onClose={(result) => { setDialog(null); if (result?.finished) setFlash({ tone: "success", text: `${result.lastTitle} quedó aprobado. No quedan videos por revisar en esta lista.` }); void refresh(); }} />}
    {dialog?.type === "generate" && <GenerateDialog items={dialog.items} onClose={() => setDialog(null)} onDone={(result) => {
      setDialog(null);
      const summary = generationSummary(result);
      setSelected((old) => new Set([...old].filter((songId) => !result.sentIds.has(songId))));
      if (result.deferred.length) setSelected(new Set(result.deferred.map((item) => item.id)));
      setFlash({ tone: summary.error ? "danger" : summary.notice ? "warning" : "success", text: [summary.message, summary.notice, summary.error].filter(Boolean).join(" ") });
      void refresh();
    }} />}
    {dialog?.type === "send" && <SendToPortalDialog campaignId={id} kind={kind} videos={videosFor(dialog.songs)} idempotencyKeys={deliveryKeys.current}
      lockedPortal={campaign?.destination_portal || ""} defaultPortal={dialog.songs[0]?.portals?.[0] || ""}
      onClose={() => setDialog(null)} onStarted={(operation, portal, total) => {
        setDialog(null); setSelected(new Set());
        updateParams({ delivery_op: operation.operation_id }, { push: true });
        setFlash({ tone: "success", text: `Envío iniciado para ${operation.total_count || total} ${total === 1 ? "video" : "videos"} a ${portal === "chile" ? "Chile" : portal === "argentina" ? "Argentina" : portal}. Sigue en segundo plano.` });
      }} />}
    {dialog?.type === "record" && <RecordDeliveryDialog campaignId={id} song={dialog.song} onClose={() => setDialog(null)} onDone={() => { setDialog(null); setFlash({ tone: "success", text: "Entrega registrada." }); void refresh(); }} />}
    {dialog?.type === "discard" && <DiscardDialog campaignId={id} songs={dialog.songs} mode={dialog.mode} onClose={() => setDialog(null)} onDone={(result) => {
      if (!result.keepOpen) setDialog(null);
      setSelected(new Set());
      setFlash({ tone: result.failed.length ? "warning" : "success", text: `${result.done} ${result.done === 1 ? "canción" : "canciones"} ${dialog.mode === "restore" ? "recuperada" : "descartada"}${result.done === 1 ? "" : "s"}.` });
      void refresh();
    }} />}
    {styleTargets && <Modal label="Asignar estilo" onClose={() => setStyleTargets(null)} width="max-w-4xl">
      <div className="mb-5 flex items-start justify-between gap-3">
        <div><p className="text-[11px] font-semibold uppercase tracking-[.18em] text-brand-light">Estilo y reparto</p>
          <h2 className="mt-1 text-xl font-semibold">Asignar estilo a {styleTargets.length} {styleTargets.length === 1 ? "canción" : "canciones"}</h2>
          <p className="mt-1 text-sm text-ink-secondary">Sólo cambia cómo se generarán estos videos. No genera nada.</p></div>
        <Button variant="ghost" size="sm" onClick={() => setStyleTargets(null)} aria-label="Cerrar">✕</Button>
      </div>
      {creative.data ? creative.data.can_manage
        ? <StyleAssignment campaignId={id} creative={creative.data} itemIds={styleTargets} onSaved={async () => { await refresh(); }} />
        : <p className="text-sm text-ink-secondary">Sólo quien creó la campaña o un administrador puede cambiar estilos.</p>
        : <Skeleton className="h-48" />}
    </Modal>}
    {help && <ShortcutHelp onClose={() => setHelp(false)} />}
  </div>;
}
