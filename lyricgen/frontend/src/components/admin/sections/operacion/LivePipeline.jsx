// Pipeline en vivo — tabla de jobs con auto-refresh.
//
// Columnas: Job (id mono), Artista, Tenant, Estado (StatusBadge), Paso
// (current_step), Progreso (barra solo mientras corre), Creado (fmtAgo).
// El filtro de status / tenant y el toggle de auto-refresh viven arriba.
import { fmtAgo } from "../../adminApi";
import FilterBar from "../../primitives/FilterBar";
import DataTable from "../../primitives/DataTable";
import StatusBadge from "../../primitives/StatusBadge";
import EmptyState from "../../primitives/EmptyState";

const STATUS_OPTIONS = [
  { id: "", label: "Todos" },
  { id: "done", label: "done" },
  { id: "pending_review", label: "pending_review" },
  { id: "processing", label: "processing" },
  { id: "queued", label: "queued" },
  { id: "error", label: "error" },
  { id: "rejected", label: "rejected" },
  { id: "validation_failed", label: "validation_failed" },
];

// created_at viene como epoch en segundos; fmtAgo espera ISO.
function createdToIso(createdAt) {
  if (typeof createdAt === "number") return new Date(createdAt * 1000).toISOString();
  return createdAt || null;
}

function ProgressBar({ job }) {
  const running = typeof job.progress === "number" && job.status !== "done" && job.status !== "error";
  if (!running) return <span className="text-caption text-gray-600">—</span>;
  const pct = Math.max(2, Math.min(100, job.progress));
  return (
    <div className="flex items-center gap-2">
      <div className="flex-1 h-1.5 bg-surface-3/60 rounded-full overflow-hidden min-w-[60px]">
        <div
          className="h-full bg-gradient-to-r from-brand to-brand-light transition-all duration-brand"
          style={{ width: `${pct}%` }}
        />
      </div>
      <span className="text-section text-gray-500 tabular-nums w-8 text-right">{job.progress}%</span>
    </div>
  );
}

const columns = [
  {
    key: "job",
    header: "Job",
    render: (j) => (
      <span className="font-mono text-label text-gray-400">{j.job_id ? `${j.job_id.slice(0, 8)}…` : "—"}</span>
    ),
  },
  { key: "artist", header: "Artista", render: (j) => <span className="text-white">{j.artist || "—"}</span> },
  { key: "tenant", header: "Tenant", render: (j) => <span className="text-gray-500">{j.tenant_id || "—"}</span> },
  { key: "status", header: "Estado", render: (j) => <StatusBadge status={j.status} /> },
  { key: "step", header: "Paso", render: (j) => <span className="text-gray-400">{j.current_step || "—"}</span> },
  { key: "progress", header: "Progreso", width: "140px", render: (j) => <ProgressBar job={j} /> },
  {
    key: "created",
    header: "Creado",
    render: (j) => <span className="text-gray-500 whitespace-nowrap">{fmtAgo(createdToIso(j.created_at))}</span>,
  },
];

export default function LivePipeline({
  jobs,
  jobsTotal,
  jobsLoading,
  jobsStatusFilter,
  setJobsStatusFilter,
  jobsTenantFilter,
  setJobsTenantFilter,
  jobsAutoRefresh,
  setJobsAutoRefresh,
}) {
  return (
    <div className="space-y-4">
      <FilterBar>
        <FilterBar.Select
          label="Estado"
          value={jobsStatusFilter}
          onChange={setJobsStatusFilter}
          options={STATUS_OPTIONS}
        />
        <FilterBar.Search
          value={jobsTenantFilter}
          onChange={(v) => setJobsTenantFilter(v.trim())}
          placeholder="Filtrar por tenant…"
        />
        <FilterBar.Toggle checked={jobsAutoRefresh} onChange={setJobsAutoRefresh} label="Auto-refresh 5 s" />
        <span className="text-section uppercase text-gray-500 ml-auto">{jobsTotal} jobs</span>
      </FilterBar>

      <div className="glass rounded-card p-2">
        <DataTable
          columns={columns}
          rows={jobs}
          rowKey={(j) => j.job_id}
          loading={jobsLoading}
          dense
          empty={<EmptyState title="Sin jobs" message="No hay jobs que coincidan con el filtro." />}
        />
      </div>
    </div>
  );
}
