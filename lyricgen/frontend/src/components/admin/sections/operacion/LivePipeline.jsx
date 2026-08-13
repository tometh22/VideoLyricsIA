// Pipeline en vivo — tabla de jobs con auto-refresh.
//
// 4 columnas consolidadas (sin scroll horizontal): el job_id y el paso/
// progreso viven como sub-línea de Video y Estado. Cuando un job falla,
// el error (categoría + mensaje + cuándo) se muestra inline — no hay que
// ir a buscarlo a otro lado.
import { fmtAgo, ERROR_CATEGORY_LABELS } from "../../adminApi";
import FilterBar from "../../primitives/FilterBar";
import DataTable from "../../primitives/DataTable";
import StatusBadge from "../../primitives/StatusBadge";
import EmptyState from "../../primitives/EmptyState";

const STATUS_OPTIONS = [
  { id: "", label: "Todos" },
  { id: "processing", label: "Procesando" },
  { id: "queued", label: "En cola" },
  { id: "pending_review", label: "En revisión" },
  { id: "done", label: "Listos" },
  { id: "error", label: "Con error" },
  { id: "validation_failed", label: "Validación falló" },
  { id: "rejected", label: "Rechazados" },
];

// created_at viene como epoch en segundos; fmtAgo espera ISO.
function createdToIso(createdAt) {
  if (typeof createdAt === "number") return new Date(createdAt * 1000).toISOString();
  return createdAt || null;
}

const FAILED = new Set(["error", "validation_failed", "transcription_failed"]);

// Estado del job: badge + (paso y barra de progreso mientras corre | error
// con categoría y mensaje si falló).
function JobStatus({ job }) {
  const running = typeof job.progress === "number" && !FAILED.has(job.status) && job.status !== "done";
  return (
    <div className="min-w-0">
      <div className="flex items-center gap-2">
        <StatusBadge status={job.status} />
        {running && job.current_step && (
          <span className="text-label text-gray-500 truncate">{job.current_step}</span>
        )}
      </div>
      {running && (
        <div className="flex items-center gap-2 mt-1.5 max-w-[200px]">
          <div className="flex-1 h-1.5 bg-surface-3/60 rounded-full overflow-hidden min-w-[60px]">
            <div
              className="h-full bg-gradient-to-r from-brand to-brand-light transition-all duration-brand"
              style={{ width: `${Math.max(2, Math.min(100, job.progress))}%` }}
            />
          </div>
          <span className="text-section text-gray-500 tabular-nums">{job.progress}%</span>
        </div>
      )}
      {FAILED.has(job.status) && job.error && (
        <div className="mt-1.5 flex items-start gap-1.5">
          {job.error_category && (
            <span className="shrink-0 px-1.5 py-0.5 rounded bg-red-500/10 ring-1 ring-red-500/20 text-label text-red-300">
              {ERROR_CATEGORY_LABELS[job.error_category] || job.error_category}
            </span>
          )}
          <span className="text-label font-mono text-red-300/90 break-words line-clamp-2">
            {job.error}
          </span>
        </div>
      )}
      {(job.status === "rejected" || job.status === "validation_failed") && typeof job.progress === "number" && (
        <p className="mt-1.5 text-label text-gray-500">Progreso técnico: {job.progress}% · Resultado: {job.status === "rejected" ? "rechazado" : "falló validación"}</p>
      )}
    </div>
  );
}

const columns = [
  {
    key: "video",
    header: "Video",
    render: (j) => (
      <div className="min-w-0">
        <span className="block text-white truncate">
          {j.artist || "—"}{j.song_title ? ` — ${j.song_title}` : ""}
        </span>
        {/* Quién (usuario) + dónde (tenant) + qué (job id para soporte) */}
        <span className="block text-label text-gray-500 truncate">
          <span className="text-gray-400">{j.username || "—"}</span>
          <span> · {j.tenant_id || "—"}</span>
          <span className="font-mono"> · {j.job_id || "—"}</span>
        </span>
      </div>
    ),
  },
  { key: "status", header: "Estado", render: (j) => <JobStatus job={j} /> },
  {
    key: "created",
    header: "Creado",
    align: "right",
    render: (j) => (
      <span className="text-gray-500 whitespace-nowrap">{fmtAgo(createdToIso(j.created_at))}</span>
    ),
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
  // Cuántos del listado actual fallaron — para que el operador no tenga que
  // scrollear buscando rojo.
  const failedCount = (jobs || []).filter((j) => FAILED.has(j.status)).length;

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
        <span className="text-section uppercase text-gray-500 ml-auto whitespace-nowrap">
          {jobsTotal} jobs
          {failedCount > 0 && <span className="text-red-400"> · {failedCount} con error</span>}
        </span>
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
