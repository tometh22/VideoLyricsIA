const API = "";

function timeAgo(ts) {
  if (!ts) return "";
  const diff = Date.now() / 1000 - ts;
  if (diff < 60) return "Ahora";
  if (diff < 3600) return `Hace ${Math.floor(diff / 60)} min`;
  if (diff < 86400) return `Hace ${Math.floor(diff / 3600)}h`;
  return `Hace ${Math.floor(diff / 86400)}d`;
}

function StatCard({ value, label, accent = false }) {
  return (
    <div className="glass rounded-2xl p-5 flex-1 text-center">
      <p className={`text-3xl font-bold ${accent ? "text-brand" : "text-white"}`}>{value}</p>
      <p className="text-[11px] text-gray-500 mt-1 uppercase tracking-wider">{label}</p>
    </div>
  );
}

function ActivityItem({ job, onSelect }) {
  const name = (job.filename || "").replace(/\.mp3$/i, "");
  const artistAndSong = name.includes(" - ") ? name : `${job.artist} - ${name}`;

  return (
    <button
      onClick={() => onSelect(job.job_id)}
      className="w-full flex items-center gap-4 px-4 py-3 rounded-xl hover:bg-surface-2/60 transition-all text-left"
    >
      {/* Thumbnail preview */}
      <div className="w-16 h-10 rounded-lg overflow-hidden shrink-0 bg-surface-3/50">
        {job.status === "done" && (
          <img
            src={`${API}/preview/${job.job_id}/thumbnail`}
            alt=""
            className="w-full h-full object-cover"
            onError={(e) => { e.target.style.display = "none"; }}
          />
        )}
        {job.status === "processing" && (
          <div className="w-full h-full flex items-center justify-center">
            <div className="w-4 h-4 border-2 border-brand border-t-transparent rounded-full animate-spin" />
          </div>
        )}
        {job.status === "error" && (
          <div className="w-full h-full flex items-center justify-center">
            <svg className="w-4 h-4 text-red-400" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
              <circle cx="12" cy="12" r="10"/><path d="M15 9l-6 6M9 9l6 6"/>
            </svg>
          </div>
        )}
      </div>

      <div className="flex-1 min-w-0">
        <p className="text-sm text-white truncate">{artistAndSong}</p>
        <p className="text-[11px] text-gray-500">
          {job.status === "done" && "Completado"}
          {job.status === "processing" && "Procesando..."}
          {job.status === "error" && "Error"}
          {job.created_at && <span className="ml-2 text-gray-600">{timeAgo(job.created_at)}</span>}
        </p>
      </div>

      {/* Status */}
      <div className={`w-2.5 h-2.5 rounded-full shrink-0 ${
        job.status === "done" ? "bg-accent" :
        job.status === "error" ? "bg-red-400" :
        "bg-brand animate-pulse"
      }`} />
    </button>
  );
}

export default function Dashboard({ history, onSelectJob, onNewBatch, onViewHistory }) {
  const done = history.filter((h) => h.status === "done").length;
  const errors = history.filter((h) => h.status === "error").length;
  const processing = history.filter((h) => h.status === "processing").length;
  const recent = history.slice(0, 8);

  // Fake monthly usage (based on actual history)
  const monthlyLimit = 100;
  const monthlyUsed = done;
  const usagePercent = Math.min(100, (monthlyUsed / monthlyLimit) * 100);

  return (
    <div className="w-full max-w-4xl animate-fade-in">
      {/* Welcome + New batch */}
      <div className="flex items-center justify-between mb-8">
        <div>
          <h1 className="text-2xl font-bold">Dashboard</h1>
          <p className="text-sm text-gray-500 mt-1">Resumen de actividad</p>
        </div>
        <button onClick={onNewBatch} className="btn-primary py-3 px-6">
          <svg className="inline-block w-4 h-4 mr-2 -mt-0.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
            <path d="M12 5v14M5 12h14" strokeLinecap="round"/>
          </svg>
          Nuevo batch
        </button>
      </div>

      {/* Stats */}
      <div className="flex gap-4 mb-8">
        <StatCard value={done} label="Videos generados" accent />
        <StatCard value={processing} label="En proceso" />
        <StatCard value={errors} label="Errores" />
        <StatCard value={history.length} label="Total jobs" />
      </div>

      {/* Monthly usage */}
      <div className="glass rounded-2xl p-6 mb-8">
        <div className="flex items-center justify-between mb-3">
          <div>
            <h3 className="text-sm font-semibold">Uso mensual</h3>
            <p className="text-[11px] text-gray-500 mt-0.5">{monthlyUsed} de {monthlyLimit} videos del plan</p>
          </div>
          <span className="text-sm font-bold text-brand">{Math.round(usagePercent)}%</span>
        </div>
        <div className="w-full h-2.5 bg-surface-3/50 rounded-full overflow-hidden">
          <div
            className="h-full rounded-full bg-gradient-to-r from-brand to-brand-light transition-all duration-500"
            style={{ width: `${usagePercent}%` }}
          />
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Recent activity */}
        <div className="lg:col-span-2 glass rounded-2xl p-6">
          <div className="flex items-center justify-between mb-4">
            <h3 className="text-sm font-semibold">Actividad reciente</h3>
            {history.length > 8 && (
              <button onClick={onViewHistory} className="text-xs text-brand hover:text-brand-light transition-colors">
                Ver todo
              </button>
            )}
          </div>
          {recent.length === 0 ? (
            <div className="text-center py-12">
              <div className="w-12 h-12 mx-auto mb-4 rounded-2xl bg-surface-3/50 flex items-center justify-center">
                <svg className="w-6 h-6 text-gray-600" fill="none" stroke="currentColor" strokeWidth="1.5" viewBox="0 0 24 24">
                  <path d="M9 18V5l12-2v13" strokeLinecap="round" strokeLinejoin="round"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/>
                </svg>
              </div>
              <p className="text-sm text-gray-500 mb-4">No hay videos todavia</p>
              <button onClick={onNewBatch} className="text-sm text-brand hover:text-brand-light transition-colors">
                Crear tu primer lyric video
              </button>
            </div>
          ) : (
            <div className="space-y-1">
              {recent.map((job) => (
                <ActivityItem key={job.job_id} job={job} onSelect={onSelectJob} />
              ))}
            </div>
          )}
        </div>

        {/* Quick actions + System */}
        <div className="space-y-6">
          {/* Quick actions */}
          <div className="glass rounded-2xl p-6">
            <h3 className="text-sm font-semibold mb-4">Acciones rapidas</h3>
            <div className="space-y-2">
              <button onClick={onNewBatch}
                className="w-full flex items-center gap-3 px-4 py-3 rounded-xl bg-brand/5 hover:bg-brand/10 text-sm text-gray-300 hover:text-white transition-all">
                <svg className="w-4 h-4 text-brand" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                  <path d="M12 5v14M5 12h14" strokeLinecap="round"/>
                </svg>
                Nuevo batch
              </button>
              <button onClick={onViewHistory}
                className="w-full flex items-center gap-3 px-4 py-3 rounded-xl bg-surface-3/30 hover:bg-surface-3/50 text-sm text-gray-300 hover:text-white transition-all">
                <svg className="w-4 h-4 text-gray-500" fill="none" stroke="currentColor" strokeWidth="1.5" viewBox="0 0 24 24">
                  <circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>
                </svg>
                Ver historial completo
              </button>
            </div>
          </div>

          {/* System status */}
          <div className="glass rounded-2xl p-6">
            <h3 className="text-sm font-semibold mb-4">Estado del sistema</h3>
            <div className="space-y-3">
              {[
                { name: "Transcripcion (Whisper)", ok: true },
                { name: "Video IA (Veo 3)", ok: true },
                { name: "Analisis tematico (Gemini)", ok: true },
              ].map((s) => (
                <div key={s.name} className="flex items-center gap-2.5">
                  <div className={`w-2 h-2 rounded-full ${s.ok ? "bg-accent" : "bg-red-400"}`} />
                  <span className="text-xs text-gray-400">{s.name}</span>
                </div>
              ))}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
