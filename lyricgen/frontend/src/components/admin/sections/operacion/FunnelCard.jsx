// Funnel operativo (Fase 1 panel world-class): dónde se traba el flujo y
// cuánto tarda cada etapa. Derivado 100% de timestamps ya registrados
// (created_at, segments_diff, provenance, approved_at, download).
//
// Lectura: el ANCHO de cada barra = cuántos jobs llegaron a esa etapa
// (conversión); los tiempos p50/p95 son la latencia de la etapa. Una
// etapa que se angosta de golpe = drop-off; un p95 que explota = el
// próximo reporte de la operadora antes de que lo escriba.
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

  return (
    <div className="glass rounded-card p-5">
      <div className="flex items-center justify-between mb-4">
        <p className="text-section uppercase text-gray-500">
          Funnel · últimos {funnel.days} días · {funnel.total_jobs} jobs
        </p>
        {funnel.failed > 0 && (
          <span className="text-label text-red-400">{funnel.failed} fallidos</span>
        )}
      </div>
      <div className="space-y-2">
        {(funnel.stages || []).map((s) => {
          const width = Math.max(4, Math.round((s.reached / max) * 100));
          return (
            <div key={s.stage} className="flex items-center gap-3">
              <span className="text-label text-gray-400 w-32 shrink-0">
                {STAGE_LABELS[s.stage] || s.stage}
              </span>
              <div className="flex-1 h-5 bg-white/[0.04] rounded overflow-hidden">
                <div
                  className="h-full bg-brand/60 rounded flex items-center px-2"
                  style={{ width: `${width}%` }}
                >
                  <span className="text-label text-white tabular-nums">{s.reached}</span>
                </div>
              </div>
              <span className="text-label text-gray-500 w-40 shrink-0 text-right tabular-nums">
                {s.p50_s != null ? `p50 ${fmtDuration(s.p50_s)}` : "—"}
                {s.p95_s != null ? ` · p95 ${fmtDuration(s.p95_s)}` : ""}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
