import { matchesCampaignSong } from "../../lib/campaignSearch";
import { reviewActionLabel } from "../../lib/reviewerNavigation";

/**
 * A "song" is the pipeline item (single source of truth for the stage)
 * enriched with whatever the current view needs: the lyric review row, the
 * creative item (style + generation settings) and the current video row.
 */
export function buildSongs(items = [], { reviewRows = [], creativeItems = [], videos = [] } = {}) {
  const reviewByItem = new Map();
  reviewRows.forEach((row, index) => reviewByItem.set(row.item_id, { ...row, queuePosition: index }));
  const creativeByItem = new Map(creativeItems.map((item) => [item.id, item]));
  const videoByJob = new Map(videos.map((video) => [video.job_id, video]));
  return items.map((item) => ({
    ...item,
    id: item.item_id,
    review: reviewByItem.get(item.item_id) || null,
    creative: creativeByItem.get(item.item_id) || null,
    video: videoByJob.get(item.current_job_id) || null,
  }));
}

export function isLockedByOther(song) {
  return Boolean(song.review?.reviewer_lock_active && !song.review?.reviewer_is_current_user);
}

export function canGenerate(song) {
  return song.stage === "ready" && song.creative?.status === "lyrics_approved" && !song.creative?.discarded;
}

export function canSend(song) {
  return ["approved", "delivered"].includes(song.stage) && song.current_status === "done" && Boolean(song.approved_at) && song.has_video
    && song.current_sendable !== false;
}

export function canDiscard(song) {
  if (!song.job_id || isLockedByOther(song)) return false;
  // The review row mirrors the backend's _DISCARDABLE rule exactly.
  if (song.review) return Boolean(song.review.can_discard);
  return song.stage === "lyrics" || (song.stage === "attention" && song.current_job_id === song.job_id);
}

/** What the row's single primary button does, by stage. */
export function primaryAction(song, { kind = "lyric_video", canManage = false, portalSends = true } = {}) {
  switch (song.stage) {
    case "audio":
      if (canManage && song.upload_state === "error") return { key: "retry", label: "Reintentar" };
      if (canManage && song.metadata_error === "missing_metadata") return { key: "metadata", label: "Completar datos" };
      return null;
    case "lyrics": {
      if (!song.job_id || isLockedByOther(song)) return null;
      const label = song.review ? reviewActionLabel(song.review, "lyrics") : "Revisar";
      return label ? { key: "review", label } : null;
    }
    case "ready":
      return kind === "art_track" ? null : { key: "generate", label: "Generar" };
    case "rendering":
      return null;
    case "qc":
      return song.has_video || song.video?.video_url ? { key: "play", label: "Revisar video" } : { key: "detail", label: "Ver detalle" };
    case "approved":
      return canManage && portalSends && canSend(song) ? { key: "send", label: "Enviar" } : { key: "play", label: "Ver video" };
    case "delivered":
      return canManage && portalSends && song.portal_outdated && canSend(song) ? { key: "send", label: "Reenviar" } : { key: "play", label: "Ver video" };
    case "attention":
      return canManage ? { key: "retry", label: "Reintentar" } : { key: "detail", label: "Ver detalle" };
    case "discarded":
      return { key: "restore", label: "Recuperar" };
    default:
      return null;
  }
}

/** Short status line under the stage badge; empty when the badge says it all. */
export function statusNote(song) {
  if (song.stage === "lyrics") {
    if (isLockedByOther(song)) return `En revisión por ${song.review.reviewer_name || "otra persona"}`;
    if (song.review?.reviewer_lock_active) return "En revisión por vos";
    if (song.review?.is_draft) return `Cambios guardados${song.review.last_reviewed_name ? ` por ${song.review.last_reviewed_name}` : ""}`;
  }
  if (song.stage === "audio") {
    if (song.upload_state === "error") return "La subida falló";
    if (song.metadata_error === "missing_metadata") return "Faltan título, artista o código";
    if (["registered", "uploading"].includes(song.upload_state)) return song.upload_state === "uploading" ? "Subiendo" : "Esperando el archivo";
    if (song.phase === "transcribing") return "Transcribiendo";
    if (song.phase === "separating" || song.phase === "separation_ready") return "Separando voz";
    return "En cola";
  }
  if (song.stage === "rendering") return song.current_is_variant ? "Nueva versión en curso" : "Generando fondo y render";
  if (song.stage === "attention") return song.error || "Falló un paso automático";
  if (song.stage === "discarded") return song.review?.discard?.reason || song.discard?.reason || "";
  if (song.stage === "approved" && song.approved_by_name) return `Aprobó ${song.approved_by_name}`;
  return "";
}

export const CLASSIFICATIONS = [
  { key: "standard", label: "Sin alertas", tone: "neutral" },
  { key: "timing_targeted", label: "Alertas de tiempos", tone: "warning" },
  { key: "manual_full", label: "Revisión completa", tone: "danger" },
];

export const PORTAL_FILTERS = [
  { key: "", label: "Todos los envíos" },
  { key: "sent", label: "Enviadas al portal" },
  { key: "unsent", label: "Sin enviar" },
  { key: "outdated", label: "Portal desactualizado" },
  { key: "changes", label: "Con cambios solicitados" },
];

export function filterSongs(songs, { view = "all", q = "", drafts = false, mine = false, version = "", classification = "", portal = "" } = {}) {
  return songs.filter((song) => {
    if (view === "all" ? song.stage === "discarded" : song.stage !== view) return false;
    if (q && !matchesCampaignSong(q, { title: song.title, artist: song.artist, technical_code: song.technical_code, filename: song.filename })) return false;
    if (drafts && !song.review?.is_draft) return false;
    if (mine && !(song.review?.resume_available || song.review?.reviewer_is_current_user || song.review?.last_reviewed_by_me)) return false;
    if (version && (song.review?.version || "studio") !== version) return false;
    if (classification && song.review?.review_priority !== classification) return false;
    if (portal === "sent" && !song.portals?.length) return false;
    if (portal === "unsent" && song.portals?.length) return false;
    if (portal === "outdated" && !song.portal_outdated) return false;
    if (portal === "changes" && !song.pending_change_requests) return false;
    return true;
  });
}

const STAGE_ORDER = ["attention", "lyrics", "qc", "ready", "approved", "audio", "rendering", "delivered", "discarded"];

export function sortSongs(songs, view) {
  const list = [...songs];
  if (view === "lyrics") {
    // Keep the backend's effort order (fewest alerts first); drafts filter
    // already comes sorted by most recent save.
    return list.sort((a, b) => (a.review?.queuePosition ?? 1e9) - (b.review?.queuePosition ?? 1e9) || a.ordinal - b.ordinal);
  }
  if (view === "all") {
    return list.sort((a, b) => STAGE_ORDER.indexOf(a.stage) - STAGE_ORDER.indexOf(b.stage) || a.ordinal - b.ordinal);
  }
  return list.sort((a, b) => a.ordinal - b.ordinal);
}

/** Next lyric to review inside the list the operator is looking at. */
export function nextLyricSong(songs, afterItemId = null) {
  const available = (song) => song.stage === "lyrics" && primaryAction(song)?.key === "review";
  const index = afterItemId ? songs.findIndex((song) => song.id === afterItemId) : -1;
  return songs.slice(index + 1).find(available) || songs.find(available) || null;
}

export function classificationCounts(songs) {
  const counts = { standard: 0, timing_targeted: 0, manual_full: 0 };
  songs.forEach((song) => {
    const key = song.review?.review_priority;
    if (key in counts) counts[key] += 1;
  });
  return counts;
}
