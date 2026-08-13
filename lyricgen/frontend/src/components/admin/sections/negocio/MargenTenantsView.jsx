// Margen por tenant (Fase 1 panel world-class) — LA métrica del negocio
// B2B: revenue del plan vs costo IA REAL (incluye retrabajos, que es donde
// el margen teórico y el real divergen). Complementa el panel de costos
// global (/admin/margin) con el desglose por cuenta.
//
// Gate: /admin/metrics/economics exige super-admin (SUPER_ADMIN_USERS).
// Si el backend responde 403, el bloque entero se oculta en silencio —
// un admin de tenant no debe saber siquiera que existe.
import DataTable from "../../primitives/DataTable";
import EmptyState from "../../primitives/EmptyState";
import SectionHeader from "../../layout/SectionHeader";

function usd(v) {
  if (v === null || v === undefined) return "—";
  return `$${Number(v).toFixed(2)}`;
}

export default function MargenTenantsView({ economics, loading, forbidden }) {
  if (forbidden) return null;

  const rows = economics?.tenants || [];
  const columns = [
    { key: "tenant_id", header: "Tenant", render: (r) => <span className="text-white font-medium">{r.tenant_id}</span> },
    { key: "approved_videos", header: "Aprobados", align: "right" },
    { key: "revenue_usd", header: "Revenue", align: "right", render: (r) => usd(r.revenue_usd) },
    { key: "ai_cost_usd", header: "Costo IA", align: "right", render: (r) => usd(r.ai_cost_usd) },
    {
      key: "margin_usd", header: "Margen", align: "right",
      render: (r) => (
        <span className={r.margin_usd >= 0 ? "text-accent" : "text-red-400"}>
          {usd(r.margin_usd)}
        </span>
      ),
    },
    {
      key: "cost_per_approved_usd", header: "Costo / video", align: "right",
      render: (r) => usd(r.cost_per_approved_usd),
    },
  ];

  return (
    <div className="mt-8">
      <SectionHeader
        title="Margen por tenant"
        subtitle={`Últimos ${economics?.days ?? 28} días · revenue = videos aprobados × precio del plan · costo = TODAS las llamadas IA del tenant (retrabajos incluidos)`}
      />
      <DataTable
        columns={columns}
        rows={rows}
        rowKey={(r) => r.tenant_id}
        loading={loading}
        dense
        empty={<EmptyState title="Sin actividad en el período" />}
      />
    </div>
  );
}
