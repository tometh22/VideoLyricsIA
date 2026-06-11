// Sección "Rendimiento" — responde ¿mejor o peor que antes? (consolidación
// 2026-06-11). Antes estas piezas vivían mezcladas dentro de Operación,
// compitiendo visualmente con el triaje en vivo; ahora el operador que
// entra a apagar incendios va a "Ahora" y el que entra a evaluar la
// semana viene acá.
import KpiCard from "../../primitives/KpiCard";
import SectionHeader from "../../layout/SectionHeader";
import { fmtMoney } from "../../adminApi";

import TenantHealthCards from "../operacion/TenantHealthCards";
import FunnelCard from "../operacion/FunnelCard";
import useRendimiento from "./useRendimiento";

function wowHint(current, prev) {
  if (!prev) return current > 0 ? "sin semana previa para comparar" : undefined;
  const pct = Math.round(((current - prev) / prev) * 100);
  const arrow = pct > 0 ? "↑" : pct < 0 ? "↓" : "=";
  return `${arrow} ${Math.abs(pct)}% vs semana anterior`;
}

export default function RendimientoSection() {
  const { tenantHealth, funnel, kpis, loading } = useRendimiento();

  return (
    <div className="space-y-6">
      {/* KPIs de la semana con tendencia — una sola fuente (timeseries) */}
      <div>
        <SectionHeader
          title="Última semana"
          subtitle="vs los 7 días anteriores · fuente única: /admin/metrics/timeseries"
        />
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
          <KpiCard
            value={kpis?.created ?? "—"}
            label="Videos creados"
            hint={kpis ? wowHint(kpis.created, kpis.createdPrev) : undefined}
            loading={loading && !kpis}
          />
          <KpiCard
            value={kpis?.approved ?? "—"}
            label="Aprobados"
            hint={kpis ? wowHint(kpis.approved, kpis.approvedPrev) : undefined}
            loading={loading && !kpis}
          />
          <KpiCard
            value={kpis?.edits ?? "—"}
            label="Pedidos de edición"
            tone={kpis && kpis.edits > kpis.editsPrev ? "warn" : "default"}
            hint={kpis ? wowHint(kpis.edits, kpis.editsPrev) : undefined}
            loading={loading && !kpis}
          />
          <KpiCard
            value={kpis ? fmtMoney(kpis.aiCost) : "—"}
            label="Costo IA"
            hint={kpis ? wowHint(kpis.aiCost, kpis.aiCostPrev) : undefined}
            loading={loading && !kpis}
          />
        </div>
      </div>

      {/* Salud por cuenta (score 0-100 + semáforo, ventana 7d server-side) */}
      <div>
        <SectionHeader
          title="Salud por cuenta"
          subtitle="score 0-100 · un rojo acá es la business-alert de mañana"
        />
        <TenantHealthCards health={tenantHealth} />
      </div>

      {/* Funnel operativo (respeta el período global, cap 28d del backend) */}
      <FunnelCard funnel={funnel} />
    </div>
  );
}
