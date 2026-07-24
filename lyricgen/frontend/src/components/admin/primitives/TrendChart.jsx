// Gráfico de tendencia temporal (área) — el corazón visual de Rendimiento
// (rediseño world-class 2026-06-12). Recharts con el lenguaje del design
// system: gradientes de marca, grid sutil, tooltip glass.
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

const SERIES_COLORS = ["#8B5CF6", "#14C8A8", "#FBBF24", "#F87171"];

function GlassTooltip({ active, payload, label }) {
  if (!active || !payload?.length) return null;
  return (
    <div className="rounded-xl bg-[#15151c]/95 ring-1 ring-white/10 px-3 py-2 shadow-xl backdrop-blur">
      <p className="text-label text-gray-400 mb-1">{label}</p>
      {payload.map((p) => (
        <p key={p.dataKey} className="text-caption tabular-nums flex items-center gap-1.5">
          <span className="w-2 h-2 rounded-full inline-block" style={{ background: p.color }} />
          <span className="text-gray-300">{p.name}:</span>
          <span className="text-white font-semibold">{p.value}</span>
        </p>
      ))}
    </div>
  );
}

/**
 * data: [{ day: "06-01", creada: 4, aprobada: 2 }, ...]
 * series: [{ key: "creada", label: "Creados" }, ...]
 */
export default function TrendChart({ data, series, height = 220 }) {
  if (!data || data.length < 2) {
    return (
      <div className="h-[140px] grid place-items-center text-label text-gray-600">
        sin datos suficientes para la tendencia
      </div>
    );
  }
  return (
    <div style={{ height }} className="w-full">
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: -18 }}>
          <defs>
            {series.map((s, i) => (
              <linearGradient key={s.key} id={`trend-${s.key}`} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={SERIES_COLORS[i % SERIES_COLORS.length]} stopOpacity={0.28} />
                <stop offset="100%" stopColor={SERIES_COLORS[i % SERIES_COLORS.length]} stopOpacity={0} />
              </linearGradient>
            ))}
          </defs>
          <CartesianGrid stroke="rgba(255,255,255,0.05)" vertical={false} />
          <XAxis
            dataKey="day"
            tick={{ fill: "#6B7280", fontSize: 10 }}
            axisLine={false}
            tickLine={false}
            interval="preserveStartEnd"
            minTickGap={28}
          />
          <YAxis
            tick={{ fill: "#6B7280", fontSize: 10 }}
            axisLine={false}
            tickLine={false}
            allowDecimals={false}
            width={34}
          />
          <Tooltip content={<GlassTooltip />} cursor={{ stroke: "rgba(255,255,255,0.15)" }} />
          {series.map((s, i) => (
            <Area
              key={s.key}
              type="monotone"
              dataKey={s.key}
              name={s.label}
              stroke={SERIES_COLORS[i % SERIES_COLORS.length]}
              strokeWidth={2}
              fill={`url(#trend-${s.key})`}
              isAnimationActive={false}
              dot={false}
              activeDot={{ r: 3.5, strokeWidth: 0 }}
            />
          ))}
        </AreaChart>
      </ResponsiveContainer>
      {/* Leyenda compacta */}
      <div className="flex items-center gap-4 mt-1 px-1">
        {series.map((s, i) => (
          <span key={s.key} className="flex items-center gap-1.5 text-label text-gray-400">
            <span className="w-2 h-2 rounded-full" style={{ background: SERIES_COLORS[i % SERIES_COLORS.length] }} />
            {s.label}
          </span>
        ))}
      </div>
    </div>
  );
}
