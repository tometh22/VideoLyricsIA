// Vista "Costos y márgenes": gasto IA, costo por deliverable y margen estimado
// contra el revenue por video. Datos de /admin/margin (hook useNegocio).
//
// Rediseño 2026-06: cards → KpiCard, tablas tenant/usuario → DataTable. Las
// vizualizaciones específicas (salud del pipeline, bar-chart por proveedor,
// detalle por modelo collapse, caja "cómo leer") se conservan tal cual — no
// se fuerzan en primitivas porque no son tablas tabulares.
import { fmtMoney } from "../../adminApi";
import DataTable from "../../primitives/DataTable";
import EmptyState from "../../primitives/EmptyState";
import FilterBar from "../../primitives/FilterBar";
import KpiCard from "../../primitives/KpiCard";
import SectionHeader from "../../layout/SectionHeader";

const PERIOD_OPTIONS = [
  { id: 7, label: "7d" },
  { id: 30, label: "30d" },
  { id: 90, label: "90d" },
];

const pct = (rate) => (rate !== null && rate !== undefined ? `${(rate * 100).toFixed(1)}%` : "—");
const money = (v) => (v !== null && v !== undefined ? fmtMoney(v) : "—");

export default function CostosView({
  costSinceDays,
  setCostSinceDays,
  costRevenuePerVideo,
  setCostRevenuePerVideo,
  costDashboard,
  costLoading,
}) {
  // Columnas de "Costo por tenant".
  const tenantColumns = [
    { key: "tenant", header: "Tenant", render: (t) => <span className="font-mono text-white">{t.tenant_id || "—"}</span> },
    { key: "calls", header: "Calls", align: "right", render: (t) => <span className="tabular-nums text-gray-300">{t.calls}</span> },
    { key: "cost", header: "Gasto", align: "right", render: (t) => <span className="tabular-nums font-mono text-white">{fmtMoney(t.cost)}</span> },
    { key: "done", header: "Done", align: "right", render: (t) => <span className="tabular-nums text-accent">{t.done}</span> },
    { key: "pending", header: "Pending", align: "right", render: (t) => <span className="tabular-nums text-amber-400">{t.pending_review}</span> },
    { key: "rejected", header: "Rejected", align: "right", render: (t) => <span className="tabular-nums text-red-400">{t.rejected}</span> },
    { key: "deliverable", header: "Deliverable", align: "right", render: (t) => <span className="tabular-nums text-gray-300">{t.deliverable}</span> },
    { key: "cpd", header: "$/deliver", align: "right", render: (t) => <span className="tabular-nums font-mono text-gray-300">{money(t.cost_per_deliverable)}</span> },
    { key: "rejrate", header: "% rejects", align: "right", render: (t) => <span className="tabular-nums text-gray-400">{pct(t.rejection_rate)}</span> },
  ];

  // Columnas de "Costo por usuario".
  const userColumns = [
    {
      key: "user",
      header: "Usuario",
      render: (u) => (
        <span className="text-white">
          {u.username || <span className="text-gray-500 italic">user #{u.user_id ?? "—"}</span>}
        </span>
      ),
    },
    { key: "tenant", header: "Tenant", render: (u) => <span className="font-mono text-gray-400">{u.tenant_id || "—"}</span> },
    { key: "calls", header: "Calls", align: "right", render: (u) => <span className="tabular-nums text-gray-300">{u.calls}</span> },
    { key: "cost", header: "Gasto", align: "right", render: (u) => <span className="tabular-nums font-mono text-white">{fmtMoney(u.cost)}</span> },
    { key: "done", header: "Done", align: "right", render: (u) => <span className="tabular-nums text-accent">{u.done}</span> },
    { key: "pending", header: "Pending", align: "right", render: (u) => <span className="tabular-nums text-amber-400">{u.pending_review}</span> },
    { key: "rejected", header: "Rejected", align: "right", render: (u) => <span className="tabular-nums text-red-400">{u.rejected}</span> },
    { key: "cpd", header: "$/deliver", align: "right", render: (u) => <span className="tabular-nums font-mono text-gray-300">{money(u.cost_per_deliverable)}</span> },
    { key: "rejrate", header: "% rejects", align: "right", render: (u) => <span className="tabular-nums text-gray-400">{pct(u.rejection_rate)}</span> },
  ];

  return (
    <div>
      <SectionHeader
        title="Costos y márgenes"
        subtitle="Gasto IA, costo por deliverable y margen estimado contra el revenue por video."
      />
      <div className="space-y-6">
        {/* Período + revenue/video */}
        <FilterBar>
          <FilterBar.Chips
            value={costSinceDays}
            onChange={setCostSinceDays}
            options={PERIOD_OPTIONS}
            label="Período"
          />
          <div className="flex items-center gap-2 ml-auto">
            <span className="text-section uppercase text-gray-500">Revenue / video</span>
            <span className="text-caption text-gray-400">USD</span>
            <input
              type="number"
              step="0.5"
              min="0"
              value={costRevenuePerVideo}
              onChange={(e) => setCostRevenuePerVideo(Math.max(0, Number(e.target.value) || 0))}
              className="w-20 bg-surface-3/40 ring-1 ring-white/[0.06] focus:ring-brand/40 focus:outline-none rounded-md px-2 py-1 text-caption text-white text-right"
            />
          </div>
        </FilterBar>

        {costLoading || !costDashboard ? (
          <div className="flex items-center justify-center py-12">
            <div className="w-6 h-6 border-2 border-brand border-t-transparent rounded-full animate-spin" />
          </div>
        ) : (
          <>
            {/* Headline KPIs */}
            <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
              <KpiCard
                value={fmtMoney(costDashboard.total_cost)}
                label="Gasto IA total"
                hint={`${costDashboard.total_calls} calls · últimos ${costDashboard.since_days}d`}
              />
              <KpiCard
                value={costDashboard.video_counts.deliverable}
                label="Videos deliverable"
                hint={`${costDashboard.video_counts.done} done · ${costDashboard.video_counts.pending_review} pending`}
              />
              <KpiCard
                value={costDashboard.cost_per_deliverable !== null ? fmtMoney(costDashboard.cost_per_deliverable) : "—"}
                label="Costo / deliverable"
                hint="incluye rejects + retries"
              />
              <KpiCard
                value={costDashboard.margin_per_video !== null ? fmtMoney(costDashboard.margin_per_video) : "—"}
                label="Margen estimado"
                tone={costDashboard.margin_per_video !== null && costDashboard.margin_per_video < 0 ? "danger" : "accent"}
                hint={`/video · total ${costDashboard.margin_total !== null ? fmtMoney(costDashboard.margin_total) : "—"}`}
              />
            </div>

            {/* Salud del pipeline — viz específica, se conserva */}
            <div className="glass-elevated rounded-card p-5">
              <div className="flex items-center justify-between mb-4">
                <h3 className="text-sm font-semibold">Salud del pipeline</h3>
                <span className="text-label text-gray-500">% rejects + status counts</span>
              </div>
              <div className="grid grid-cols-2 lg:grid-cols-5 gap-3">
                <div>
                  <p className="text-section uppercase tracking-wider text-gray-500 mb-0.5">Done</p>
                  <p className="text-base font-bold text-accent tabular-nums">{costDashboard.video_counts.done}</p>
                </div>
                <div>
                  <p className="text-section uppercase tracking-wider text-gray-500 mb-0.5">Pending</p>
                  <p className="text-base font-bold text-amber-400 tabular-nums">{costDashboard.video_counts.pending_review}</p>
                </div>
                <div>
                  <p className="text-section uppercase tracking-wider text-gray-500 mb-0.5">Rejected</p>
                  <p className="text-base font-bold text-red-400 tabular-nums">{costDashboard.video_counts.rejected}</p>
                </div>
                <div>
                  <p className="text-section uppercase tracking-wider text-gray-500 mb-0.5">Error</p>
                  <p className="text-base font-bold text-red-500 tabular-nums">{costDashboard.video_counts.error}</p>
                </div>
                <div>
                  <p className="text-section uppercase tracking-wider text-gray-500 mb-0.5">% rejects</p>
                  <p className="text-base font-bold tabular-nums">{pct(costDashboard.rejection_rate)}</p>
                </div>
              </div>
            </div>

            {/* Desglose por proveedor — bar-chart específico, se conserva */}
            <div className="glass-elevated rounded-card p-5">
              <div className="flex items-center justify-between mb-4">
                <h3 className="text-sm font-semibold">Desglose por proveedor</h3>
                <span className="text-label text-gray-500">{costDashboard.by_provider.length} buckets</span>
              </div>
              <div className="space-y-2">
                {costDashboard.by_provider.map((p) => {
                  const share = costDashboard.total_cost > 0
                    ? (p.cost / costDashboard.total_cost) * 100
                    : 0;
                  return (
                    <div key={p.provider} className="flex items-center gap-3">
                      <span className="w-20 text-caption font-medium capitalize">{p.provider}</span>
                      <div className="flex-1 h-2 rounded-full bg-surface-3/40 overflow-hidden">
                        <div className="h-full bg-brand/60" style={{ width: `${Math.min(100, share)}%` }} />
                      </div>
                      <span className="w-20 text-label text-gray-400 tabular-nums text-right">{p.calls} calls</span>
                      <span className="w-20 text-caption font-mono tabular-nums text-right">{fmtMoney(p.cost)}</span>
                      <span className="w-12 text-label text-gray-500 tabular-nums text-right">{share.toFixed(0)}%</span>
                    </div>
                  );
                })}
              </div>
            </div>

            {/* Costo por tenant — DataTable */}
            <div className="glass-elevated rounded-card p-5">
              <div className="flex items-center justify-between mb-4">
                <h3 className="text-sm font-semibold">Costo por tenant</h3>
                <span className="text-label text-gray-500">{costDashboard.by_tenant.length} tenants</span>
              </div>
              <DataTable
                dense
                columns={tenantColumns}
                rows={costDashboard.by_tenant}
                rowKey={(t) => t.tenant_id}
                empty={<EmptyState title="Sin datos por tenant" message="No hubo gasto registrado por tenant en esta ventana." />}
              />
            </div>

            {/* Costo por usuario — DataTable */}
            <div className="glass-elevated rounded-card p-5">
              <div className="flex items-center justify-between mb-4">
                <h3 className="text-sm font-semibold">Costo por usuario</h3>
                <span className="text-label text-gray-500">{costDashboard.by_user.length} usuarios</span>
              </div>
              <DataTable
                dense
                columns={userColumns}
                rows={costDashboard.by_user}
                rowKey={(u) => `${u.user_id}|${u.tenant_id}`}
                empty={<EmptyState title="Sin datos por usuario" message="No hubo gasto registrado por usuario en esta ventana." />}
              />
            </div>

            {/* Detalle por modelo — collapse específico, se conserva */}
            <details className="glass rounded-card p-5">
              <summary className="text-caption text-gray-400 cursor-pointer select-none">
                Detalle por modelo ({costDashboard.by_tool.length} tools)
              </summary>
              <div className="mt-4 space-y-1.5">
                {costDashboard.by_tool.map((t) => (
                  <div key={`${t.tool_name}|${t.tool_provider}`} className="flex items-center gap-3 text-label">
                    <span className="flex-1 font-mono text-gray-300 truncate">{t.tool_name}</span>
                    <span className="text-gray-500">{t.tool_provider}</span>
                    <span className="w-16 text-right tabular-nums">{t.calls}×</span>
                    <span className="w-16 text-right tabular-nums font-mono">${t.rate_per_call.toFixed(3)}</span>
                    <span className="w-20 text-right tabular-nums font-mono text-white">{fmtMoney(t.cost)}</span>
                  </div>
                ))}
              </div>
            </details>

            {/* Cómo leer estos números — verbatim */}
            <div className="rounded-card bg-surface-3/30 ring-1 ring-white/[0.04] p-4 space-y-2">
              <p className="text-label text-gray-300 font-medium uppercase tracking-wide">
                Cómo leer estos números
              </p>
              <ul className="text-section text-gray-500 leading-relaxed list-disc pl-4 space-y-1">
                <li>
                  <b>Veo Fast</b> a $0.80/call (palindrome loop 8s) · <b>Veo Standard</b> $3.20.
                </li>
                <li>
                  <b>Whisper</b> cobrado como API de OpenAI a ~$0.006/min de audio (estimado en $0.021/call · canción promedio ~3.5 min). Las canciones más largas pueden costar +50%.
                </li>
                <li>
                  <b>Margen</b> calculado contra revenue editable arriba (default $8/video = contrato Universal $2k / 250 videos). No incluye costos de infra (Railway + R2 ≈ $50/mes fijo) ni Stripe fees.
                </li>
                <li>
                  <b>Costo / deliverable</b> incluye rejects y retries — por eso es mayor que el marginal de un render limpio.
                </li>
                <li>
                  <b>Veo in-flight</b> (calls con duration NULL): se cuentan como costo aunque Google probablemente no las facture si el render no completó. Sobre-estimación máxima ~$1.60.
                </li>
                <li>
                  Provenance de Whisper antes del 2026-05-13 fue backfilled con rows sintéticas (un row por job que llegó a status done/pending/rejected/editing). Jobs nuevos quedan tracked automáticamente.
                </li>
              </ul>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
