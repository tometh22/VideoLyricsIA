// Salud por cuenta (Fase 2 panel world-class) — la vista del guardián de
// negocio. Una card por tenant activo con score 0-100, semáforo y los 4
// componentes (uso WoW, first-pass, retrabajo, errores). El mismo dato que
// alimenta las alertas diarias [BUSINESS-ALERT] de Sentry: si acá hay un
// rojo, mañana hay un email — la idea es que el operador lo vea ANTES.
const STATUS_STYLES = {
  verde: { dot: "bg-accent", text: "text-accent" },
  amarillo: { dot: "bg-amber-400", text: "text-amber-400" },
  rojo: { dot: "bg-red-400", text: "text-red-400" },
};

function fmtPct(v) {
  if (v === null || v === undefined) return "—";
  return `${Math.round(v * 100)}%`;
}

function DeltaChip({ delta }) {
  if (delta === null || delta === undefined) {
    return <span className="text-gray-500">— WoW</span>;
  }
  const pct = Math.round(delta * 100);
  const tone = pct > 0 ? "text-accent" : pct < 0 ? "text-red-400" : "text-gray-400";
  const arrow = pct > 0 ? "↑" : pct < 0 ? "↓" : "=";
  return <span className={tone}>{arrow} {Math.abs(pct)}% WoW</span>;
}

export default function TenantHealthCards({ health }) {
  const tenants = health?.tenants || [];
  if (tenants.length === 0) return null;

  return (
    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
      {tenants.map((t) => {
        const s = STATUS_STYLES[t.status] || STATUS_STYLES.amarillo;
        return (
          <div key={t.tenant_id} className="glass-elevated rounded-card p-4">
            <div className="flex items-center justify-between mb-2">
              <div className="flex items-center gap-2 min-w-0">
                <span className={`w-2.5 h-2.5 rounded-full shrink-0 ${s.dot}`} />
                <span className="text-caption font-semibold text-white truncate">{t.tenant_id}</span>
              </div>
              <span className={`text-xl font-bold tabular-nums ${s.text}`}>{t.score}</span>
            </div>
            <div className="grid grid-cols-2 gap-x-3 gap-y-1 text-label text-gray-400">
              <span>Uso: {t.jobs_7d} jobs · <DeltaChip delta={t.usage_delta_wow} /></span>
              <span>First-pass: <span className="text-white">{fmtPct(t.first_pass_rate)}</span></span>
              <span>Retrabajo: <span className="text-white">{fmtPct(t.rework_rate)}</span></span>
              <span>Errores: <span className="text-white">{fmtPct(t.error_rate)}</span></span>
            </div>
          </div>
        );
      })}
    </div>
  );
}
