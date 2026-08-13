// POST a lyrics-edit to /edit/:jobId with the 409 youtube_already_published
// retry path and the "unchanged segments" short-circuit. Extracted from
// EditRequestPanel so the post-render Studio Console flow (App.jsx
// EditLyricsRoute → handleApproveLyrics) can reuse the same wire format
// without dragging EditRequestPanel's React state around.
//
// The caller owns submit-lock, error display, and snapshot capture; this
// module only builds the payload, posts, retries, and returns a result
// shape the caller can branch on.

import { editorRevisionConflictDetail } from "./editorRevisionConflict";

const API = import.meta.env.VITE_API_URL || "";

function authHeaders() {
  const token = localStorage.getItem("genly_token");
  return token ? { Authorization: `Bearer ${token}` } : {};
}

// Strip a segments array to the minimal wire shape, preserving manual
// timing locks AND per-line layout (pos/scale/rot) set in the timeline /
// preview. Stripping those silently discards the operator's layout on
// re-render.
export function normalizeSegmentsForEdit(segments) {
  return segments.map((s) => ({
    start: Number(s.start) || 0,
    end: Number(s.end) || 0,
    text: String(s.text || ""),
    ...(s.locked ? { locked: true } : {}),
    ...(s.pos && typeof s.pos.x === "number" ? { pos: { x: s.pos.x, y: s.pos.y } } : {}),
    ...(typeof s.scale === "number" && s.scale !== 1 ? { scale: s.scale } : {}),
    ...(typeof s.rot === "number" && s.rot !== 0 ? { rot: s.rot } : {}),
  }));
}

// Compare a normalized payload against a baseline (the segments as they
// were RENDERED — captured on modal/route open, not the live segments_json
// which the editor's autosave bumps on every layout change).
export function segmentsUnchanged(baseline, payloadSegments) {
  if (!Array.isArray(baseline)) return false;
  if (baseline.length !== payloadSegments.length) return false;
  return baseline.every((s, i) =>
    s.text === payloadSegments[i].text &&
    Math.abs((s.start ?? 0) - payloadSegments[i].start) < 0.001 &&
    Math.abs((s.end ?? 0) - payloadSegments[i].end) < 0.001
  );
}

export function layoutChanged(baseline, payloadSegments) {
  if (!Array.isArray(baseline) || baseline.length !== payloadSegments.length) return false;
  return baseline.some((s, i) => {
    const p = payloadSegments[i];
    const o = s.pos || {}, np = p.pos || {};
    return (o.x ?? null) !== (np.x ?? null) || (o.y ?? null) !== (np.y ?? null)
      || (s.scale ?? 1) !== (p.scale ?? 1) || (s.rot ?? 0) !== (p.rot ?? 0);
  });
}

// Map raw backend HTTPException details to friendly Spanish copy. If the
// backend message doesn't match a known prefix we fall through to the
// original detail so nothing gets swallowed silently.
//
// CRITICAL: must always return a string (or null). Pydantic v2 returns
// `detail` as an array of {type, loc, msg, input} objects on 422 —
// returning that raw bombs the consumer into "Objects are not valid as a
// React child" (incident 2026-05-18).
export function translateBackendError(raw, t) {
  if (raw == null) return null;
  const tr = typeof t === "function" ? t : () => null;
  if (raw && typeof raw === "object" && raw.code === "edit_in_progress") {
    return tr("edit.error_already_editing") ||
      "Este video se está re-renderizando ahora. Esperá a que termine (revisalo en la página del video) y volvé a aplicar tus cambios.";
  }
  // 409 revision conflict: the backend returns {detail:"editor_revision_conflict",
  // server_revision, server_segments}. Without this map the raw object leaked into
  // React and crashed /generating (Sentry #33); map it to a clear retry message.
  if (editorRevisionConflictDetail(raw) || raw === "editor_revision_conflict") {
    return tr("edit.error_revision_conflict") ||
      "La letra cambió en el servidor mientras editabas. Recargá el editor para traer la última versión y volvé a aplicar tus cambios.";
  }
  let str;
  if (typeof raw === "string") {
    str = raw;
  } else if (Array.isArray(raw)) {
    str = raw
      .map((e) => (e && typeof e === "object" && e.msg) ? e.msg : String(e))
      .join("; ");
  } else if (typeof raw === "object") {
    str = raw.msg || raw.detail || JSON.stringify(raw);
  } else {
    str = String(raw);
  }
  if (str.startsWith("No cached background available")) {
    return tr("edit.error_no_bg_cache") ||
      "Este video no tiene un fondo cacheado para reusar. Regenerá el fondo primero (cuesta ~US$0.90).";
  }
  if (str.startsWith("Job must be in pending_review")) {
    return tr("edit.error_wrong_status") ||
      "Esta regeneración ya está en marcha o el video pasó a otro estado.";
  }
  if (str.startsWith("Maximum edit limit")) {
    return tr("edit.error_limit_reached") ||
      "Alcanzaste el límite de 3 regeneraciones para este video.";
  }
  // QA fix 2026-05-28: el backend tiene DOS errores 400 que empiezan con
  // "Lyrics edit requires". Antes los mappeábamos a "no hay letras", lo
  // cual era falso para el caso del status gate y confundía mal al
  // operador. Diferenciamos por el sub-string específico:
  //   1) "Lyrics edit requires 'segments' in the request body..."
  //      → realmente faltan segments en el payload (bug del frontend o
  //        un editor abierto sobre un job sin segments_json).
  //   2) "Lyrics edit requires the job to be done, pending_review, or
  //       rejected (current: editing)"
  //      → el job está mid-render, hay que esperar.
  // El segundo caso también lo dispara el endpoint para typography/
  // background con "Job must be in pending_review" (ya mapeado arriba).
  // Acá agregamos el match específico para lyrics+metadata status gate.
  if (
    str.includes("requires the job to be") ||
    /\(current: editing\)/.test(str)
  ) {
    return tr("edit.error_already_editing") ||
      "Este video se está re-renderizando ahora. Esperá a que termine (revisalo en la página del video) y volvé a aplicar tus cambios.";
  }
  if (str.startsWith("Lyrics edit requires") || str.startsWith("Job has no persisted")) {
    return tr("edit.error_no_segments") ||
      "Este video no tiene letras guardadas para editar. Subí la canción de nuevo.";
  }
  return str;
}

// Single POST with the 409 youtube_already_published retry. Returns
// {ok, status, data, cancelled} so the caller decides what to do.
// cancelled=true means the operator declined the confirm prompt.
async function postEditWithRetry(jobId, payload, { confirmYoutubeDrift, t } = {}) {
  let res = await fetch(`${API}/edit/${jobId}`, {
    method: "POST",
    headers: { ...authHeaders(), "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  let data = await res.json().catch(() => ({}));
  if (
    res.status === 409 &&
    data?.detail?.code === "youtube_already_published"
  ) {
    const tr = typeof t === "function" ? t : () => null;
    const url = data.detail.youtube_url;
    const msg = (tr("edit.youtube_drift_confirm") ||
      "Este video ya está publicado en YouTube. La re-sincronización actualizará el archivo en la plataforma pero NO reemplazará el video en YouTube (la API de YouTube no permite reemplazar archivos, solo metadata).\n\n¿Continuar igual?")
      + (url ? `\n\nYouTube: ${url}` : "");
    const confirmFn = typeof confirmYoutubeDrift === "function"
      ? confirmYoutubeDrift
      : (m) => window.confirm(m);
    if (!confirmFn(msg)) {
      return { ok: false, cancelled: true };
    }
    res = await fetch(`${API}/edit/${jobId}`, {
      method: "POST",
      headers: { ...authHeaders(), "Content-Type": "application/json" },
      body: JSON.stringify({ ...payload, allow_youtube_drift: true }),
    });
    data = await res.json().catch(() => ({}));
  }
  return { ok: res.ok, status: res.status, data };
}

// Submit a lyrics edit. Returns:
//   { ok: true, status: 202, data }                  → enqueued
//   { ok: false, cancelled: true }                   → operator declined YouTube prompt
//   { ok: false, unchanged: true, segments }         → nothing changed; caller may force-rerender
//   { ok: false, status, data, error }               → backend rejected (4xx/5xx), error = friendly string
//
// `baselineSegments` is the snapshot of the segments as they were RENDERED,
// captured when the editor opened. Used for the unchanged short-circuit so
// the editor's autosave-of-layout doesn't make every approve look like a no-op.
export async function submitLyricsEdit({
  jobId,
  segments,
  baselineSegments,
  font,
  textCase,
  textContrast,
  lyricsAnimation,
  lineTransition,
  force = false,
  confirmYoutubeDrift,
  t,
}) {
  if (!Array.isArray(segments) || segments.length === 0) {
    return {
      ok: false,
      error: (typeof t === "function" && t("edit.lyrics_empty")) ||
        "Las letras quedaron vacías — no hay nada que renderizar.",
    };
  }
  const payloadSegments = normalizeSegmentsForEdit(segments);
  const payload = {
    edit_type: "lyrics",
    segments: payloadSegments,
    ...(font != null ? { font } : {}),
    ...(textCase != null ? { text_case: textCase } : {}),
    ...(textContrast != null ? { text_contrast: textContrast } : {}),
    ...(lyricsAnimation != null ? { lyrics_animation: lyricsAnimation } : {}),
    ...(lineTransition != null ? { line_transition: lineTransition } : {}),
  };
  if (!force) {
    const typographyChanged =
      font != null || textCase != null || textContrast != null ||
      lyricsAnimation != null || lineTransition != null;
    const layoutDelta = layoutChanged(baselineSegments, payloadSegments);
    const textChanged = !segmentsUnchanged(baselineSegments, payloadSegments);
    if (!typographyChanged && !layoutDelta && !textChanged) {
      return { ok: false, unchanged: true, segments };
    }
  }
  const { ok, status, data, cancelled } = await postEditWithRetry(jobId, payload, {
    confirmYoutubeDrift,
    t,
  });
  if (cancelled) return { ok: false, cancelled: true };
  if (!ok) {
    const friendly = translateBackendError(data?.detail, t) || `Error ${status}`;
    return { ok: false, status, data, error: friendly };
  }
  return { ok: true, status, data };
}
