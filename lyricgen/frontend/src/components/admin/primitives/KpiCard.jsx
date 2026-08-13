// Card de métrica del admin (rediseño world-class 2026-06-12).
//
// Backward-compatible con el API original (value/label/hint/tone/loading)
// y suma la capa visual que faltaba:
//   - delta: número (proporción, ej. 0.17) → chip ↗ +17% / ↘ -17% con color
//   - spark: array de números → sparkline de tendencia al pie
//   - deltaLabel: texto del período del delta (default "vs período anterior")
//
//   <KpiCard value="91" label="Videos creados" delta={0.17} spark={[3,5,4,8]} />
import { Skeleton } from "../../Skeleton";
import Spark from "./Spark";

const TONES = {
  default: "text-white",
  accent: "text-accent",
  warn: "text-amber-400",
  danger: "text-red-400",
  brand: "text-brand-light",
};

const SPARK_COLORS = {
  default: "#8B5CF6",
  accent: "#14C8A8",
  warn: "#FBBF24",
  danger: "#F87171",
  brand: "#8B5CF6",
};

function DeltaChip({ delta, deltaLabel }) {
  if (delta === null || delta === undefined) return null;
  const pct = Math.round(delta * 100);
  const up = pct > 0;
  const flat = pct === 0;
  return (
    <span
      className={`inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded-full text-label font-semibold tabular-nums ${
        flat
          ? "bg-white/[0.06] text-gray-400"
          : up
            ? "bg-accent/15 text-accent"
            : "bg-red-500/15 text-red-300"
      }`}
      title={deltaLabel || "vs período anterior"}
    >
      {flat ? "→" : up ? "↗" : "↘"} {Math.abs(pct)}%
    </span>
  );
}

export default function KpiCard({
  value,
  label,
  hint,
  tone = "default",
  loading = false,
  delta,
  deltaLabel,
  spark,
}) {
  return (
    <div className="glass-elevated rounded-card px-5 pt-4 pb-3 flex flex-col min-h-[96px]">
      <p className="text-label uppercase tracking-[0.08em] text-gray-500">{label}</p>
      {loading ? (
        <Skeleton className="h-8 w-20 rounded-md mt-1.5" />
      ) : (
        <div className="flex items-baseline gap-2 mt-1">
          <p className={`text-[1.75rem] leading-8 font-bold tabular-nums tracking-tight ${TONES[tone] || TONES.default}`}>
            {value}
          </p>
          <DeltaChip delta={delta} deltaLabel={deltaLabel} />
        </div>
      )}
      {hint && <p className="text-label text-gray-500 mt-1">{hint}</p>}
      {!loading && <Spark data={spark} color={SPARK_COLORS[tone] || SPARK_COLORS.default} id={`kpi-${label}`} />}
    </div>
  );
}
