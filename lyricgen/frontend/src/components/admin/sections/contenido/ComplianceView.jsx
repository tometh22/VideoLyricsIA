// Vista "Compliance": estado de los checks de cumplimiento UMG (read-only) +
// enlaces a la política de datos y al JSON de estado.
//
// Los datos vienen de useContenido (vía props desde ContenidoSection).
import { API, COMPLIANCE_STATUS } from "../../adminApi";
import EmptyState from "../../primitives/EmptyState";
import StatusBadge from "../../primitives/StatusBadge";
import TableSkeleton from "../../primitives/TableSkeleton";
import SectionHeader from "../../layout/SectionHeader";

// "guideline_data_minimization" → "Guideline data minimization" (title-case
// del primer token, underscores → espacios), igual que el admin viejo.
const titleCase = (key) =>
  key.replace(/_/g, " ").replace("guideline ", "Guideline ");

export default function ComplianceView({ compliance }) {
  const checks = Object.entries(compliance?.checks || {});

  return (
    <div>
      <SectionHeader
        title="Compliance UMG"
        subtitle={compliance?.guidelines_version}
      />

      <div className="space-y-6">
        {compliance === null ? (
          <div className="glass-elevated rounded-card p-6">
            <TableSkeleton rows={4} cols={3} />
          </div>
        ) : checks.length === 0 ? (
          <div className="glass-elevated rounded-card p-6">
            <EmptyState
              title="Sin checks de compliance"
              message="El backend no devolvió ningún check de cumplimiento."
            />
          </div>
        ) : (
          <div className="glass-elevated rounded-card p-6">
            <h3 className="text-ui font-semibold text-white mb-1">Estado de compliance UMG</h3>
            <p className="text-caption text-gray-500 mb-5">{compliance.guidelines_version}</p>

            <div className="space-y-3">
              {checks.map(([key, check]) => (
                <div
                  key={key}
                  className="rounded-card px-4 py-3 bg-surface-3/30 ring-1 ring-white/[0.06]"
                >
                  <div className="flex items-start gap-3">
                    <div className="flex-1 min-w-0">
                      <p className="text-caption font-medium text-white">{titleCase(key)}</p>
                      {check.detail && (
                        <p className="text-caption text-gray-400 mt-0.5">{check.detail}</p>
                      )}
                    </div>
                    <StatusBadge status={check.status} map={COMPLIANCE_STATUS} />
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Enlaces a la política de datos y al JSON de estado */}
        <div className="glass-elevated rounded-card p-6">
          <h3 className="text-ui font-semibold text-white mb-4">Política de datos</h3>
          <p className="text-caption text-gray-400 mb-4">
            Mirá la política de datos completa en{" "}
            <button
              onClick={() => window.open(`${API}/compliance/data-policy`, "_blank")}
              className="text-brand hover:text-brand-light underline"
            >
              /compliance/data-policy
            </button>
          </p>
          <div className="flex gap-3">
            <a
              href={`${API}/compliance/data-policy`}
              target="_blank"
              rel="noopener noreferrer"
              className="btn-secondary text-caption py-2 px-4"
            >
              Ver JSON de política de datos
            </a>
            <a
              href={`${API}/compliance/status`}
              target="_blank"
              rel="noopener noreferrer"
              className="btn-secondary text-caption py-2 px-4"
            >
              Ver JSON de estado de compliance
            </a>
          </div>
        </div>
      </div>
    </div>
  );
}
