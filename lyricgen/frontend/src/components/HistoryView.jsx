const API = "";

function timeAgo(ts) {
  if (!ts) return "";
  const diff = Date.now() / 1000 - ts;
  if (diff < 60) return "Ahora";
  if (diff < 3600) return `${Math.floor(diff / 60)} min`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h`;
  return `${Math.floor(diff / 86400)}d`;
}

export default function HistoryView({ history, onSelect, onBack }) {
  // Only show completed and processing jobs, not errors
  const visibleHistory = history.filter((h) => h.status === "done" || h.status === "processing");

  return (
    <div className="w-full max-w-4xl animate-fade-in">
      <div className="flex items-center justify-between mb-8">
        <div className="flex items-center gap-3">
          <button onClick={onBack}
            className="w-9 h-9 rounded-xl glass flex items-center justify-center text-gray-400 hover:text-white transition-colors">
            <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
              <path d="M19 12H5M12 19l-7-7 7-7" />
            </svg>
          </button>
          <div>
            <h1 className="text-2xl font-bold">Historial</h1>
            <p className="text-sm text-gray-500">{visibleHistory.length} videos</p>
          </div>
        </div>
      </div>

      {visibleHistory.length === 0 ? (
        <div className="text-center py-20">
          <p className="text-gray-500">No hay videos en el historial</p>
        </div>
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
          {visibleHistory.map((job) => {
            const name = (job.filename || "").replace(/\.mp3$/i, "");
            const songName = name.includes(" - ") ? name.split(" - ").slice(1).join(" - ") : name;
            const artistName = job.artist || (name.includes(" - ") ? name.split(" - ")[0] : "");

            return (
              <button
                key={job.job_id}
                onClick={() => onSelect(job.job_id)}
                className="glass rounded-2xl overflow-hidden text-left hover:border-white/[0.1] hover:shadow-glow transition-all duration-300 group"
              >
                {/* Thumbnail */}
                <div className="aspect-video bg-surface-3/30 relative overflow-hidden">
                  {job.status === "done" && (
                    <img
                      src={`${API}/preview/${job.job_id}/thumbnail`}
                      alt=""
                      className="w-full h-full object-cover group-hover:scale-105 transition-transform duration-500"
                      onError={(e) => { e.target.style.display = "none"; }}
                    />
                  )}
                  {job.status === "processing" && (
                    <div className="absolute inset-0 flex items-center justify-center">
                      <div className="w-8 h-8 border-2 border-brand border-t-transparent rounded-full animate-spin" />
                    </div>
                  )}
                  {job.status === "error" && (
                    <div className="absolute inset-0 flex items-center justify-center bg-red-500/5">
                      <svg className="w-8 h-8 text-red-400/50" fill="none" stroke="currentColor" strokeWidth="1.5" viewBox="0 0 24 24">
                        <circle cx="12" cy="12" r="10"/><path d="M15 9l-6 6M9 9l6 6"/>
                      </svg>
                    </div>
                  )}

                  {/* Status badge */}
                  <div className={`absolute top-2 right-2 px-2 py-0.5 rounded-full text-[10px] font-medium backdrop-blur-sm
                    ${job.status === "done" ? "bg-accent/20 text-accent" :
                      job.status === "error" ? "bg-red-500/20 text-red-400" :
                      "bg-brand/20 text-brand"}`}>
                    {job.status === "done" ? "Listo" : job.status === "error" ? "Error" : "Procesando"}
                  </div>

                  {/* Play overlay */}
                  {job.status === "done" && (
                    <div className="absolute inset-0 flex items-center justify-center opacity-0 group-hover:opacity-100 transition-opacity bg-black/30">
                      <div className="w-12 h-12 rounded-full bg-white/20 backdrop-blur-sm flex items-center justify-center">
                        <svg className="w-5 h-5 text-white ml-0.5" fill="currentColor" viewBox="0 0 24 24">
                          <path d="M8 5v14l11-7z"/>
                        </svg>
                      </div>
                    </div>
                  )}
                </div>

                {/* Info */}
                <div className="p-4">
                  <p className="text-sm font-medium text-white truncate">{songName || "Sin nombre"}</p>
                  <p className="text-xs text-gray-500 truncate mt-0.5">
                    {artistName}
                    {job.created_at && <span className="ml-2 text-gray-600">{timeAgo(job.created_at)}</span>}
                  </p>
                </div>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
