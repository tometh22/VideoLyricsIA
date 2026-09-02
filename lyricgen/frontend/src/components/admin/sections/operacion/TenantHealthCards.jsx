// Salud por cuenta (rediseño world-class 2026-06-12): anillo de score SVG
// con el semáforo integrado + métricas en grilla limpia. El mismo dato que
// alimenta las [BUSINESS-ALERT] diarias — un rojo acá es el email de mañana.
const STATUS_COLORS = {
  verde: "#14C8A8",
  amarillo: "#FBBF24",
  rojo: "#F87171",
};

function fmtPct(v) {
  if (v === null || v === undefined) return "—";
  return `${Math.round(v * 100)}%`;
}

function ScoreRing({ score, status }) {
  const color = STATUS_COLORS[status] || STATUS_COLORS.amarillo;
  const R = 24;
  const C = 2 * Math.PI * R;
  const filled = C * Math.min(Math.max(score, 0), 100) / 100;
  return (
    <svg width="62" height="62" viewBox="0 0 62 62" className="shrink-0 -rotate-90">
      <circle cx="31" cy="31" r={R} fill="none" stroke="rgba(255,255,255,0.07)" strokeWidth="5" />
      <circle
        cx="31" cy="31" r={R} fill="none"
        stroke={color} strokeWidth="5" strokeLinecap="round"
        strokeDasharray={`${filled} ${C - filled}`}
      />
      <text
        x="31" y="31" textAnchor="middle" dominantBaseline="central"
        className="rotate-90" style={{ transformOrigin: "31px 31px" }}
        fill={color} fontSize="16" fontWeight="700"
      >
        {score}
      </text>
    </svg>
  );
}

function DeltaInline({ delta }) {
  if (delta === null || delta === undefined) return <span className="text-gray-600">— WoW</span>;
  const pct = Math.round(delta * 100);
  const tone = pct > 0 ? "text-accent" : pct < 0 ? "text-red-300" : "text-gray-400";
  return <span className={`${tone} tabular-nums`}>{pct > 0 ? "↗" : pct < 0 ? "↘" : "→"} {Math.abs(pct)}% WoW</span>;
}

export default function TenantHealthCards({ health }) {
  const tenants = health?.tenants || [];
  if (tenants.length === 0) return null;

  return (
    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
      {tenants.map((t) => (
        <div key={t.tenant_id} className="glass-elevated rounded-card p-4 flex items-center gap-4">
          <ScoreRing score={t.score} status={t.status} />
          <div className="min-w-0 flex-1">
            <p className="text-caption font-semibold text-white truncate font-mono">{t.tenant_id}</p>
            <p className="text-label text-gray-400 mt-0.5">
              {t.jobs_7d} jobs · <DeltaInline delta={t.usage_delta_wow} />
            </p>
            <div className="flex items-center gap-3 mt-1.5 text-label text-gray-500">
              <span>1ª pasada <span className="text-gray-200 tabular-nums">{fmtPct(t.first_pass_rate)}</span></span>
              <span>retrabajo <span className="text-gray-200 tabular-nums">{fmtPct(t.rework_rate)}</span></span>
              <span>errores <span className={`tabular-nums ${t.error_rate > 0 ? "text-red-300" : "text-gray-200"}`}>{fmtPct(t.error_rate)}</span></span>
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}
