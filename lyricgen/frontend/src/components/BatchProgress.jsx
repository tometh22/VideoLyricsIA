import { useEffect } from "react";
import { useI18n } from "../i18n";
import { getDownloadUrl } from "../mediaUrl";

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

function JobRow({ job, index, t }) {
  const { filename, status, current_step, progress, job_id, error,
          queue_reason, queue_retry_in_s } = job;
  const name = filename.replace(/\.mp3$/i, "");

  const STEP_LABELS = {
    // current_step="uploading" is set by processQueueDirect while the
    // browser PUTs to R2. `progress` carries the upload %.
    uploading: t("batch.step_uploading") || "Subiendo",
    whisper: t("transcribe.title").split(" ")[0] || "Transcribiendo",
    background: t("batch.in_progress"),
    video: t("batch.generating").split(" ")[0] || "Generando",
    short: "Short",
    thumbnail: "Thumbnail",
    validation: t("batch.validating") || "Validando",
  };

  // Friendly substatus when the upload is being held by capacity
  // pressure (rate-limit, tenant backlog, server disk). The user
  // never sees a red error for these — just "waiting" with the
  // reason. Auto-retry is invisible to them.
  const queueLabel = (() => {
    if (status !== "queued" || !queue_reason) return null;
    if (queue_reason === "team_backlog") {
      return t("batch.queue_team_backlog")
        || "Esperando que se libere un lugar en el equipo. Reintentamos solos en unos segundos.";
    }
    if (queue_reason === "server_busy") {
      return t("batch.queue_server_busy")
        || `Servidor saturado momentáneamente. Reintentamos automáticamente en ~${queue_retry_in_s || 60}s.`;
    }
    if (queue_reason === "rate_limit") {
      return t("batch.queue_rate_limit")
        || "Subiendo… reintentamos en unos segundos.";
    }
    return null;
  })();

  return (
    <div className={`glass rounded-card p-4 transition-all duration-300 ${
      status === "processing" ? "border border-brand/20" : ""
    }`}>
      <div className="flex items-center gap-3 mb-3">
        {/* Status icon */}
        <div className={`w-9 h-9 rounded-xl flex items-center justify-center shrink-0 ${
          status === "done" ? "bg-accent/10" :
          status === "pending_review" ? "bg-amber-500/10" :
          status === "error" || status === "validation_failed" ? "bg-red-500/10" :
          status === "processing" ? "bg-brand/10" :
          "bg-surface-3/50"
        }`}>
          {status === "done" && (
            <svg className="w-4.5 h-4.5 text-accent" fill="none" stroke="currentColor" strokeWidth="2.5" viewBox="0 0 24 24">
              <polyline points="20 6 9 17 4 12" />
            </svg>
          )}
          {status === "pending_review" && (
            <svg className="w-4.5 h-4.5 text-amber-400" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
              <circle cx="12" cy="12" r="10" /><path d="M12 8v4M12 16h.01" />
            </svg>
          )}
          {(status === "error" || status === "validation_failed") && (
            <svg className="w-4.5 h-4.5 text-red-400" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
              <path d="M18 6L6 18M6 6l12 12" />
            </svg>
          )}
          {status === "processing" && (
            <div className="w-4 h-4 border-2 border-brand border-t-transparent rounded-full animate-spin" />
          )}
          {status === "queued" && (
            <span className="text-xs font-bold text-gray-500">{index + 1}</span>
          )}
        </div>

        {/* File info */}
        <div className="min-w-0 flex-1">
          <p className="text-sm font-medium text-white truncate">{name}</p>
          <p className={`text-[11px] ${queueLabel ? "text-amber-300/80" : "text-gray-500"}`}>
            {status === "done" ? t("dash.completed") :
             status === "pending_review" ? (t("batch.pending_review") || "Pendiente de aprobacion") :
             status === "validation_failed" ? (t("batch.validation_failed") || "Validacion fallida") :
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

        {/* Download buttons for done jobs */}
        {status === "done" && job_id && (
          <div className="flex gap-1.5 shrink-0">
            {["video", "short", "thumbnail"].map((type) => (
              <button
                key={type}
                onClick={() => triggerDownload(job_id, type)}
                className="w-8 h-8 rounded-lg bg-surface-1 hover:bg-brand/10 flex items-center justify-center text-gray-400 hover:text-brand transition-colors"
                title={type === "video" ? "Lyric Video" : type === "short" ? "Short" : "Thumbnail"}
              >
                {type === "video" && (
                  <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24"><rect x="2" y="2" width="20" height="20" rx="2" /><path d="M10 8l6 4-6 4V8z" /></svg>
                )}
                {type === "short" && (
                  <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24"><rect x="5" y="2" width="14" height="20" rx="2" /><line x1="12" y1="18" x2="12.01" y2="18" /></svg>
                )}
                {type === "thumbnail" && (
                  <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="2" /><circle cx="8.5" cy="8.5" r="1.5" /><polyline points="21 15 16 10 5 21" /></svg>
                )}
              </button>
            ))}
          </div>
        )}
      </div>

      {/* Progress bar for active job */}
      {status === "processing" && (
        <div className="w-full h-1.5 bg-surface-1 rounded-full overflow-hidden">
          <div
            className="h-full rounded-full bg-gradient-to-r from-brand to-brand-light transition-all duration-700"
            style={{ width: `${progress}%` }}
          />
        </div>
      )}

      {/* Error detail */}
      {status === "error" && error && (
        <div className="mt-2 px-3 py-2 rounded-lg bg-red-500/5 border border-red-500/10">
          <p className="text-[11px] text-red-400/80">{error}</p>
        </div>
      )}
    </div>
  );
}

export default function BatchProgress({ jobs, onReset, onRetry, onSingleDone }) {
  const { t } = useI18n();
  // `done` includes pending_review for the BATCH PROGRESS view ("processing
  // is finished, awaiting your review"). But `downloadable` only counts jobs
  // that are actually approved — pending_review jobs are NOT downloadable
  // until the operator clicks Approve. Mixing the two created a bug where
  // the user could download a video from the batch screen before approving,
  // bypassing the review gate entirely (Tomi spotted this on 2026-05-05).
  const done = jobs.filter((j) => j.status === "done" || j.status === "pending_review").length;
  const downloadable = jobs.filter((j) => j.status === "done").length;
  const total = jobs.length;
  const allDone = done === total && !jobs.some((j) => j.status === "processing" || j.status === "queued");
  const allApproved = downloadable === total && !jobs.some((j) => j.status === "processing" || j.status === "queued");
  const hasPendingReview = jobs.some((j) => j.status === "pending_review");
  const hasErrors = jobs.some((j) => j.status === "error" || j.status === "validation_failed");
  const isSingle = total === 1;

  // Single-song flow: jump straight to JobDetail (review/approve) when the
  // one job lands in pending_review or done — no need to make the operator
  // navigate through History to find their video.
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
                    {total - done > 0 && <span className="text-gray-600"> — ~{(total - done) * 5} {t("dash.min_remaining")}</span>}
                  </>}
            </p>
          </>
        )}
      </div>

      {/* Overall progress */}
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
          <JobRow key={i} job={job} index={i} t={t} />
        ))}
      </div>

      {/* Pending-review banner — shown when batch finished processing but
          some videos still need approval before download. */}
      {allDone && hasPendingReview && (
        <div className="mb-4 px-4 py-3 rounded-2xl bg-amber-500/5 border border-amber-500/20">
          <p className="text-xs text-amber-300/90">
            {t("batch.pending_review_notice") ||
              "Algunos videos esperan tu aprobación antes de poder descargarlos. Hacé click en cada uno para revisarlo."}
          </p>
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
