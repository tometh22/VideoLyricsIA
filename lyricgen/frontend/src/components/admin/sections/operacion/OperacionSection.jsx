// Sección "Ahora" — el centro de triaje del operador (¿está sano AHORA?).
//
// Consolidación 2026-06-11: la analítica (salud por cuenta, funnel) se
// mudó a la sección Rendimiento — acá queda solo lo accionable en vivo.
// Orden por prioridad: primero lo que rompe (zombies), después la salud del
// sistema, los KPIs del día, y abajo el trabajo en curso (cambios de UMG a
// la izquierda, pipeline en vivo a la derecha).
//
// El panel de "Cambios de UMG" (change requests del portal de deliveries) se
// había eliminado 2026-06-02 (UI-only; el backend nunca se tocó) creyendo
// que UMG ya no lo usaba. Re-agregado 2026-07-24: el botón "Solicitar
// cambios" sigue vivo en el portal — sin este panel los pedidos se
// guardaban en la DB pero nadie los veía.
import { useAdmin } from "../../AdminContext";
import KpiCard from "../../primitives/KpiCard";
import SectionHeader from "../../layout/SectionHeader";

import useOperacion from "./useOperacion";
import StuckJobsAlert from "./StuckJobsAlert";
import HealthStrip from "./HealthStrip";
import LivePipeline from "./LivePipeline";
import ChangeRequestsPanel from "./ChangeRequestsPanel";

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
      <HealthStrip health={op.health} issueCount={errors} stuckCount={op.stuckJobs?.length || 0} />

      {/* 3 · KPIs del día */}
      <div className="grid grid-cols-2 lg:grid-cols-5 gap-4">
        <KpiCard
          value={jobsStats.pending_review ?? 0}
          label="Pendientes de revisión"
          tone="warn"
          loading={statsLoading}
          hint="Ahora · requiere acción"
        />
        <KpiCard value={jobsStats.processing ?? 0} label="En proceso" tone="brand" loading={statsLoading} hint="Cola activa ahora" />
        <KpiCard value={errors} label="Errores" tone={errors > 0 ? "danger" : "default"} loading={statsLoading} hint="Mes actual · resultado final" />
        <KpiCard value={jobsStats.this_month ?? 0} label="Videos" loading={statsLoading} hint="Completados este mes" />
        <KpiCard
          value={op.crPendingCount}
          label="Cambios pendientes"
          tone={op.crPendingCount > 0 ? "warn" : "default"}
          hint="Pedidos de UMG en el portal"
        />
      </div>

      {/* 4 · Trabajo en curso */}
      <div className="grid grid-cols-1 lg:grid-cols-5 gap-6">
        <div className="lg:col-span-2">
          <SectionHeader title="Cambios de UMG" subtitle="Pedidos de corrección del portal" />
          <ChangeRequestsPanel
            changeRequests={op.changeRequests}
            crStatusFilter={op.crStatusFilter}
            setCrStatusFilter={op.setCrStatusFilter}
            crPendingCount={op.crPendingCount}
            crResolvedCount={op.crResolvedCount}
            crLoading={op.crLoading}
            crResolvingId={op.crResolvingId}
            resolveChangeRequest={op.resolveChangeRequest}
            reopenChangeRequest={op.reopenChangeRequest}
          />
        </div>
        <div className="lg:col-span-3">
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
    </div>
  );
}
