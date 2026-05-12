import { useState, useEffect, useRef } from "react";
import { useI18n } from "../i18n";
import { getDownloadUrl, useMediaUrl } from "../mediaUrl";
import { JobDetailTour } from "./OnboardingTour";
import ProResBadge from "./ProResBadge";
import EditRequestPanel from "./EditRequestPanel";
import EnableProResModal from "./EnableProResModal";
import DriveTransferModal from "./DriveTransferModal";

const API = import.meta.env.VITE_API_URL || "";

function authHeaders() {
  const token = localStorage.getItem("genly_token");
  return token ? { Authorization: `Bearer ${token}` } : {};
}

const MEDIA_TABS = [
  { key: "video", label: "Lyric Video", desc: "1920x1080" },
  { key: "short", label: "Short", desc: "1080x1920" },
  { key: "thumbnail", label: "Thumbnail", desc: "1280x720" },
];

// Broadcast master tab — added conditionally only when the job's
// delivery_profile is "umg" or "both". ProRes 422 HQ in a .mov, not
// previewable in browser, so the tab shows a download-only panel.
// (Internal `umg_master` key is preserved end-to-end on the wire so
// existing jobs keep working; only the visible label is generic.)
const PRORES_MASTER_TAB = {
  key: "umg_master",
  label: "Máster ProRes",
  desc: "ProRes 422 HQ · MOV",
};

function ProvenanceTab({ jobId, t }) {
  const [records, setRecords] = useState(null);
  const [loading, setLoading] = useState(true);
  const [expandedId, setExpandedId] = useState(null);

  useEffect(() => {
    fetch(`${API}/provenance/${jobId}`, { headers: authHeaders() })
      .then((r) => r.json())
      .then((data) => { setRecords(data); setLoading(false); })
      .catch(() => setLoading(false));
  }, [jobId]);

  const STEP_ICONS = {
    lyrics_analysis: { icon: "M9 19V6l12-2v13", color: "text-purple-400" },
    video_bg: { icon: "M15 10l4.553-2.276A1 1 0 0121 8.618v6.764a1 1 0 01-1.447.894L15 14M5 18h8a2 2 0 002-2V8a2 2 0 00-2-2H5a2 2 0 00-2 2v8a2 2 0 002 2z", color: "text-blue-400" },
    image_bg: { icon: "M4 16l4.586-4.586a2 2 0 012.828 0L16 16m-2-2l1.586-1.586a2 2 0 012.828 0L20 14m-6-6h.01M6 20h12a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v12a2 2 0 002 2z", color: "text-green-400" },
    yt_metadata: { icon: "M22.54 6.42a2.78 2.78 0 00-1.94-2C18.88 4 12 4 12 4s-6.88 0-8.6.46a2.78 2.78 0 00-1.94 2A29 29 0 001 11.75a29 29 0 00.46 5.33A2.78 2.78 0 003.4 19.13C5.12 19.56 12 19.56 12 19.56s6.88 0 8.6-.46a2.78 2.78 0 001.94-2A29 29 0 0023 11.75a29 29 0 00-.46-5.33z", color: "text-red-400" },
    output_validation: { icon: "M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z", color: "text-amber-400" },
    background_human: { icon: "M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z", color: "text-cyan-400" },
  };

  const STEP_LABELS = {
    lyrics_analysis: t("prov.lyrics_analysis") || "Lyrics Analysis",
    video_bg: t("prov.video_bg") || "Video Background",
    image_bg: t("prov.image_bg") || "Image Background",
    yt_metadata: t("prov.yt_metadata") || "YouTube Metadata",
    output_validation: t("prov.output_validation") || "Content Validation",
    background_human: t("prov.background_human") || "Human Background",
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center py-12">
        <div className="w-6 h-6 border-2 border-brand border-t-transparent rounded-full animate-spin" />
      </div>
    );
  }

  if (!records || records.length === 0) {
    return (
      <div className="text-center py-12">
        <p className="text-gray-500 text-sm">{t("prov.no_records") || "No AI provenance records found"}</p>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between mb-2">
        <p className="text-xs text-gray-500 uppercase tracking-wider">{t("prov.title") || "AI Provenance"}</p>
        <button
          onClick={async () => {
            const res = await fetch(`${API}/provenance/${jobId}/export`, { headers: authHeaders() });
            if (!res.ok) return;
            const blob = await res.blob();
            const url = URL.createObjectURL(blob);
            const a = document.createElement("a");
            a.href = url;
            a.download = `${jobId}-provenance.json`;
            a.click();
            URL.revokeObjectURL(url);
          }}
          className="text-xs text-brand hover:text-brand-light transition-colors flex items-center gap-1"
        >
          <svg className="w-3 h-3" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
            <path d="M12 10v6m0 0l-3-3m3 3l3-3m2 8H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
          </svg>
          {t("prov.export") || "Export"}
        </button>
      </div>

      {records.map((r) => {
        const stepInfo = STEP_ICONS[r.step] || { icon: "M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z", color: "text-gray-400" };
        const isExpanded = expandedId === r.id;

        return (
          <div key={r.id} className="glass rounded-xl overflow-hidden">
            <button
              onClick={() => setExpandedId(isExpanded ? null : r.id)}
              className="w-full flex items-center gap-3 px-4 py-3 hover:bg-white/[0.02] transition-colors"
            >
              <div className={`w-8 h-8 rounded-lg bg-surface-1 flex items-center justify-center shrink-0`}>
                <svg className={`w-4 h-4 ${stepInfo.color}`} fill="none" stroke="currentColor" strokeWidth="1.5" viewBox="0 0 24 24">
                  <path d={stepInfo.icon} />
                </svg>
              </div>
              <div className="flex-1 min-w-0 text-left">
                <p className="text-sm font-medium text-white">{STEP_LABELS[r.step] || r.step}</p>
                <p className="text-[10px] text-gray-500">{r.tool_name}</p>
              </div>
              <div className="text-right shrink-0">
                <p className="text-[10px] text-gray-500">{r.duration_ms ? `${(r.duration_ms / 1000).toFixed(1)}s` : ""}</p>
                <p className="text-[10px] text-gray-600">{r.created_at ? new Date(r.created_at).toLocaleTimeString() : ""}</p>
              </div>
              <svg className={`w-4 h-4 text-gray-500 transition-transform ${isExpanded ? "rotate-180" : ""}`} fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                <path d="M19 9l-7 7-7-7" />
              </svg>
            </button>

            {isExpanded && (
              <div className="px-4 pb-3 pt-0 space-y-2 border-t border-white/[0.04]">
                {r.input_data_types && (
                  <div>
                    <p className="text-[10px] text-gray-600 uppercase mb-1">{t("prov.data_sent") || "Data Sent"}</p>
                    <div className="flex flex-wrap gap-1">
                      {r.input_data_types.map((dt, i) => (
                        <span key={i} className="px-2 py-0.5 rounded bg-surface-1 text-[10px] text-gray-400">{dt}</span>
                      ))}
                    </div>
                  </div>
                )}
                <div>
                  <p className="text-[10px] text-gray-600 uppercase mb-1">Prompt</p>
                  <pre className="text-[11px] text-gray-400 bg-surface-1 rounded-lg px-3 py-2 max-h-40 overflow-y-auto whitespace-pre-wrap break-words">
                    {r.prompt_sent}
                  </pre>
                </div>
                {r.response_summary && (
                  <div>
                    <p className="text-[10px] text-gray-600 uppercase mb-1">{t("prov.response") || "Response"}</p>
                    <p className="text-[11px] text-gray-400 bg-surface-1 rounded-lg px-3 py-2">{r.response_summary}</p>
                  </div>
                )}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

export default function JobDetail({ job, onBack, onJobUpdate }) {
  const { t } = useI18n();
  const [activeTab, setActiveTab] = useState("video");
  const [uploading, setUploading] = useState(false);
  const [youtubeResult, setYoutubeResult] = useState(job.youtube || null);
  const [metadataPreview, setMetadataPreview] = useState(null);
  const [showYoutubePanel, setShowYoutubePanel] = useState(false);
  const [reviewNotes, setReviewNotes] = useState("");
  const [approving, setApproving] = useState(false);
  const [retrying, setRetrying] = useState(false);

  // handleRetry MUST estar definida antes del early-return que la usa
  // (línea ~311 para jobs con status=error). Si se la pone más abajo
  // junto a los otros handlers, el JSX del early-return accede a la
  // const en su temporal dead zone → ReferenceError "Cannot access
  // 'handleRetry' before initialization" → GlobalErrorBoundary catch
  // → app entera crashea. Lo aprendimos cuando un job en error rompió
  // toda la dashboard de un cliente.
  const handleRetry = async () => {
    if (retrying) return;
    setRetrying(true);
    try {
      const res = await fetch(`${API}/retry/${job.job_id}`, {
        method: "POST",
        headers: authHeaders(),
      });
      if (res.ok) {
        const updated = await (await fetch(`${API}/status/${job.job_id}`, { headers: authHeaders() })).json();
        onJobUpdate?.(updated);
        // Navigate back so the user sees the batch/history with the job now processing.
        onBack?.();
      } else {
        const body = await res.json().catch(() => ({}));
        alert(body.detail || "No se pudo reintentar el video.");
      }
    } catch {
      alert("Error de red al reintentar.");
    }
    setRetrying(false);
  };

  // Synchronous guard against double-click — `approving` (state) is updated
  // asynchronously by React, so a rapid second click can fire its handler
  // before the re-render flips the disabled flag. The ref is set BEFORE
  // any await, so the second handler sees `current=true` immediately and
  // bails out.
  const approveLockRef = useRef(false);
  const name = (job.filename || "").replace(/\.mp3$/i, "");

  // Short-lived media URLs (re-fetch when the active tab changes).
  const previewMediaType = activeTab === "thumbnail" ? "thumbnail" : activeTab;
  const previewSrc = useMediaUrl(job.job_id, previewMediaType, "preview");
  const downloadHref = useMediaUrl(job.job_id, previewMediaType, "download");

  const canPreview = job.status === "done" || job.status === "pending_review";
  const canDownload = job.status === "done";
  const isPendingReview = job.status === "pending_review";
  const isEditing = job.status === "editing";
  const isValidationFailed = job.status === "validation_failed";
  const isError = job.status === "error";

  // While the worker is re-rendering an edit request, poll /status every
  // 5s and propagate updates up so the rest of the screen (status badge,
  // approve panel visibility, preview URLs) stays in sync. The interval
  // cleans itself up the moment status leaves "editing".
  useEffect(() => {
    if (!isEditing) return;
    let cancelled = false;
    const tick = async () => {
      try {
        const res = await fetch(`${API}/status/${job.job_id}`, { headers: authHeaders() });
        if (!res.ok || cancelled) return;
        const updated = await res.json();
        if (cancelled) return;
        // Merge into existing job so we don't drop fields /status doesn't return
        // (youtube_data, etc.). onJobUpdate flows it back through App state.
        if (onJobUpdate) onJobUpdate({ ...job, ...updated });
      } catch {}
    };
    const iv = setInterval(tick, 5000);
    tick(); // first tick immediately, no need to wait 5s
    return () => { cancelled = true; clearInterval(iv); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isEditing, job.job_id]);

  const handleEditTriggered = (resp) => {
    // Server already flipped status to "editing" + bumped edit_count.
    // Reflect that immediately in the UI so the approve panel hides and
    // the editing overlay appears, then let polling take over.
    if (onJobUpdate) {
      onJobUpdate({
        ...job,
        status: "editing",
        edit_count: resp?.edit_count ?? (job.edit_count || 0) + 1,
        edits_remaining: resp?.edits_remaining ?? Math.max(0, (job.edits_remaining ?? 3) - 1),
        current_step: resp?.edit_type === "background" ? "background" : "video",
        progress: 0,
      });
    }
  };

  // Editing in progress: render a focused panel instead of falling through
  // to the "not available" early-return below. canPreview is false during
  // editing (the video bytes are being rewritten on R2) but we DO want to
  // show progress + clear messaging — not the generic dead-end message.
  if (isEditing) {
    return (
      <div className="w-full max-w-2xl animate-fade-in">
        <div className="flex items-center gap-3 mb-6">
          <button onClick={onBack} className="w-9 h-9 shrink-0 rounded-xl bg-surface-2/40 ring-1 ring-white/[0.04] hover:ring-white/[0.08] flex items-center justify-center text-gray-400 hover:text-white transition-colors">
            <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24"><path d="M19 12H5M12 19l-7-7 7-7" /></svg>
          </button>
          <div>
            <h2 className="text-xl font-bold">{name}</h2>
            <p className="text-sm text-gray-500">{job.artist}</p>
          </div>
        </div>
        <div className="rounded-card p-5 bg-brand/[0.08] ring-1 ring-brand/25">
          <div className="flex items-start gap-3">
            <div className="w-9 h-9 rounded-lg bg-brand/15 ring-1 ring-brand/30 flex items-center justify-center shrink-0">
              <span className="w-4 h-4 border-2 border-brand-light border-t-transparent rounded-full animate-spin" />
            </div>
            <div className="flex-1 min-w-0">
              <p className="text-sm font-semibold text-white">
                {t("edit.in_progress_title") || "Aplicando tus cambios..."}
              </p>
              <p className="text-xs text-ink-secondary mt-0.5">
                {job.current_step === "background"
                  ? (t("edit.in_progress_bg") || "Generando nuevo fondo con Veo · mantiene lyrics y tiempos · ~10-15 min")
                  : (t("edit.in_progress_typo") || "Re-renderizando con la tipografía nueva · usa el fondo cacheado · ~5-10 min")}
              </p>
              <div className="mt-3 h-1.5 rounded-full bg-surface-3/60 overflow-hidden">
                <div
                  className="h-full bg-gradient-to-r from-brand to-brand-light transition-[width] duration-700 ease-out"
                  style={{ width: `${Math.min(100, Math.max(3, job.progress || 0))}%` }}
                />
              </div>
              <p className="text-[10px] text-gray-500 mt-1 font-mono">
                {job.current_step || "?"} · {job.progress || 0}%
              </p>
              <p className="text-[11px] text-gray-500 mt-3 leading-relaxed">
                {t("edit.no_video_during_editing") || "El video viejo se está reemplazando con tus cambios. Cuando termine vas a poder verlo acá."}
              </p>
            </div>
          </div>
        </div>
      </div>
    );
  }

  if (!canPreview && !isValidationFailed && !isError) {
    return (
      <div className="w-full max-w-2xl animate-fade-in text-center py-20">
        <p className="text-gray-400">{t("detail.not_available")}</p>
        <button onClick={onBack} className="btn-secondary mt-4">{t("detail.back")}</button>
      </div>
    );
  }

  // Error state: show a compact error panel with retry option.
  if (isError && !isValidationFailed) {
    return (
      <div className="w-full max-w-2xl animate-fade-in">
        <div className="flex items-center gap-3 mb-6">
          <button onClick={onBack} className="w-9 h-9 shrink-0 rounded-xl bg-surface-2/40 ring-1 ring-white/[0.04] hover:ring-white/[0.08] flex items-center justify-center text-gray-400 hover:text-white transition-colors">
            <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24"><path d="M19 12H5M12 19l-7-7 7-7" /></svg>
          </button>
          <div>
            <h2 className="text-xl font-bold">{name}</h2>
            <p className="text-sm text-gray-500">{job.artist}</p>
          </div>
        </div>
        <div className="rounded-card bg-red-500/[0.06] ring-1 ring-red-500/20 px-5 py-5">
          <p className="text-sm font-semibold text-red-300 mb-1">{t("detail.error_title") || "El video falló durante la generación"}</p>
          <p className="text-xs text-red-400/70 mb-4">{job.error || t("detail.error_unknown") || "Error desconocido"}</p>
          <div className="flex gap-2">
            <button
              onClick={handleRetry}
              disabled={retrying}
              className="btn-primary text-xs h-9 px-4 disabled:opacity-50"
            >
              {retrying ? (
                <><div className="inline-block w-3 h-3 border-2 border-white border-t-transparent rounded-full animate-spin mr-1.5" />Reintentando…</>
              ) : (
                t("detail.retry") || "Reintentar sin re-subir"
              )}
            </button>
            <button onClick={() => onBack && onBack()} className="btn-secondary text-xs h-9 px-4">
              {t("detail.back") || "Volver"}
            </button>
          </div>
        </div>
      </div>
    );
  }

  // Single navigation to a server-streamed zip. The previous "loop three
  // <a>.click() calls" approach got blocked as popup spam by Chrome —
  // the browser would only honour the last click (thumbnail) and open it
  // in a tab instead of downloading. /download/{id}/all bundles the
  // small deliverables server-side so we get one click → one file. We
  // mint a short-lived media token first so the URL doesn't carry the
  // long-lived JWT (C3 fix).
  const downloadAllZip = async () => {
    try {
      const url = await getDownloadUrl(job.job_id, "all");
      window.location.href = url;
    } catch {}
  };
  // ProRes is generated lazily server-side. Fast path: prewarm has
  // already produced the .mov → 200 with bytes (or 302 to R2). Slow
  // path: backend returns 202 + Retry-After when the transcode is
  // queued or in progress; we keep the toast up and re-fetch until
  // 200/302 lands. The whole point is to NEVER block a uvicorn worker
  // for the 60-300 s of ffmpeg — under multi-tenant load, blocking
  // would tie up workers and hang every other request.
  //
  // Hard ceiling at 8 minutes total wait (16 polls × 30 s). 4K@60 cold
  // transcode + R2 upload is ~3-4 min; 8 min covers a queue depth of
  // 2-3 jobs ahead before we give up and tell the user to retry.
  // Local readiness state so the badge turns green immediately after a
  // successful ProRes download — no server re-fetch needed.
  const [localProresReady, setLocalProresReady] = useState(
    Boolean(
      (job.s3_keys && job.s3_keys.umg_master && job.s3_keys.umg_short)
      || job.prores_ready
    )
  );

  const [proResHint, setProResHint] = useState(null);
  const PRORES_MAX_WAIT_MS = 8 * 60 * 1000;
  const PRORES_POLL_FALLBACK_MS = 30 * 1000;

  const fetchProResAndSave = async (fileType, suggestedName) => {
    setProResHint(fileType);
    const deadline = Date.now() + PRORES_MAX_WAIT_MS;
    try {
      while (Date.now() < deadline) {
        const url = await getDownloadUrl(job.job_id, fileType);
        // `redirect: 'manual'` is critical: /download responds with
        // 302 → R2 signed URL when the file is cached. Default
        // `redirect: 'follow'` would make fetch hit R2 cross-origin
        // and fail CORS (R2 doesn't allow XHR from our origin).
        // With 'manual' we get opaqueredirect → we then navigate the
        // main window to the same-origin /download URL which follows
        // the 302 natively. The R2 signed URL includes
        // ResponseContentDisposition: attachment so the browser
        // downloads without navigating away.
        const res = await fetch(url, { redirect: "manual" });
        if (res.type === "opaqueredirect") {
          // Navigate same-tab: the 302 → R2 URL carries
          // Content-Disposition: attachment so the browser triggers
          // a download, not a page navigation. _blank would open
          // a new tab AND lose the Content-Disposition hint for
          // cross-origin URLs.
          window.location.href = url;
          setLocalProresReady(true);
          return;
        }
        if (res.status === 200) {
          // Bytes arrived — turn into a blob download and exit.
          const blob = await res.blob();
          const blobUrl = URL.createObjectURL(blob);
          const a = document.createElement("a");
          a.href = blobUrl;
          a.download = suggestedName;
          a.click();
          setTimeout(() => URL.revokeObjectURL(blobUrl), 60_000);
          setLocalProresReady(true);
          return;
        }
        if (res.status === 202) {
          // Backend queued/in-progress. Honour Retry-After header.
          const retryHdr = parseInt(res.headers.get("Retry-After") || "", 10);
          const retryMs = (Number.isFinite(retryHdr) && retryHdr > 0)
            ? retryHdr * 1000
            : PRORES_POLL_FALLBACK_MS;
          await new Promise((r) => setTimeout(r, retryMs));
          continue;
        }
        // Hard error (400/404/500). Surface the backend's own `detail`
        // string when present so the operator sees a real reason instead
        // of a generic HTTP code. 404 specifically means the source MP4
        // is no longer on disk/R2 — irrecoverable, needs a full re-render.
        let backendDetail = "";
        try {
          const body = await res.json();
          backendDetail = (body && body.detail) || "";
        } catch {
          /* non-JSON body — fall through with empty detail */
        }
        const err = new Error(`HTTP ${res.status}`);
        err.status = res.status;
        err.detail = backendDetail;
        throw err;
      }
      const err = new Error("timeout");
      err.kind = "timeout";
      throw err;
    } catch (err) {
      console.error("ProRes download failed:", err);
      let message;
      if (err.kind === "timeout") {
        message = t("detail.prores_timeout");
      } else if (err.status === 404) {
        // Source MP4 missing on the server — only a full re-render fixes it.
        message = t("detail.prores_source_missing");
      } else {
        const reason = err.detail || err.message || "error";
        message = t("detail.prores_failed", { reason });
      }
      alert(message);
    } finally {
      setProResHint(null);
    }
  };
  const songSlug = (job.filename || "video").replace(/\.[^.]+$/, "");
  const downloadProResMaster = () =>
    fetchProResAndSave("umg_master", `${songSlug}_master.mov`);
  const downloadProResShort = () =>
    fetchProResAndSave("umg_short", `${songSlug}_short.mov`);

  const previewMetadata = async () => {
    setShowYoutubePanel(true);
    try {
      const res = await fetch(`${API}/youtube/metadata/${job.job_id}`, { method: "POST", headers: authHeaders() });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || `Error ${res.status}`);
      }
      const data = await res.json();
      setMetadataPreview(data);
    } catch (err) {
      setMetadataPreview({ error: err.message });
    }
  };

  const uploadToYoutube = async (privacy = "unlisted") => {
    setUploading(true);
    try {
      const res = await fetch(`${API}/youtube/upload/${job.job_id}?privacy=${privacy}`, { method: "POST", headers: authHeaders() });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || `Error ${res.status}`);
      }
      const data = await res.json();
      setYoutubeResult(data);
    } catch (err) {
      setYoutubeResult({ error: err.message });
    }
    setUploading(false);
  };

  const handleApprove = async () => {
    if (approveLockRef.current) return;
    approveLockRef.current = true;
    setApproving(true);
    try {
      const res = await fetch(`${API}/approve/${job.job_id}`, {
        method: "POST",
        headers: { ...authHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ notes: reviewNotes }),
      });
      if (res.ok) {
        const updated = await (await fetch(`${API}/status/${job.job_id}`, { headers: authHeaders() })).json();
        onJobUpdate?.(updated);
      }
    } catch {}
    setApproving(false);
    approveLockRef.current = false;
  };

  // handleRetry está definida más arriba (~línea 175) para que esté
  // disponible antes del early-return de status=error. No duplicar acá.

  const handleReject = async () => {
    if (approveLockRef.current) return;
    approveLockRef.current = true;
    setApproving(true);
    try {
      const res = await fetch(`${API}/reject/${job.job_id}`, {
        method: "POST",
        headers: { ...authHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ notes: reviewNotes }),
      });
      if (res.ok) {
        // Refresh the job state for any listing in the parent so the row
        // shows "rejected", then go back. Staying on the detail screen
        // would show "this job is not previewable" because rejected jobs
        // intentionally can't be re-opened — better UX is to land the
        // user back on the dashboard / batch view.
        try {
          const updated = await (await fetch(`${API}/status/${job.job_id}`, { headers: authHeaders() })).json();
          onJobUpdate?.(updated);
        } catch {}
        onBack?.();
      }
    } catch {}
    setApproving(false);
    approveLockRef.current = false;
  };

  // ProRes button visibility — gated by delivery profile + done status,
  // NOT by the presence of `files.umg_master_url`. The download endpoint
  // (/download/{id}/umg_master) handles the missing-file case by
  // enqueueing a lazy prewarm and returning 202 + Retry-After; the
  // fetchProResAndSave polls until ready (up to 8 min).
  //
  // Why decouple from the URL: jobs created before the prewarm feature
  // existed (or whose prewarm died silently) sit forever with
  // umg_master_url=null and no way for the operator to recover the file.
  // Showing the button always lets clicking it trigger the recovery.
  // Un job es "UMG" si fue creado con delivery_profile=umg/both, O si
  // se le habilitó ProRes retroactivamente via POST /enable-prores
  // (que persiste umg_spec sin tocar delivery_profile, para no perder
  // el dato histórico de cómo se rindió originalmente).
  const isUmgJob =
    job.delivery_profile === "umg"
    || job.delivery_profile === "both"
    || !!job.umg_spec;
  const isJobDone = job.status === "done";
  const hasUmgMaster = isUmgJob && isJobDone;

  // El botón "Exportar a ProRes" aparece solo cuando el job está done,
  // NO tiene ProRes habilitado todavía, y el usuario tiene el feature
  // flag prores_export. Click → modal que persiste umg_spec en el job
  // y dispara el transcoding. Una vez hecho, isUmgJob flipea a true en
  // el próximo /status poll y aparece el tab de ProRes Master.
  const user = (() => {
    try { return JSON.parse(localStorage.getItem("genly_user") || "null"); } catch { return null; }
  })();
  const canEnableProRes =
    isJobDone && !isUmgJob && user?.features?.prores_export === true;
  const [showProResModal, setShowProResModal] = useState(false);
  const [proResToast, setProResToast] = useState(null);

  // Drive integration: poleamos /drive/status al cargar el job para
  // decidir si mostrar el botón "Guardar en Drive". El status también
  // se polea cada N min por si el user se desconectó en otra tab.
  // No bloqueamos el render del job — el botón aparece cuando llega.
  //
  // Canary mode: features.drive_export gatea TODO el flow Drive.
  // Sin el flag no peguemos a /drive/status (evita 403 noise en logs).
  const driveFeatureEnabled = user?.features?.drive_export === true;
  const [driveConnected, setDriveConnected] = useState(false);
  const [showDriveModal, setShowDriveModal] = useState(false);
  // file_type a transferir cuando el user abre el modal: por default el
  // umg_master si está disponible, sino el video MP4.
  const driveFileType = isUmgJob ? "umg_master" : "video";
  useEffect(() => {
    if (!isJobDone) return;
    if (!driveFeatureEnabled) return;
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch(`${API}/drive/status`, { headers: authHeaders() });
        if (!res.ok) return;
        const data = await res.json();
        if (!cancelled) setDriveConnected(!!data.connected);
      } catch {
        // Silent fail — si /drive/status no responde, no mostramos el
        // botón, lo cual es la conducta segura. El user puede ir a
        // Settings a conectar.
      }
    })();
    return () => { cancelled = true; };
  }, [isJobDone, job.job_id, driveFeatureEnabled]);
  // Short ProRes follows the same opt-in: any UMG-flavoured job gets a
  // separate vertical-format master alongside the main one. Generated
  // lazily by /download/{id}/umg_short the first time it's clicked.
  const hasUmgShort = isUmgJob && isJobDone;

  const ALL_TABS = [
    ...MEDIA_TABS,
    ...(hasUmgMaster ? [PRORES_MASTER_TAB] : []),
    { key: "provenance", label: t("prov.title") || "Provenance" },
  ];

  return (
    <div className="w-full max-w-4xl animate-fade-in">
      {/* JobDetail tour: auto-fires on the FIRST pending_review job a
          new operator opens. The tour walks through approval semantics
          + ProRes download. We read `user` from localStorage here so
          we don't have to thread it through the route — the age-gate
          just needs `created_at`. */}
      <JobDetailTour
        user={(() => { try { return JSON.parse(localStorage.getItem("genly_user") || "null"); } catch { return null; } })()}
        hasUmgMaster={hasUmgMaster}
        isPendingReview={isPendingReview}
      />
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-end sm:justify-between gap-3 mb-8">
        <div className="flex items-center gap-3 min-w-0">
          <button onClick={onBack}
            className="w-9 h-9 shrink-0 rounded-xl bg-surface-2/40 ring-1 ring-white/[0.04] hover:ring-white/[0.08] hover:text-white flex items-center justify-center text-gray-400 transition-colors">
            <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
              <path d="M19 12H5M12 19l-7-7 7-7" />
            </svg>
          </button>
          <div className="min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              <h2 className="text-xl font-bold tracking-tight truncate">{name}</h2>
              {isPendingReview && (
                <span
                  data-tour="jobdetail-status-badge"
                  className="px-2 py-0.5 rounded-full bg-amber-500/15 text-amber-300 ring-1 ring-amber-500/30 text-[10px] font-semibold uppercase tracking-wider"
                >
                  {t("batch.pending_review") || "Pendiente"}
                </span>
              )}
              {isValidationFailed && (
                <span className="px-2 py-0.5 rounded-full bg-red-500/15 text-red-300 ring-1 ring-red-500/30 text-[10px] font-semibold uppercase tracking-wider">
                  {t("batch.validation_failed") || "Falló validación"}
                </span>
              )}
              <ProResBadge
                deliveryProfile={job.delivery_profile}
                proresReady={localProresReady}
                jobStatus={job.status}
                size="md"
              />
              {job.status === "done" && job.approved_by && (
                <span className="px-2 py-0.5 rounded-full bg-accent/15 text-accent ring-1 ring-accent/30 text-[10px] font-semibold uppercase tracking-wider">
                  {t("detail.approved") || "Aprobado"}
                </span>
              )}
            </div>
            <p className="text-sm text-ink-secondary mt-0.5 truncate">{job.artist}</p>
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          {canDownload && (() => {
            // All profiles (youtube, umg, both) now produce the MP4 +
            // short + thumbnail set in the pipeline, so "Descargar todo"
            // is always relevant. ProRes is generated on demand via the
            // dedicated button when the job opted into UMG.
            const profile = job.delivery_profile || "youtube";
            const downloadIcon = (
              <svg className="inline-block w-4 h-4 mr-1.5 -mt-0.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4" /><polyline points="7 10 12 15 17 10" /><line x1="12" y1="15" x2="12" y2="3" />
              </svg>
            );
            return (
              <>
                <button
                  onClick={downloadAllZip}
                  className="btn-secondary text-xs h-10 px-4"
                  data-tour="jobdetail-download-all"
                >
                  {downloadIcon}
                  {t("detail.download_all") || "Descargar todo"}
                </button>
                {hasUmgMaster && (
                  <button
                    onClick={downloadProResMaster}
                    className="btn-secondary text-xs h-10 px-4"
                    data-tour="jobdetail-prores-master"
                  >
                    {downloadIcon}
                    {t("detail.download_master") || "Master ProRes"}
                  </button>
                )}
                {hasUmgShort && (
                  <button onClick={downloadProResShort} className="btn-secondary text-xs h-10 px-4">
                    {downloadIcon}
                    {t("detail.download_short_prores") || "Short ProRes"}
                  </button>
                )}
              </>
            );
          })()}
          {canDownload && !youtubeResult && (
            <button onClick={previewMetadata} className="btn-primary text-xs h-10 px-5">
              <svg className="inline-block w-4 h-4 mr-1.5 -mt-0.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                <path d="M22.54 6.42a2.78 2.78 0 00-1.94-2C18.88 4 12 4 12 4s-6.88 0-8.6.46a2.78 2.78 0 00-1.94 2A29 29 0 001 11.75a29 29 0 00.46 5.33A2.78 2.78 0 003.4 19.13C5.12 19.56 12 19.56 12 19.56s6.88 0 8.6-.46a2.78 2.78 0 001.94-2A29 29 0 0023 11.75a29 29 0 00-.46-5.33z"/><polygon points="9.75 15.02 15.5 11.75 9.75 8.48 9.75 15.02"/>
              </svg>
              {t("detail.publish_youtube")}
            </button>
          )}
          {canDownload && youtubeResult && !youtubeResult.error && (
            <a href={youtubeResult.url} target="_blank" rel="noopener noreferrer"
              className="inline-flex items-center h-10 px-5 rounded-button text-xs font-semibold text-white bg-red-600 hover:bg-red-700 transition-colors">
              <svg className="inline-block w-4 h-4 mr-1.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                <path d="M22.54 6.42a2.78 2.78 0 00-1.94-2C18.88 4 12 4 12 4s-6.88 0-8.6.46a2.78 2.78 0 00-1.94 2A29 29 0 001 11.75a29 29 0 00.46 5.33A2.78 2.78 0 003.4 19.13C5.12 19.56 12 19.56 12 19.56s6.88 0 8.6-.46a2.78 2.78 0 001.94-2A29 29 0 0023 11.75a29 29 0 00-.46-5.33z"/><polygon points="9.75 15.02 15.5 11.75 9.75 8.48 9.75 15.02"/>
              </svg>
              {t("detail.view_youtube")}
            </a>
          )}
        </div>
      </div>

      {/* ProRes hint toast — only on first click. The transcode runs
          on the server (~60-120 s for a 3-min song) and the browser
          shows its native download UI during the wait, so the user
          knows something is happening; this banner explains why. */}
      {proResHint && (
        <div className="mb-4 rounded-card bg-brand/[0.08] ring-1 ring-brand/25 px-4 py-3 flex items-center gap-3">
          <div className="w-4 h-4 border-2 border-brand border-t-transparent rounded-full animate-spin shrink-0" />
          <div className="flex-1 text-sm text-brand-light">
            {proResHint === "umg_short"
              ? "Generando Short ProRes (vertical) desde el MP4… puede tomar 1-2 minutos. La descarga arranca cuando esté listo (no cierres la pestaña)."
              : "Generando Master ProRes desde el MP4… puede tomar 1-2 minutos. La descarga arranca cuando esté listo (no cierres la pestaña)."}
          </div>
        </div>
      )}

      {/* Validation failed detail */}
      {isValidationFailed && job.error && (
        <div className="mb-6 rounded-card bg-red-500/[0.06] ring-1 ring-red-500/20 px-5 py-4">
          <p className="text-sm font-semibold text-red-300 mb-1">{t("detail.validation_issues") || "Problemas de política de contenido detectados"}</p>
          <p className="text-xs text-red-400/70 mb-3">{job.error}</p>
          <div className="px-3 py-2 rounded-xl bg-accent/[0.06] ring-1 ring-accent/20 mb-3">
            <p className="text-[11px] text-accent">
              {t("detail.validation_no_quota") || "Este video NO consume tu cuota mensual — solo los aprobados cuentan."}
            </p>
          </div>
          <div className="flex gap-2">
            <button
              onClick={handleRetry}
              disabled={retrying}
              className="btn-primary text-xs h-9 px-4 disabled:opacity-50"
            >
              {retrying ? (
                <><div className="inline-block w-3 h-3 border-2 border-white border-t-transparent rounded-full animate-spin mr-1.5" />Reintentando…</>
              ) : (
                t("detail.retry") || "Reintentar sin re-subir"
              )}
            </button>
            <button onClick={() => onBack && onBack()} className="btn-secondary text-xs h-9 px-4">
              {t("detail.upload_again") || "Subir nuevo archivo"}
            </button>
          </div>
        </div>
      )}

      {/* Tabs — pill style matching the rest of the app */}
      <div className="flex flex-wrap gap-2 mb-6">
        {ALL_TABS.map((tab) => (
          <button
            key={tab.key}
            onClick={() => setActiveTab(tab.key)}
            className={`h-9 px-4 rounded-full text-xs font-medium transition-all ${
              activeTab === tab.key
                ? "bg-brand/15 text-brand-light ring-1 ring-brand/40"
                : "bg-surface-2/40 text-ink-secondary ring-1 ring-white/[0.04] hover:ring-white/[0.08] hover:text-white"
            }`}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {/* Provenance tab */}
      {activeTab === "provenance" && (
        <div className="rounded-card bg-surface-2/40 ring-1 ring-white/[0.04] p-6 mb-6">
          <ProvenanceTab jobId={job.job_id} t={t} />
        </div>
      )}

      {/* UMG master tab — non-previewable, download-only panel */}
      {activeTab === "umg_master" && canPreview && (
        <div className="rounded-card bg-surface-2/40 ring-1 ring-white/[0.04] p-8 mb-6 text-center">
          <div className="w-14 h-14 mx-auto mb-4 rounded-2xl bg-brand/10 ring-1 ring-brand/25 flex items-center justify-center">
            <svg className="w-7 h-7 text-brand-light" fill="none" stroke="currentColor" strokeWidth="1.6" viewBox="0 0 24 24">
              <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4M7 10l5 5 5-5M12 15V3" strokeLinecap="round" strokeLinejoin="round"/>
            </svg>
          </div>
          <h3 className="text-base font-semibold text-white mb-1.5">
            {t("detail.umg_master_title") || "Máster ProRes 422 HQ"}
          </h3>
          <p className="text-xs text-ink-secondary mb-1">
            1920×1080 · 24 fps · BT.709 · pcm_s24le · QuickTime .mov
          </p>
          <p className="text-[11px] text-gray-600 mb-5">
            {t("detail.umg_master_subtitle") || "ProRes no se reproduce en el navegador. Descargá el archivo para reproducirlo en QuickTime / DaVinci / Premiere."}
          </p>
          {canDownload ? (
            <button
              onClick={downloadProResMaster}
              className="inline-flex items-center gap-2 btn-primary text-sm h-11 px-5"
            >
              <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4" /><polyline points="7 10 12 15 17 10" /><line x1="12" y1="15" x2="12" y2="3" />
              </svg>
              {t("detail.download_master") || "Descargar máster"}
            </button>
          ) : (
            <p className="text-[11px] text-amber-300/90">
              {t("detail.master_pending_approval") || "Aprobá el video para habilitar la descarga."}
            </p>
          )}
        </div>
      )}

      {/* Media preview (video / short / thumbnail) */}
      {activeTab !== "provenance" && activeTab !== "umg_master" && canPreview && (
        <>
          <div
            data-tour="jobdetail-preview"
            className="rounded-card bg-surface-2/40 ring-1 ring-white/[0.04] overflow-hidden mb-4"
          >
            {activeTab === "thumbnail" ? (
              previewSrc ? (
                <img
                  src={previewSrc}
                  alt="Thumbnail"
                  className="w-full max-h-[500px] object-contain bg-black/40"
                />
              ) : (
                <div className="w-full h-[500px] bg-black/40" />
              )
            ) : (
              previewSrc ? (
                <video
                  key={activeTab}
                  src={previewSrc}
                  controls
                  className={`w-full bg-black/40 ${
                    activeTab === "short" ? "max-h-[600px] mx-auto" : "max-h-[500px]"
                  }`}
                  style={activeTab === "short" ? { maxWidth: "340px", margin: "0 auto", display: "block" } : {}}
                />
              ) : (
                <div className="w-full h-[500px] bg-black/40" />
              )
            )}
          </div>

          {/* File info */}
          <div className="flex items-center justify-between mb-6">
            <p className="text-xs text-gray-500">
              {MEDIA_TABS.find((t) => t.key === activeTab)?.desc}
              {activeTab !== "thumbnail" ? " MP4" : " JPG"}
            </p>
            {canDownload && downloadHref && (
              <a href={downloadHref} download
                className="text-xs font-medium text-brand hover:text-brand-light transition-colors flex items-center gap-1.5">
                <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                  <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4" /><polyline points="7 10 12 15 17 10" /><line x1="12" y1="15" x2="12" y2="3" />
                </svg>
                {t("detail.download")} {MEDIA_TABS.find((tb) => tb.key === activeTab)?.label}
              </a>
            )}
          </div>
        </>
      )}

      {/* The dedicated full-page editing UI lives in the early return at
          the top of the component — by the time we get down here, status
          is pending_review or done, so no editing overlay needed. */}

      {/* Edit request panel for pending_review (above approve) */}
      {isPendingReview && (
        <EditRequestPanel job={job} onEditTriggered={handleEditTriggered} />
      )}

      {/* Approval panel for pending_review */}
      {isPendingReview && (
        <div
          data-tour="jobdetail-approve-panel"
          className="rounded-card p-6 mb-6 animate-fade-in bg-gradient-to-br from-brand/[0.08] via-brand/[0.04] to-transparent ring-1 ring-brand/25"
        >
          <div className="flex items-center gap-2 mb-1.5">
            <svg className="w-4 h-4 text-brand-light" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
              <path d="M9 11l3 3L22 4M21 12v7a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2h11" strokeLinecap="round" strokeLinejoin="round"/>
            </svg>
            <h3 className="text-sm font-semibold tracking-tight">
              {t("review.title") || "Revisar y aprobar"}
            </h3>
          </div>
          <p className="text-xs text-ink-secondary mb-4">
            {t("review.description") || "Revisá el video generado antes de habilitar la descarga y publicación."}
          </p>
          <div className="px-3 py-2 rounded-xl bg-accent/[0.06] ring-1 ring-accent/20 mb-4">
            <p className="text-[11px] text-accent">
              {t("review.reject_free") || "Rechazar es gratis — solo los videos aprobados cuentan en tu cuota mensual."}
            </p>
          </div>
          <textarea
            value={reviewNotes}
            onChange={(e) => setReviewNotes(e.target.value)}
            placeholder={t("review.notes_placeholder") || "Notas (opcional)…"}
            className="input-field text-sm mb-4 resize-none"
            rows="2"
          />
          <div className="flex flex-wrap gap-3">
            <button
              onClick={handleApprove}
              disabled={approving}
              className="inline-flex items-center justify-center h-12 px-6 rounded-button text-sm font-semibold text-white bg-accent hover:bg-accent/90 disabled:opacity-50 transition-colors"
            >
              {approving ? (
                <div className="inline-block w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin mr-2" />
              ) : (
                <svg className="inline-block w-4 h-4 mr-1.5" fill="none" stroke="currentColor" strokeWidth="2.5" viewBox="0 0 24 24">
                  <polyline points="20 6 9 17 4 12" />
                </svg>
              )}
              {t("review.approve") || "Aprobar"}
            </button>
            <button
              onClick={handleReject}
              disabled={approving}
              className="btn-secondary h-12 px-6 disabled:opacity-50 !text-red-300 hover:!text-red-200"
            >
              <svg className="inline-block w-4 h-4 mr-1.5 -mt-0.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                <path d="M18 6L6 18M6 6l12 12" />
              </svg>
              {t("review.reject") || "Rechazar"}
            </button>
          </div>
        </div>
      )}

      {/* Exportar a ProRes — para jobs MP4-only cuyo tenant tiene
          prores_export habilitado. Persiste umg_spec retroactivo y
          dispara el transcoding lazy. */}
      {canEnableProRes && (
        <div className="rounded-card bg-surface-2/40 ring-1 ring-white/[0.04] p-5 mb-4 flex items-start gap-4">
          <div className="w-10 h-10 shrink-0 rounded-xl bg-brand/10 ring-1 ring-brand/30 flex items-center justify-center text-brand">
            <svg className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
              <path d="M12 4v12m0 0l-4-4m4 4l4-4M4 20h16" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </div>
          <div className="flex-1 min-w-0">
            <div className="text-sm font-semibold text-white">
              {t("prores.cta_title") || "Exportar a ProRes (.mov broadcast)"}
            </div>
            <div className="text-xs text-gray-400 mt-0.5">
              {t("prores.cta_desc") ||
                "Este video se rindió como MP4. Generá una versión ProRes para broadcast / cliente."}
            </div>
            {proResToast && (
              <div className="mt-2 text-xs text-accent">
                {proResToast}
              </div>
            )}
          </div>
          <button
            type="button"
            onClick={() => setShowProResModal(true)}
            className="shrink-0 px-4 py-2 rounded-md text-sm font-medium text-white bg-brand hover:bg-brand-strong ring-1 ring-brand/30 transition-colors"
          >
            {t("prores.cta_button") || "Exportar"}
          </button>
        </div>
      )}

      {showProResModal && (
        <EnableProResModal
          jobId={job.job_id}
          onClose={() => setShowProResModal(false)}
          onSuccess={(data) => {
            setShowProResModal(false);
            setProResToast(
              t("prores.queued_toast") ||
                "ProRes encolado. En 1-5 min va a estar disponible para descargar.",
            );
            // Trigger un refresh del job en el próximo tick para que
            // isUmgJob flipee a true (gracias al umg_spec recién
            // persistido) y aparezca el tab de Máster ProRes.
            onJobUpdate?.({ ...job, umg_spec: data.umg_spec });
          }}
        />
      )}

      {/* Guardar en Drive — botón visible cuando el job está done y el
          user tiene Drive conectado. El flow R2 → Drive server-to-server
          es ~30x más rápido que descargar+subir desde casa para ProRes
          de 16 GB. Si el user no tiene Drive conectado, en Settings está
          el botón Conectar. */}
      {isJobDone && driveFeatureEnabled && driveConnected && (
        <div className="rounded-card bg-surface-2/40 ring-1 ring-white/[0.04] p-5 mb-4 flex items-start gap-4">
          <div className="w-10 h-10 shrink-0 rounded-xl bg-accent/10 ring-1 ring-accent/30 flex items-center justify-center text-accent">
            <svg className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
              <path d="M3 8l3-5h12l3 5M3 8v11a2 2 0 002 2h14a2 2 0 002-2V8M3 8h18M12 12v6" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </div>
          <div className="flex-1 min-w-0">
            <div className="text-sm font-semibold text-white">
              {t("drive.cta_title")}
            </div>
            <div className="text-xs text-gray-400 mt-0.5">
              {t("drive.cta_desc")}
            </div>
          </div>
          <button
            type="button"
            onClick={() => setShowDriveModal(true)}
            className="shrink-0 px-4 py-2 rounded-md text-sm font-medium text-white bg-accent hover:bg-accent/90 ring-1 ring-accent/30 transition-colors"
          >
            {t("drive.cta_button")}
          </button>
        </div>
      )}

      {showDriveModal && (
        <DriveTransferModal
          jobId={job.job_id}
          fileType={driveFileType}
          onClose={() => setShowDriveModal(false)}
        />
      )}

      {/* YouTube Panel (only for approved/done jobs) */}
      {canDownload && showYoutubePanel && (
        <div className="rounded-card bg-surface-2/40 ring-1 ring-white/[0.04] p-6 animate-fade-in">
          <h3 className="font-semibold mb-4 flex items-center gap-2">
            <svg className="w-5 h-5 text-red-500" fill="currentColor" viewBox="0 0 24 24">
              <path d="M22.54 6.42a2.78 2.78 0 00-1.94-2C18.88 4 12 4 12 4s-6.88 0-8.6.46a2.78 2.78 0 00-1.94 2A29 29 0 001 11.75a29 29 0 00.46 5.33A2.78 2.78 0 003.4 19.13C5.12 19.56 12 19.56 12 19.56s6.88 0 8.6-.46a2.78 2.78 0 001.94-2A29 29 0 0023 11.75a29 29 0 00-.46-5.33z"/><polygon points="9.75 15.02 15.5 11.75 9.75 8.48 9.75 15.02" fill="white"/>
            </svg>
            {t("detail.publish_youtube")}
          </h3>

          {!metadataPreview && !youtubeResult && (
            <div className="flex items-center justify-center py-8">
              <div className="w-6 h-6 border-2 border-brand border-t-transparent rounded-full animate-spin" />
              <span className="ml-3 text-sm text-gray-400">{t("detail.generating_meta")}</span>
            </div>
          )}

          {metadataPreview && !metadataPreview.error && !youtubeResult && (
            <div className="space-y-4">
              <div>
                <label className="text-xs text-gray-500 uppercase tracking-wider">{t("settings.title_format").split(" ")[0]}</label>
                <p className="text-sm text-white mt-1 glass rounded-xl px-4 py-2.5">{metadataPreview.title}</p>
              </div>
              <div>
                <label className="text-xs text-gray-500 uppercase tracking-wider">{t("settings.desc_header").split(" ")[0]}</label>
                <p className="text-sm text-gray-300 mt-1 glass rounded-xl px-4 py-2.5 whitespace-pre-line">{metadataPreview.description}</p>
              </div>
              <div>
                <label className="text-xs text-gray-500 uppercase tracking-wider">Tags</label>
                <div className="flex flex-wrap gap-1.5 mt-1">
                  {(metadataPreview.tags || []).map((tag, i) => (
                    <span key={i} className="px-2 py-1 rounded-lg bg-surface-3/50 text-xs text-gray-400">{tag}</span>
                  ))}
                </div>
              </div>

              <div className="flex gap-3 pt-2">
                <button onClick={() => uploadToYoutube("unlisted")} disabled={uploading}
                  className="btn-primary text-sm py-2.5 px-5 disabled:opacity-50">
                  {uploading ? (
                    <><div className="inline-block w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin mr-2" />{t("detail.uploading")}</>
                  ) : (
                    t("detail.upload_unlisted")
                  )}
                </button>
                <button onClick={() => uploadToYoutube("public")} disabled={uploading}
                  className="btn-secondary text-sm py-2.5 px-5 disabled:opacity-50">
                  {t("detail.upload_public")}
                </button>
                <button onClick={() => setShowYoutubePanel(false)}
                  className="text-xs text-gray-500 hover:text-white transition-colors ml-auto">
                  {t("detail.cancel")}
                </button>
              </div>
            </div>
          )}

          {youtubeResult && !youtubeResult.error && (
            <div className="text-center py-6">
              <div className="w-12 h-12 mx-auto mb-3 rounded-2xl bg-accent/10 flex items-center justify-center">
                <svg className="w-6 h-6 text-accent" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                  <polyline points="20 6 9 17 4 12" />
                </svg>
              </div>
              <p className="text-sm font-medium text-white mb-1">{t("detail.published")}</p>
              <a href={youtubeResult.url} target="_blank" rel="noopener noreferrer"
                className="text-sm text-brand hover:text-brand-light transition-colors underline">
                {youtubeResult.url}
              </a>
              <p className="text-xs text-gray-500 mt-2">Estado: {youtubeResult.privacy}</p>
            </div>
          )}

          {(metadataPreview?.error || youtubeResult?.error) && (
            <div className="rounded-xl bg-red-500/10 border border-red-500/20 px-4 py-3 text-center">
              <p className="text-sm text-red-400">{metadataPreview?.error || youtubeResult?.error}</p>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
