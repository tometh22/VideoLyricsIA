import { useEffect, useRef, useState } from "react";
import { useI18n } from "../i18n";
import { getDownloadUrl, useMediaUrl } from "../mediaUrl";

const API = import.meta.env.VITE_API_URL || "";

async function triggerDownload(jobId, type) {
  try {
    const href = await getDownloadUrl(jobId, type);
    const a = document.createElement("a");
    a.href = href;
    a.download = "";
    a.click();
  } catch {}
}

// ─── Processing list row (shown while jobs are still running) ───────────────
function JobRow({ job, index, t, onSelectJob }) {
  const { filename, status, current_step, progress, job_id, error,
          queue_reason, queue_retry_in_s } = job;
  const name = filename.replace(/\.(mp3|wav)$/i, "");
  const isClickable = (status === "pending_review" || status === "done") && job_id && onSelectJob;

  const STEP_LABELS = {
    uploading: t("batch.step_uploading") || "Subiendo",
    whisper: t("transcribe.title").split(" ")[0] || "Transcribiendo",
    background: t("batch.in_progress"),
    video: t("batch.generating").split(" ")[0] || "Generando",
    short: "Short",
    thumbnail: "Thumbnail",
    validation: t("batch.validating") || "Validando",
  };

  const queueLabel = (() => {
    if (status !== "queued" || !queue_reason) return null;
    if (queue_reason === "team_backlog") return t("batch.queue_team_backlog") || "Esperando lugar en el equipo…";
    if (queue_reason === "server_busy") return t("batch.queue_server_busy") || `Servidor saturado, reintentamos en ~${queue_retry_in_s || 60}s`;
    if (queue_reason === "rate_limit") return t("batch.queue_rate_limit") || "Subiendo… reintentamos en unos segundos";
    return null;
  })();

  return (
    <div
      onClick={isClickable ? () => onSelectJob(job_id) : undefined}
      className={`glass rounded-card p-4 transition-all duration-300 ${
        status === "processing" ? "border border-brand/20" : ""
      } ${isClickable ? "cursor-pointer hover:bg-white/[0.02] hover:ring-1 hover:ring-brand/20" : ""}`}
    >
      <div className="flex items-center gap-3 mb-3">
        <div className={`w-9 h-9 rounded-xl flex items-center justify-center shrink-0 ${
          status === "done" ? "bg-accent/10" :
          status === "pending_review" ? "bg-amber-500/10" :
          status === "error" || status === "validation_failed" ? "bg-red-500/10" :
          status === "processing" ? "bg-brand/10" :
          "bg-surface-3/50"
        }`}>
          {status === "done" && <svg className="w-4.5 h-4.5 text-accent" fill="none" stroke="currentColor" strokeWidth="2.5" viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12" /></svg>}
          {status === "pending_review" && <svg className="w-4.5 h-4.5 text-amber-400" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10" /><path d="M12 8v4M12 16h.01" /></svg>}
          {(status === "error" || status === "validation_failed") && <svg className="w-4.5 h-4.5 text-red-400" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12" /></svg>}
          {status === "processing" && <div className="w-4 h-4 border-2 border-brand border-t-transparent rounded-full animate-spin" />}
          {status === "queued" && <span className="text-xs font-bold text-gray-500">{index + 1}</span>}
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5 min-w-0">
            <p className="text-sm font-medium text-white truncate">{name}</p>
            {isClickable && (
              <svg className="w-3.5 h-3.5 text-brand/60 shrink-0" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                <path d="M9 18l6-6-6-6" />
              </svg>
            )}
          </div>
          <p className={`text-[11px] ${queueLabel ? "text-amber-300/80" : "text-gray-500"}`}>
            {status === "done" ? t("dash.completed") :
             status === "pending_review" ? (t("batch.pending_review") || "Pendiente de aprobación") :
             status === "validation_failed" ? (t("batch.validation_failed") || "Validación fallida") :
             status === "error" ? (error || t("dash.error")) :
             status === "processing" ? (
               current_step === "uploading"
                 ? `${STEP_LABELS.uploading} ${progress || 0}%`
                 : STEP_LABELS[current_step] || current_step
             ) :
             queueLabel ? queueLabel :
             t("batch.queued")}
          </p>
        </div>
        {status === "done" && job_id && (
          <div className="flex gap-1.5 shrink-0">
            {["video", "short", "thumbnail"].map((type) => (
              <button
                key={type}
                onClick={(e) => { e.stopPropagation(); triggerDownload(job_id, type); }}
                className="w-8 h-8 rounded-lg bg-surface-1 hover:bg-brand/10 flex items-center justify-center text-gray-400 hover:text-brand transition-colors"
                title={type === "video" ? "Lyric Video" : type === "short" ? "Short" : "Thumbnail"}
              >
                {type === "video" && <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24"><rect x="2" y="2" width="20" height="20" rx="2" /><path d="M10 8l6 4-6 4V8z" /></svg>}
                {type === "short" && <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24"><rect x="5" y="2" width="14" height="20" rx="2" /><line x1="12" y1="18" x2="12.01" y2="18" /></svg>}
                {type === "thumbnail" && <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="2" /><circle cx="8.5" cy="8.5" r="1.5" /><polyline points="21 15 16 10 5 21" /></svg>}
              </button>
            ))}
          </div>
        )}
      </div>
      {status === "processing" && (
        <div className="w-full h-1.5 bg-surface-1 rounded-full overflow-hidden">
          <div className="h-full rounded-full bg-gradient-to-r from-brand to-brand-light transition-all duration-700" style={{ width: `${progress}%` }} />
        </div>
      )}
      {status === "error" && error && (
        <div className="mt-2 px-3 py-2 rounded-lg bg-red-500/5 border border-red-500/10">
          <p className="text-[11px] text-red-400/80">{error}</p>
        </div>
      )}
    </div>
  );
}

// ─── Approval grid card (shown when all done, REQUIRE_REVIEW=true) ───────────
function ApprovalCard({ job, t, onSelectJob, onApprove, approving }) {
  const name = (job.filename || "").replace(/\.(mp3|wav)$/i, "");
  const thumbSrc = useMediaUrl(job.job_id, "thumbnail", "preview");
  const isDone = job.status === "done";
  const isPending = job.status === "pending_review";

  return (
    <div
      className={`rounded-card overflow-hidden ring-1 transition-all duration-300 ${
        isDone
          ? "ring-accent/40 bg-accent/[0.04]"
          : isPending
          ? "ring-white/[0.06] bg-surface-2/40"
          : "ring-red-500/20 bg-red-500/[0.04]"
      }`}
    >
      {/* Thumbnail */}
      <div
        className="aspect-video bg-black/40 relative cursor-pointer group overflow-hidden"
        onClick={() => job.job_id && onSelectJob?.(job.job_id)}
      >
        {thumbSrc ? (
          <img
            src={thumbSrc}
            alt={name}
            className="w-full h-full object-cover group-hover:scale-[1.03] transition-transform duration-500"
          />
        ) : (
          <div className="w-full h-full flex items-center justify-center">
            <div className="w-6 h-6 border-2 border-white/20 border-t-white/60 rounded-full animate-spin" />
          </div>
        )}

        {/* Status overlay */}
        <div className="absolute inset-0 bg-gradient-to-t from-black/70 via-transparent to-transparent" />
        <div className="absolute bottom-2 left-2 right-2 flex items-end justify-between">
          <p className="text-xs font-semibold text-white truncate">{name}</p>
          {isDone && (
            <span className="shrink-0 ml-2 inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-accent text-white text-[10px] font-semibold">
              <svg className="w-2.5 h-2.5" fill="none" stroke="currentColor" strokeWidth="3" viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12" /></svg>
              {t("detail.approved") || "Aprobado"}
            </span>
          )}
          {isPending && (
            <span className="shrink-0 ml-2 inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-amber-500/80 text-white text-[10px] font-semibold">
              {t("batch.pending_review") || "Pendiente"}
            </span>
          )}
        </div>

        {/* Play hint on hover */}
        {thumbSrc && (
          <div className="absolute inset-0 flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity duration-200">
            <div className="w-10 h-10 rounded-full bg-black/50 backdrop-blur-sm flex items-center justify-center">
              <svg className="w-5 h-5 text-white ml-0.5" fill="currentColor" viewBox="0 0 24 24"><path d="M8 5v14l11-7z" /></svg>
            </div>
          </div>
        )}
      </div>

      {/* Card footer */}
      <div className="px-3 py-2.5">
        <p className="text-[11px] text-gray-400 truncate mb-2">{job.artist}</p>
        {isPending && (
          <div className="flex gap-1.5">
            <button
              onClick={() => onApprove?.(job.job_id)}
              disabled={approving}
              className="flex-1 inline-flex items-center justify-center gap-1 h-7 rounded-lg bg-accent/15 text-accent ring-1 ring-accent/30 text-[11px] font-semibold hover:bg-accent/25 transition-colors disabled:opacity-40"
            >
              {approving ? (
                <div className="w-3 h-3 border-2 border-accent border-t-transparent rounded-full animate-spin" />
              ) : (
                <>
                  <svg className="w-3 h-3" fill="none" stroke="currentColor" strokeWidth="2.5" viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12" /></svg>
                  {t("review.approve") || "Aprobar"}
                </>
              )}
            </button>
            <button
              onClick={() => job.job_id && onSelectJob?.(job.job_id)}
              className="h-7 px-2 rounded-lg text-gray-400 ring-1 ring-white/[0.06] text-[11px] hover:text-white hover:ring-white/[0.12] transition-colors"
            >
              {t("detail.back") ? "Ver" : "Ver"}
            </button>
          </div>
        )}
        {isDone && (
          <button
            onClick={() => triggerDownload(job.job_id, "video")}
            className="w-full inline-flex items-center justify-center gap-1 h-7 rounded-lg text-accent/80 ring-1 ring-accent/20 text-[11px] hover:bg-accent/10 transition-colors"
          >
            <svg className="w-3 h-3" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4" /><polyline points="7 10 12 15 17 10" /><line x1="12" y1="15" x2="12" y2="3" /></svg>
            {t("detail.download") || "Descargar"}
          </button>
        )}
      </div>
    </div>
  );
}

// ─── Celebration screen ──────────────────────────────────────────────────────

// Simple CSS-only particles — no library. Each particle is a tiny coloured
// circle that flies out from the center using a keyframe defined inline via
// style.animationName. We generate the keyframes once and inject them into
// a <style> tag rendered inside the component.
const PARTICLE_COLORS = ["#8b5cf6", "#06b6d4", "#10b981", "#f59e0b", "#ec4899", "#fff"];
const PARTICLES = Array.from({ length: 24 }, (_, i) => {
  const angle = (i / 24) * 360;
  const dist = 80 + Math.random() * 80;
  const dx = Math.cos((angle * Math.PI) / 180) * dist;
  const dy = Math.sin((angle * Math.PI) / 180) * dist;
  return {
    id: i,
    color: PARTICLE_COLORS[i % PARTICLE_COLORS.length],
    dx,
    dy,
    size: 4 + Math.random() * 5,
    delay: Math.random() * 400,
    duration: 700 + Math.random() * 500,
  };
});

function CelebrationScreen({ jobs, total, downloadable, onDownloadAll, onReset, t }) {
  const [show, setShow] = useState(false);
  const [burst, setBurst] = useState(false);

  // Count actual downloadable files across all done jobs.
  const fileCount = jobs
    .filter((j) => j.status === "done")
    .reduce((acc, j) => {
      const f = j.files || {};
      return acc + [f.video_url, f.short_url, f.thumbnail_url].filter(Boolean).length;
    }, 0) || downloadable * 3;

  // Animate in; trigger particle burst slightly after mount.
  useEffect(() => {
    const t1 = setTimeout(() => setShow(true), 80);
    const t2 = setTimeout(() => setBurst(true), 200);
    return () => { clearTimeout(t1); clearTimeout(t2); };
  }, []);

  // Total batch processing time (earliest created_at → latest completed_at).
  // Timestamps are Unix epoch floats from the backend's .timestamp() call.
  const batchDurationLabel = (() => {
    const completedJobs = jobs.filter((j) => j.completed_at && j.created_at);
    if (completedJobs.length === 0) return null;
    const toMs = (v) => v > 1e10 ? v : v * 1000; // handle both ms and s
    const start = Math.min(...completedJobs.map((j) => toMs(j.created_at)));
    const end = Math.max(...completedJobs.map((j) => toMs(j.completed_at)));
    const mins = Math.round((end - start) / 60000);
    return mins >= 1 ? `${mins} min` : "<1 min";
  })();

  return (
    <div className={`text-center transition-all duration-700 ${show ? "opacity-100 translate-y-0" : "opacity-0 translate-y-4"}`}>
      {/* Particle keyframe — injected once, scoped to this component's lifetime */}
      <style>{`
        @keyframes particle-fly {
          0%   { transform: translate(-50%, -50%) scale(1); opacity: 1; }
          80%  { opacity: 0.6; }
          100% { transform: translate(calc(-50% + var(--dx)), calc(-50% + var(--dy))) scale(0); opacity: 0; }
        }
      `}</style>
      {/* Glowing icon + particle burst */}
      <div className="relative w-20 h-20 mx-auto mb-6">
        {/* Particles */}
        {burst && PARTICLES.map((p) => (
          <span
            key={p.id}
            className="absolute rounded-full pointer-events-none"
            style={{
              width: p.size,
              height: p.size,
              background: p.color,
              top: "50%",
              left: "50%",
              transform: "translate(-50%, -50%)",
              animation: `particle-fly ${p.duration}ms ease-out ${p.delay}ms both`,
              // Inline keyframe via CSS custom properties — avoids needing a
              // global stylesheet or an animation library.
              "--dx": `${p.dx}px`,
              "--dy": `${p.dy}px`,
            }}
          />
        ))}
        <div className="absolute inset-0 rounded-3xl bg-accent/20 animate-ping" style={{ animationDuration: "2s" }} />
        <div className="absolute inset-0 rounded-3xl bg-accent/10 animate-ping" style={{ animationDuration: "2.5s", animationDelay: "0.3s" }} />
        <div className="relative w-20 h-20 rounded-3xl bg-gradient-to-br from-accent/30 to-brand/30 ring-1 ring-accent/40 flex items-center justify-center">
          <svg className="w-10 h-10 text-accent" fill="none" stroke="currentColor" strokeWidth="1.8" viewBox="0 0 24 24">
            <path d="M22 11.08V12a10 10 0 11-5.93-9.14" strokeLinecap="round" strokeLinejoin="round"/>
            <polyline points="22 4 12 14.01 9 11.01" strokeLinecap="round" strokeLinejoin="round"/>
          </svg>
        </div>
      </div>

      <h2 className="text-3xl font-bold tracking-tight mb-2">
        {total === 1
          ? (t("batch.celebration_single") || "¡Video listo!")
          : (t("batch.celebration_batch") || `¡${total} videos aprobados!`)}
      </h2>
      <p className="text-gray-400 mb-6">
        {total === 1
          ? (t("batch.celebration_sub_single") || "Tu lyric video está aprobado y listo para descargar.")
          : (t("batch.celebration_sub_batch") || "Todos los videos están aprobados y listos para descargar.")}
      </p>

      {/* Stats chips */}
      <div className="flex items-center justify-center gap-3 mb-8 flex-wrap">
        <div className="px-3 py-1.5 rounded-full bg-surface-2/60 ring-1 ring-white/[0.06] text-xs text-gray-400">
          <span className="font-semibold text-white">{downloadable}</span> {downloadable === 1 ? "video" : "videos"}
        </div>
        <div className="px-3 py-1.5 rounded-full bg-surface-2/60 ring-1 ring-white/[0.06] text-xs text-gray-400">
          <span className="font-semibold text-white">{fileCount}</span> archivos
        </div>
        {batchDurationLabel && (
          <div className="px-3 py-1.5 rounded-full bg-surface-2/60 ring-1 ring-white/[0.06] text-xs text-gray-400">
            generado en <span className="font-semibold text-white">{batchDurationLabel}</span>
          </div>
        )}
      </div>

      {/* Actions */}
      <div className="flex gap-3 justify-center">
        <button onClick={onDownloadAll} className="btn-primary h-12 px-7 text-sm">
          <svg className="inline-block w-4 h-4 mr-2 -mt-0.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
            <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4" /><polyline points="7 10 12 15 17 10" /><line x1="12" y1="15" x2="12" y2="3" />
          </svg>
          {t("batch.download_all") || "Descargar todo"} ({fileCount} archivos)
        </button>
        <button onClick={onReset} className="btn-secondary h-12 px-6 text-sm">
          {t("batch.new_batch") || "Nuevo batch"}
        </button>
      </div>
    </div>
  );
}

// ─── Main component ──────────────────────────────────────────────────────────
export default function BatchProgress({ jobs, onReset, onSingleDone, onSelectJob, onBulkApprove }) {
  const { t } = useI18n();
  const [bulkApproving, setBulkApproving] = useState(false);
  // Per-card approving state: Set of job_ids currently being approved.
  const [approvingIds, setApprovingIds] = useState(new Set());

  const done = jobs.filter((j) => j.status === "done" || j.status === "pending_review").length;
  const downloadable = jobs.filter((j) => j.status === "done").length;
  const total = jobs.length;
  const allDone = done === total && !jobs.some((j) => j.status === "processing" || j.status === "queued");
  const allApproved = downloadable === total && !jobs.some((j) => j.status === "processing" || j.status === "queued") && total > 0;
  const hasPendingReview = jobs.some((j) => j.status === "pending_review");
  const hasErrors = jobs.some((j) => j.status === "error" || j.status === "validation_failed");
  const isSingle = total === 1;
  const pendingReviewIds = jobs.filter((j) => j.status === "pending_review" && j.job_id).map((j) => j.job_id);

  // Show the approval grid when all done and there are multi-video batches pending.
  const showApprovalGrid = allDone && !allApproved && total > 1;

  // Real ETA: average duration of completed jobs (Unix epoch float timestamps).
  const etaLabel = (() => {
    if (total - done <= 0) return null;
    const toMs = (v) => v > 1e10 ? v : v * 1000;
    const completedMs = jobs
      .filter((j) => (j.status === "done" || j.status === "pending_review") && j.completed_at && j.created_at)
      .map((j) => toMs(j.completed_at) - toMs(j.created_at));
    const avgMin = completedMs.length > 0
      ? completedMs.reduce((a, b) => a + b, 0) / completedMs.length / 60000
      : 8;
    const etaMin = Math.max(1, Math.ceil((total - done) * avgMin));
    return `~${etaMin} ${t("dash.min_remaining") || "min restantes"}`;
  })();

  // Single-song auto-redirect.
  useEffect(() => {
    if (!isSingle || !onSingleDone) return;
    const j = jobs[0];
    if (!j || !j.job_id) return;
    if (j.status === "pending_review" || j.status === "done") {
      onSingleDone(j.job_id);
    }
  }, [isSingle, jobs, onSingleDone]);

  const downloadAll = async () => {
    for (const job of jobs) {
      if (job.status !== "done" || !job.job_id) continue;
      for (const type of ["video", "short", "thumbnail"]) {
        await triggerDownload(job.job_id, type);
      }
    }
  };

  const handleBulkApprove = async () => {
    if (!onBulkApprove || pendingReviewIds.length === 0 || bulkApproving) return;
    setBulkApproving(true);
    await onBulkApprove(pendingReviewIds);
    setBulkApproving(false);
  };

  // Per-card individual approve (used in the approval grid).
  const handleApproveOne = async (jobId) => {
    if (!onBulkApprove || approvingIds.has(jobId)) return;
    setApprovingIds((prev) => new Set([...prev, jobId]));
    await onBulkApprove([jobId]);
    setApprovingIds((prev) => { const s = new Set(prev); s.delete(jobId); return s; });
  };

  // ── Celebration screen ────────────────────────────────────────────────────
  if (allApproved && !isSingle) {
    return (
      <div className="w-full max-w-xl mt-12 animate-fade-in">
        <CelebrationScreen
          jobs={jobs}
          total={total}
          downloadable={downloadable}
          onDownloadAll={downloadAll}
          onReset={onReset}
          t={t}
        />
      </div>
    );
  }

  // ── Approval grid (multi-video batch, all done, some pending) ─────────────
  if (showApprovalGrid) {
    return (
      <div className="w-full max-w-3xl mt-12 animate-fade-in">
        {/* Header */}
        <div className="text-center mb-8">
          <div className="w-14 h-14 mx-auto mb-4 rounded-2xl bg-amber-500/10 ring-1 ring-amber-500/20 flex items-center justify-center">
            <svg className="w-7 h-7 text-amber-400" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
              <path d="M9 11l3 3L22 4M21 12v7a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2h11" strokeLinecap="round" strokeLinejoin="round"/>
            </svg>
          </div>
          <h2 className="text-2xl font-bold mb-1">
            {t("batch.approval_title") || "Revisá y aprobá tus videos"}
          </h2>
          <p className="text-gray-500 text-sm">
            {pendingReviewIds.length === 0
              ? (t("batch.all_approved_sub") || "Todos aprobados. Podés descargarlos.")
              : (t("batch.approval_sub") || `${pendingReviewIds.length} de ${total} videos necesitan aprobación`)}
          </p>
        </div>

        {/* Approval bar */}
        {pendingReviewIds.length > 0 && (
          <div className="mb-6 flex items-center justify-between gap-4">
            <div className="flex-1">
              <div className="w-full h-1.5 bg-surface-2 rounded-full overflow-hidden">
                <div
                  className="h-full rounded-full bg-gradient-to-r from-accent to-brand transition-all duration-700"
                  style={{ width: `${(downloadable / total) * 100}%` }}
                />
              </div>
              <p className="text-[11px] text-gray-500 mt-1">{downloadable} de {total} aprobados</p>
            </div>
            {onBulkApprove && pendingReviewIds.length > 1 && (
              <button
                onClick={handleBulkApprove}
                disabled={bulkApproving}
                className="shrink-0 inline-flex items-center gap-1.5 h-9 px-4 rounded-lg bg-accent/15 text-accent ring-1 ring-accent/30 text-xs font-semibold hover:bg-accent/25 transition-colors disabled:opacity-50"
              >
                {bulkApproving ? (
                  <div className="w-3 h-3 border-2 border-accent border-t-transparent rounded-full animate-spin" />
                ) : (
                  <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2.5" viewBox="0 0 24 24">
                    <polyline points="20 6 9 17 4 12" />
                  </svg>
                )}
                {t("batch.approve_all") || "Aprobar todos"}
              </button>
            )}
          </div>
        )}

        {/* Thumbnail grid — cards stagger in for a polished entrance */}
        <div className="grid grid-cols-2 sm:grid-cols-3 gap-3 mb-8">
          {jobs.map((job, i) => (
            <div
              key={job.job_id || i}
              className="animate-fade-in"
              style={{ animationDelay: `${i * 60}ms`, animationFillMode: "both" }}
            >
              <ApprovalCard
                job={job}
                t={t}
                onSelectJob={onSelectJob}
                onApprove={handleApproveOne}
                approving={approvingIds.has(job.job_id)}
              />
            </div>
          ))}
        </div>

        <div className="flex gap-3 justify-center">
          {downloadable > 0 && (
            <button onClick={downloadAll} className="btn-secondary text-sm">
              <svg className="inline-block w-4 h-4 mr-1.5 -mt-0.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4" /><polyline points="7 10 12 15 17 10" /><line x1="12" y1="15" x2="12" y2="3" />
              </svg>
              {t("batch.download_approved") || "Descargar aprobados"} ({downloadable})
            </button>
          )}
          <button onClick={onReset} className="btn-secondary text-sm">{t("batch.new_batch") || "Nuevo batch"}</button>
        </div>
      </div>
    );
  }

  // ── Processing view (jobs still running) ──────────────────────────────────
  return (
    <div className="w-full max-w-xl mt-12 animate-fade-in">
      {/* Header */}
      <div className="text-center mb-8">
        {allDone ? (
          <>
            <div className="w-14 h-14 mx-auto mb-4 rounded-2xl bg-accent/10 flex items-center justify-center">
              <svg className="w-7 h-7 text-accent" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                <polyline points="20 6 9 17 4 12" />
              </svg>
            </div>
            <h2 className="text-2xl font-bold mb-1">
              {isSingle ? (t("batch.single_done") || "Video listo") : t("batch.completed")}
            </h2>
            <p className="text-gray-500 text-sm">
              {isSingle
                ? (t("batch.single_done_sub") || "Te llevamos a revisar y aprobar...")
                : `${total} videos ${t("batch.generated")}`}
            </p>
          </>
        ) : (
          <>
            <h2 className="text-2xl font-bold mb-1">
              {isSingle ? (t("batch.single_generating") || "Generando tu video") : t("batch.generating")}
            </h2>
            <p className="text-gray-500 text-sm">
              {isSingle
                ? (t("batch.single_generating_sub") || "Te avisamos cuando esté listo")
                : <>
                    {done} {t("dash.monthly_of")} {total} {t("batch.completed_of")}
                    {etaLabel && <span className="text-gray-600"> — {etaLabel}</span>}
                  </>}
            </p>
          </>
        )}
      </div>

      {/* Overall progress bar */}
      {!allDone && (
        <div className="mb-6">
          <div className="w-full h-2 bg-surface-2 rounded-full overflow-hidden">
            <div
              className="h-full rounded-full bg-gradient-to-r from-brand to-accent transition-all duration-500"
              style={{ width: `${(done / total) * 100}%` }}
            />
          </div>
        </div>
      )}

      {/* Job list */}
      <div className="space-y-2 mb-8">
        {jobs.map((job, i) => (
          <JobRow key={i} job={job} index={i} t={t} onSelectJob={onSelectJob} />
        ))}
      </div>

      {/* Single pending_review notice (single-song or mixed state) */}
      {allDone && hasPendingReview && !showApprovalGrid && (
        <div className="mb-4 rounded-2xl bg-amber-500/5 border border-amber-500/20 overflow-hidden">
          <div className="px-4 py-3 flex items-center justify-between gap-3">
            <p className="text-xs text-amber-300/90 flex-1">
              {pendingReviewIds.length === 1
                ? (t("batch.pending_review_notice_one") || "Un video espera tu aprobación. Hacé click en la card para revisarlo.")
                : (t("batch.pending_review_notice") || `${pendingReviewIds.length} videos esperan aprobación.`)}
            </p>
            {pendingReviewIds.length > 1 && onBulkApprove && (
              <button
                onClick={handleBulkApprove}
                disabled={bulkApproving}
                className="shrink-0 inline-flex items-center gap-1.5 h-8 px-3 rounded-lg bg-accent/15 text-accent ring-1 ring-accent/30 text-[11px] font-semibold hover:bg-accent/25 transition-colors disabled:opacity-50"
              >
                {bulkApproving
                  ? <div className="w-3 h-3 border-2 border-accent border-t-transparent rounded-full animate-spin" />
                  : <svg className="w-3 h-3" fill="none" stroke="currentColor" strokeWidth="2.5" viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12" /></svg>}
                {t("batch.approve_all") || "Aprobar todos"}
              </button>
            )}
          </div>
        </div>
      )}

      {/* Actions */}
      <div className="flex gap-3 justify-center">
        {allApproved && downloadable > 0 && (
          <button onClick={downloadAll} className="btn-primary">
            <svg className="inline-block w-4 h-4 mr-2 -mt-0.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
              <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4" /><polyline points="7 10 12 15 17 10" /><line x1="12" y1="15" x2="12" y2="3" />
            </svg>
            {t("batch.download_all")} ({downloadable * 3} archivos)
          </button>
        )}
        {(allDone || hasErrors) && (
          <button onClick={onReset} className="btn-secondary">
            {t("batch.new_batch")}
          </button>
        )}
      </div>
    </div>
  );
}
