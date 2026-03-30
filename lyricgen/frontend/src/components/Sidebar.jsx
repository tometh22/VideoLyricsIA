const API = "";

function timeAgo(ts) {
  if (!ts) return "";
  const diff = Date.now() / 1000 - ts;
  if (diff < 60) return "Ahora";
  if (diff < 3600) return `${Math.floor(diff / 60)}m`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h`;
  return `${Math.floor(diff / 86400)}d`;
}

export default function Sidebar({ history, selectedId, onSelect, onNew, open, onToggle }) {
  if (!open) return null;

  return (
    <aside className="fixed left-0 top-0 bottom-0 w-72 bg-surface-1 border-r border-white/[0.04] z-20 flex flex-col">
      {/* Header */}
      <div className="flex items-center justify-between px-5 py-5 border-b border-white/[0.04]">
        <span className="text-sm font-semibold text-gray-300">Historial</span>
        <div className="flex items-center gap-2">
          <button
            onClick={onNew}
            className="text-xs font-medium text-brand hover:text-brand-light transition-colors flex items-center gap-1"
          >
            <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
              <path d="M12 5v14M5 12h14" />
            </svg>
            Nuevo
          </button>
          <button onClick={onToggle} className="text-gray-500 hover:text-white transition-colors ml-1">
            <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
              <path d="M11 19l-7-7 7-7M18 19l-7-7 7-7" />
            </svg>
          </button>
        </div>
      </div>

      {/* Job list */}
      <div className="flex-1 overflow-y-auto py-2">
        {history.length === 0 && (
          <p className="text-xs text-gray-600 text-center py-8">No hay jobs todavia</p>
        )}
        {history.map((job) => {
          const name = (job.filename || "").replace(/\.mp3$/i, "");
          const isSelected = job.job_id === selectedId;

          return (
            <button
              key={job.job_id}
              onClick={() => onSelect(job.job_id)}
              className={`w-full text-left px-4 py-3 flex items-center gap-3 transition-all duration-200
                ${isSelected
                  ? "bg-brand/10 border-r-2 border-brand"
                  : "hover:bg-surface-2/60"
                }`}
            >
              {/* Status dot */}
              <div className={`w-2 h-2 rounded-full shrink-0 ${
                job.status === "done" ? "bg-accent" :
                job.status === "error" ? "bg-red-400" :
                job.status === "processing" ? "bg-brand animate-pulse" :
                "bg-gray-600"
              }`} />

              <div className="min-w-0 flex-1">
                <p className="text-sm text-white truncate">{name || "Sin nombre"}</p>
                <p className="text-[11px] text-gray-500 truncate">
                  {job.artist}
                  {job.created_at && (
                    <span className="ml-2 text-gray-600">{timeAgo(job.created_at)}</span>
                  )}
                </p>
              </div>
            </button>
          );
        })}
      </div>
    </aside>
  );
}
