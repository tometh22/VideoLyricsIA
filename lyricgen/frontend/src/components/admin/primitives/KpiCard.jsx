// Card de métrica del admin. Reemplaza al StatCard ad-hoc y a las cards
// inline del tab de costos.
//
//   <KpiCard value="68" label="Videos creados" hint="últimos 30 días" />
//   <KpiCard value="3" label="Errores" tone="danger" />
import { Skeleton } from "../../Skeleton";

const TONES = {
  default: "text-white",
  accent: "text-accent",
  warn: "text-amber-400",
  danger: "text-red-400",
  brand: "text-brand-light",
};

export default function KpiCard({ value, label, hint, tone = "default", loading = false }) {
  return (
    <div className="glass-elevated rounded-card p-5">
      {loading ? (
        <Skeleton className="h-8 w-20 rounded-md mb-2" />
      ) : (
        <p className={`text-2xl font-bold tabular-nums ${TONES[tone] || TONES.default}`}>{value}</p>
      )}
      <p className="text-section uppercase text-gray-500 mt-1">{label}</p>
      {hint && <p className="text-label text-gray-500 mt-1">{hint}</p>}
    </div>
  );
}
