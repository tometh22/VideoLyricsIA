// Sección "Rendimiento" — ¿mejor o peor que antes? (rediseño world-class
// 2026-06-12: KPIs con delta chip + sparkline, área chart de tendencia
// real con tooltips, funnel visual con conversión y salud por cuenta con
// anillo de score — chau divs pintados a mano).
import KpiCard from "../../primitives/KpiCard";
import TrendChart from "../../primitives/TrendChart";
import SectionHeader from "../../layout/SectionHeader";
import { fmtMoney } from "../../adminApi";

import TenantHealthCards from "../operacion/TenantHealthCards";
import FunnelCard from "../operacion/FunnelCard";
import useRendimiento from "./useRendimiento";

function delta(current, prev) {
  if (!prev) return null;
  return (current - prev) / prev;
}

export default function RendimientoSection() {
  const { tenantHealth, funnel, kpis, daily, sparks, loading } = useRendimiento();

  return (
    <div className="space-y-6">
      {/* KPIs de la semana: valor + delta WoW + sparkline de 14 días */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <KpiCard
          value={kpis?.created ?? "—"}
          label="Videos creados"
          delta={kpis ? delta(kpis.created, kpis.createdPrev) : undefined}
          deltaLabel="vs semana anterior"
          spark={sparks?.created}
          loading={loading && !kpis}
        />
        <KpiCard
          value={kpis?.approved ?? "—"}
          label="Aprobados"
          tone="accent"
          delta={kpis ? delta(kpis.approved, kpis.approvedPrev) : undefined}
          deltaLabel="vs semana anterior"
          spark={sparks?.approved}
          loading={loading && !kpis}
        />
        <KpiCard
          value={kpis?.edits ?? "—"}
          label="Pedidos de edición"
          tone={kpis && kpis.edits > kpis.editsPrev ? "warn" : "default"}
          delta={kpis ? delta(kpis.edits, kpis.editsPrev) : undefined}
          deltaLabel="vs semana anterior"
          spark={sparks?.edits}
          loading={loading && !kpis}
        />
        <KpiCard
          value={kpis ? fmtMoney(kpis.aiCost) : "—"}
          label="Costo IA"
          delta={kpis ? delta(kpis.aiCost, kpis.aiCostPrev) : undefined}
          deltaLabel="vs semana anterior"
          spark={sparks?.cost}
          loading={loading && !kpis}
        />
      </div>

      {/* Tendencia 28 días — área chart real */}
      <div className="glass rounded-card p-5">
        <SectionHeader
          title="Tendencia · 28 días"
          subtitle="creados vs aprobados por día · pasá el mouse para el detalle"
        />
        <TrendChart
          data={daily || []}
          series={[
            { key: "created", label: "Creados" },
            { key: "approved", label: "Aprobados" },
          ]}
        />
      </div>

      {/* Funnel operativo (visual nuevo dentro de FunnelCard) */}
      <FunnelCard funnel={funnel} />

      {/* Salud por cuenta con anillo de score */}
      <div>
        <SectionHeader
          title="Salud por cuenta"
          subtitle="score 0-100 · un rojo acá es la business-alert de mañana"
        />
        <TenantHealthCards health={tenantHealth} />
      </div>
    </div>
  );
}
