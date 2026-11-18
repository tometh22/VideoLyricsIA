// Sparkline minimalista para KpiCard — la tendencia de un vistazo, sin
// ejes ni ruido (rediseño world-class 2026-06-12).
import { Area, AreaChart, ResponsiveContainer } from "recharts";

export default function Spark({ data, color = "#8B5CF6", height = 36, id = "spark" }) {
  if (!data || data.length < 2) return null;
  const points = data.map((v, i) => ({ i, v }));
  // El id termina en un url(#…) del SVG: con espacios o acentos (sale del
  // label de la KPI) la referencia no resuelve y el área se pinta negra.
  const gradId = `grad-${String(id).normalize("NFD").replace(/[^A-Za-z0-9_-]/g, "-")}`;
  return (
    <div style={{ height }} className="w-full mt-2 -mb-1">
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={points} margin={{ top: 2, right: 0, bottom: 0, left: 0 }}>
          <defs>
            <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={color} stopOpacity={0.35} />
              <stop offset="100%" stopColor={color} stopOpacity={0} />
            </linearGradient>
          </defs>
          <Area
            type="monotone"
            dataKey="v"
            stroke={color}
            strokeWidth={1.8}
            fill={`url(#${gradId})`}
            isAnimationActive={false}
            dot={false}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}
