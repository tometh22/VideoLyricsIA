// Funnel operativo (rediseño world-class 2026-06-12): barras con gradiente
// de marca, % de conversión ENTRE etapas (donde se lee el drop-off) y
// p50/p95 alineados. Derivado 100% de timestamps ya registrados.
import { fmtDuration } from "../../adminApi";

const STAGE_LABELS = {
  hasta_review: "Subida → review",
  review: "Review de letra",
  render: "Render",
  aprobacion: "Aprobación",
  descarga: "Descarga",
};

export default function FunnelCard({ funnel }) {
  if (!funnel || !funnel.total_jobs) return null;
  const max = Math.max(funnel.total_jobs, 1);
  const stages = funnel.stages || [];

  return (
    <div className="glass rounded-card p-5">
      <div className="flex items-center justify-between mb-5">
        <p className="text-section uppercase text-gray-500">
          Funnel · últimos {funnel.days} días · {funnel.total_jobs} jobs
        </p>
        {funnel.failed > 0 && (
          <span className="px-2 py-0.5 rounded-full bg-red-500/10 ring-1 ring-red-500/25 text-label text-red-300">
            {funnel.failed} fallidos
          </span>
        )}
      </div>
      <div className="space-y-1">
        {stages.map((s, i) => {
          const width = Math.max(5, Math.round((s.reached / max) * 100));
          const prev = i > 0 ? stages[i - 1].reached : null;
          const conv = prev ? Math.round((s.reached / Math.max(prev, 1)) * 100) : null;
          return (
            <div key={s.stage}>
              {/* % de conversión entre etapas — donde vive el drop-off */}
              {conv !== null && (
                <div className="flex items-center gap-2 pl-44 py-0.5">
                  <span className={`text-label tabular-nums ${conv < 60 ? "text-amber-400" : "text-gray-600"}`}>
                    ↓ {conv}%
                  </span>
                </div>
              )}
              <div className="flex items-center gap-3">
                <span className="text-caption text-gray-400 w-40 shrink-0 text-right">
                  {STAGE_LABELS[s.stage] || s.stage}
                </span>
                <div className="flex-1 h-7 bg-white/[0.03] rounded-lg overflow-hidden ring-1 ring-white/[0.04]">
                  <div
                    className="h-full rounded-lg flex items-center px-2.5 transition-all"
                    style={{
                      width: `${width}%`,
                      background: "linear-gradient(90deg, rgba(139,92,246,0.85), rgba(139,92,246,0.45))",
                    }}
                  >
                    <span className="text-caption font-semibold text-white tabular-nums drop-shadow">
                      {s.reached}
                    </span>
                  </div>
                </div>
                <span className="text-label text-gray-500 w-40 shrink-0 text-right tabular-nums">
                  {s.p50_s != null ? `p50 ${fmtDuration(s.p50_s)}` : "—"}
                  {s.p95_s != null ? ` · p95 ${fmtDuration(s.p95_s)}` : ""}
                </span>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
