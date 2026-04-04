import { useI18n } from "../i18n";

const API = "";

function JobRow({ job, index, t }) {
  const { filename, status, current_step, progress, job_id, error } = job;
  const name = filename.replace(/\.mp3$/i, "");

  const STEP_LABELS = {
    whisper: t("transcribe.title").split(" ")[0] || "Transcribiendo",
    background: t("batch.in_progress"),
    video: t("batch.generating").split(" ")[0] || "Generando",
    short: "Short",
    thumbnail: "Thumbnail",
  };

  return (
    <div className={`glass rounded-2xl p-4 transition-all duration-300 ${
      status === "processing" ? "border border-brand/20" : ""
    }`}>
      <div className="flex items-center gap-3 mb-3">
        {/* Status icon */}
        <div className={`w-9 h-9 rounded-xl flex items-center justify-center shrink-0 ${
          status === "done" ? "bg-accent/10" :
          status === "error" ? "bg-red-500/10" :
          status === "processing" ? "bg-brand/10" :
          "bg-surface-3/50"
        }`}>
          {status === "done" && (
            <svg className="w-4.5 h-4.5 text-accent" fill="none" stroke="currentColor" strokeWidth="2.5" viewBox="0 0 24 24">
              <polyline points="20 6 9 17 4 12" />
            </svg>
          )}
          {status === "error" && (
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
          <p className="text-[11px] text-gray-500">
            {status === "done" ? t("dash.completed") :
             status === "error" ? (error || t("dash.error")) :
             status === "processing" ? STEP_LABELS[current_step] || current_step :
             t("batch.queued")}
          </p>
        </div>

        {/* Download buttons for done jobs */}
        {status === "done" && job_id && (
          <div className="flex gap-1.5 shrink-0">
            {["video", "short", "thumbnail"].map((type) => (
              <a
                key={type}
                href={`${API}/download/${job_id}/${type}`}
                download
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
              </a>
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
    </div>
  );
}

export default function BatchProgress({ jobs, onReset }) {
  const { t } = useI18n();
  const done = jobs.filter((j) => j.status === "done").length;
  const total = jobs.length;
  const allDone = done === total;
  const hasErrors = jobs.some((j) => j.status === "error");

  const downloadAll = () => {
    jobs.forEach((job) => {
      if (job.status !== "done" || !job.job_id) return;
      ["video", "short", "thumbnail"].forEach((type) => {
        const a = document.createElement("a");
        a.href = `${API}/download/${job.job_id}/${type}`;
        a.download = "";
        a.click();
      });
    });
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
            <h2 className="text-2xl font-bold mb-1">{t("batch.completed")}</h2>
            <p className="text-gray-500 text-sm">{total} video{total > 1 ? "s" : ""} {t("batch.generated")}</p>
          </>
        ) : (
          <>
            <h2 className="text-2xl font-bold mb-1">{t("batch.generating")}</h2>
            <p className="text-gray-500 text-sm">
              {done} {t("dash.monthly_of")} {total} {t("batch.completed_of")}
              {total - done > 0 && <span className="text-gray-600"> — ~{(total - done) * 5} {t("dash.min_remaining")}</span>}
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

      {/* Actions */}
      <div className="flex gap-3 justify-center">
        {allDone && (
          <button onClick={downloadAll} className="btn-primary">
            <svg className="inline-block w-4 h-4 mr-2 -mt-0.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
              <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4" /><polyline points="7 10 12 15 17 10" /><line x1="12" y1="15" x2="12" y2="3" />
            </svg>
            {t("batch.download_all")} ({done * 3} archivos)
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
