// Sección Operación — el centro de triaje del operador.
//
// Orden por prioridad: primero lo que rompe (zombies), después la salud del
// sistema, los KPIs del día, y abajo el pipeline en vivo.
//
// Nota: el panel de "Cambios de UMG" (change requests del portal de
// deliveries) se eliminó 2026-06-02 — era una instancia pre-lanzamiento;
// hoy los operadores de UMG usan la app directamente.
import { useAdmin } from "../../AdminContext";
import KpiCard from "../../primitives/KpiCard";
import SectionHeader from "../../layout/SectionHeader";

import useOperacion from "./useOperacion";
import StuckJobsAlert from "./StuckJobsAlert";
import TenantHealthCards from "./TenantHealthCards";
import FunnelCard from "./FunnelCard";
import HealthStrip from "./HealthStrip";
import LivePipeline from "./LivePipeline";

export default function OperacionSection() {
  const { stats, statsLoading } = useAdmin();
  const op = useOperacion();

  const jobsStats = stats?.jobs || {};
  const errors = jobsStats.errors || 0;

  return (
    <div className="space-y-6">
      {/* 1 · Zombies (solo si hay) + toast del reaper */}
      <StuckJobsAlert
        stuckJobs={op.stuckJobs}
        reaperRunning={op.reaperRunning}
        runReaperNow={op.runReaperNow}
        reaperResult={op.reaperResult}
        onDismissResult={() => op.setReaperResult(null)}
      />

      {/* 2 · Salud del sistema */}
      <HealthStrip health={op.health} />

      {/* 2b · Salud por cuenta + funnel (métricas de decisión, Fases 1+2) */}
      <TenantHealthCards health={op.tenantHealth} />
      <FunnelCard funnel={op.funnel} />

      {/* 3 · KPIs del día */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <KpiCard
          value={jobsStats.pending_review ?? 0}
          label="Pendientes de revisión"
          tone="warn"
          loading={statsLoading}
        />
        <KpiCard value={jobsStats.processing ?? 0} label="En proceso" tone="brand" loading={statsLoading} />
        <KpiCard value={errors} label="Errores" tone={errors > 0 ? "danger" : "default"} loading={statsLoading} />
        <KpiCard value={jobsStats.this_month ?? 0} label="Videos este mes" loading={statsLoading} />
      </div>

      {/* 4 · Pipeline en vivo (ancho completo) */}
      <div>
        <SectionHeader title="Pipeline en vivo" subtitle="Jobs en curso · auto-refresh 5 s" />
        <LivePipeline
          jobs={op.jobs}
          jobsTotal={op.jobsTotal}
          jobsLoading={op.jobsLoading}
          jobsStatusFilter={op.jobsStatusFilter}
          setJobsStatusFilter={op.setJobsStatusFilter}
          jobsTenantFilter={op.jobsTenantFilter}
          setJobsTenantFilter={op.setJobsTenantFilter}
          jobsAutoRefresh={op.jobsAutoRefresh}
          setJobsAutoRefresh={op.setJobsAutoRefresh}
        />
      </div>
    </div>
  );
}
