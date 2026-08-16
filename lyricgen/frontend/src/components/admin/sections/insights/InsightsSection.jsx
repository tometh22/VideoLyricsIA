// Sección "Insights" — el panel del CEO (solo super-admin; la entrada del
// sidebar ni aparece para el resto y el backend gatea cada endpoint).
//
// Una sola vista con drill-down jerárquico, sin sub-tabs:
//   App (toda la plataforma) → click en un tenant → Tenant → click en un
//   usuario → Usuario (perfil completo). Breadcrumb arriba para subir.
// Cada nivel repite la misma gramática: KPIs → ¿Qué falló? → adopción de
// features → funnel del wizard → tabla clickeable del nivel siguiente.
import { useState } from "react";

import { fmtAgo, fmtMoney } from "../../adminApi";
import DataTable from "../../primitives/DataTable";
import EmptyState from "../../primitives/EmptyState";
import FilterBar from "../../primitives/FilterBar";
import KpiCard from "../../primitives/KpiCard";
import MargenTenantsView from "../negocio/MargenTenantsView";
import AdoptionPanel from "./AdoptionPanel";
import FeatureDetailPanel from "./FeatureDetailPanel";
import JobDetailPanel from "./JobDetailPanel";
import ProblemsPanel from "./ProblemsPanel";
import QualityLearningPanel from "./QualityLearningPanel";
import UserProfileView from "./UserProfileView";
import WizardFunnelPanel from "./WizardFunnelPanel";
import useInsights from "./useInsights";

const PERIOD_OPTIONS = [
  { id: 7, label: "7d" },
  { id: 30, label: "30d" },
  { id: 90, label: "90d" },
];

function Spinner() {
  return <div className="w-6 h-6 border-2 border-brand border-t-transparent rounded-full animate-spin" />;
}

function Crumb({ label, onClick, current }) {
  if (current) {
    return <span className="text-white font-medium">{label}</span>;
  }
  return (
    <button
      type="button"
      onClick={onClick}
      className="text-gray-400 hover:text-white transition-colors duration-brand"
    >
      {label}
    </button>
  );
}

function DeltaHint({ delta }) {
  if (delta === null || delta === undefined) return undefined;
  const pct = Math.round(delta * 100);
  const arrow = pct > 0 ? "↑" : pct < 0 ? "↓" : "=";
  return `${arrow} ${Math.abs(pct)}% vs ventana anterior`;
}

export default function InsightsSection({ subTab = "resumen" }) {
  const ins = useInsights();
  const { nav, overview, adoption, wizard, detail, economics, loading } = ins;
  const [userSearch, setUserSearch] = useState("");
  // Fila del ranking del usuario clickeado — alimenta los KPIs del perfil
  // (retrabajo/costo ya computados por overview, sin re-fetch).
  const [selectedUserRow, setSelectedUserRow] = useState(null);
  // Profundidad (2026-06-11): drill de una barra de features + ficha de
  // un video (modal, se abre desde cualquier lista).
  const [featureDrill, setFeatureDrill] = useState(null);
  const [jobDetailId, setJobDetailId] = useState(null);

  const kpis = overview?.kpis;

  // Tabla de tenants (nivel app)
  const tenantColumns = [
    {
      key: "tenant_id",
      header: "Tenant",
      render: (t) => (
        <div className="min-w-0">
          <span className="text-white font-medium font-mono">{t.tenant_id}</span>
          <span className="block text-label text-gray-500">{fmtAgo(t.last_activity)}</span>
        </div>
      ),
    },
    { key: "active_users", header: "Usuarios", align: "right" },
    {
      key: "jobs",
      header: "Videos",
      align: "right",
      render: (t) => (
        <span className="tabular-nums whitespace-nowrap">
          <span className="text-accent">{t.done}</span>
          <span className="text-gray-500">/{t.jobs}</span>
          {t.failed > 0 && <span className="text-red-400"> · {t.failed}✗</span>}
        </span>
      ),
    },
    {
      key: "rework_events",
      header: "Retrabajo",
      align: "right",
      render: (t) =>
        t.rework_events > 0 ? (
          <span className="text-amber-300 tabular-nums">{t.rework_events}</span>
        ) : (
          <span className="text-gray-600">0</span>
        ),
    },
    {
      key: "ai_cost_usd",
      header: "Costo IA",
      align: "right",
      render: (t) => <span className="font-mono tabular-nums">{fmtMoney(t.ai_cost_usd)}</span>,
    },
  ];

  // Tabla de usuarios (niveles app y tenant)
  const userColumns = [
    {
      key: "username",
      header: "Usuario",
      render: (u) => (
        <div className="min-w-0">
          <div className="flex items-center gap-1.5">
            <span className="text-white font-medium truncate">{u.username}</span>
            {u.online && (
              <span className="shrink-0 inline-block w-2 h-2 rounded-full bg-emerald-400 animate-pulse" title="En línea ahora" />
            )}
          </div>
          <span className="block text-label text-gray-500 truncate">
            <span className="font-mono">{u.tenant_id}</span> · {fmtAgo(u.last_activity)}
          </span>
        </div>
      ),
    },
    {
      key: "jobs",
      header: "Videos",
      align: "right",
      render: (u) => (
        <span className="tabular-nums whitespace-nowrap">
          <span className="text-accent">{u.done}</span>
          <span className="text-gray-500">/{u.jobs}</span>
          {u.failed > 0 && <span className="text-red-400"> · {u.failed}✗</span>}
        </span>
      ),
    },
    {
      key: "rework_events",
      header: "Retrabajo",
      align: "right",
      render: (u) =>
        u.rework_events > 0 ? (
          <span className="text-amber-300 tabular-nums">{u.rework_events}</span>
        ) : (
          <span className="text-gray-600">0</span>
        ),
    },
    {
      key: "ai_cost_usd",
      header: "Costo IA",
      align: "right",
      render: (u) => <span className="font-mono tabular-nums">{fmtMoney(u.ai_cost_usd)}</span>,
    },
  ];

  const visibleUsers = (overview?.users || []).filter(
    (u) => !userSearch || u.username.toLowerCase().includes(userSearch.toLowerCase())
  );

  return (
    <div className="space-y-5">
      {/* Breadcrumb + período */}
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-2 text-ui">
          <Crumb label="Toda la app" onClick={ins.upToApp} current={nav.level === "app"} />
          {nav.tenantId && (
            <>
              <span className="text-gray-600">›</span>
              <Crumb
                label={nav.tenantId}
                onClick={ins.upToTenant}
                current={nav.level === "tenant"}
              />
            </>
          )}
          {nav.level === "user" && (
            <>
              <span className="text-gray-600">›</span>
              <Crumb label={detail?.user?.username || `usuario ${nav.userId}`} current />
            </>
          )}
        </div>
        <FilterBar>
          <FilterBar.Chips value={ins.days} onChange={ins.setDays} options={PERIOD_OPTIONS} label="Período" />
          <button
            onClick={ins.refresh}
            className="px-3 py-1 rounded-md text-caption ring-1 ring-white/[0.06] text-gray-400 hover:text-white transition-colors duration-brand"
          >
            Refrescar
          </button>
        </FilterBar>
      </div>

      {loading ? (
        <div className="flex items-center justify-center py-16">
          <Spinner />
        </div>
      ) : nav.level === "user" ? (
        // El perfil de usuario es una página completa — ignora las tabs
        // (es el nivel más profundo del drill, todo el detalle junto).
        <>
          <UserProfileView
            detail={detail}
            summaryRow={selectedUserRow}
            wizardSessions={ins.userEvents}
            onJobClick={(jobId) => setJobDetailId(jobId)}
          />
          <AdoptionPanel adoption={adoption} title="Qué features usa (y cuáles nunca tocó)" />
          <WizardFunnelPanel wizard={wizard} />
        </>
      ) : subTab === "features" ? (
        <>
          <AdoptionPanel
            adoption={adoption}
            title={nav.level === "tenant" ? `Features que usa ${nav.tenantId}` : "Qué features se usan"}
            onDrill={(feature, value, label) => setFeatureDrill({ feature, value, label })}
          />
          {featureDrill && (
            <FeatureDetailPanel
              drill={featureDrill}
              days={ins.days}
              tenantId={nav.tenantId}
              onClose={() => setFeatureDrill(null)}
              onUserClick={(u) => {
                setFeatureDrill(null);
                ins.drillUser(u.user_id);
              }}
              onJobClick={(jobId) => setJobDetailId(jobId)}
            />
          )}
        </>
      ) : subTab === "wizard" ? (
        <WizardFunnelPanel wizard={wizard} />
      ) : subTab === "aprendizaje" ? (
        <QualityLearningPanel />
      ) : subTab === "margen" ? (
        nav.level === "app" ? (
          <MargenTenantsView economics={economics} loading={!economics} forbidden={false} />
        ) : (
          <MargenTenantsView
            economics={economics && {
              ...economics,
              tenants: (economics.tenants || []).filter((t) => t.tenant_id === nav.tenantId),
            }}
            loading={!economics}
            forbidden={false}
          />
        )
      ) : (
        <>
          {/* Tab Resumen: KPIs + qué falló + drill-down de tenants/usuarios */}
          {kpis && (
            <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
              <KpiCard
                value={kpis.jobs_total}
                label={`Videos creados (${ins.days}d)`}
                hint={DeltaHint({ delta: kpis.wow_delta })}
              />
              <KpiCard
                value={kpis.active_users}
                label="Usuarios activos"
                hint={kpis.in_progress > 0 ? `${kpis.in_progress} videos en curso` : undefined}
              />
              <KpiCard
                value={kpis.rework_events}
                label="Retrabajos"
                tone={kpis.rework_events > 0 ? "warn" : "default"}
                hint={`${kpis.retries} reintentos · ${kpis.corrected_jobs} correcciones`}
              />
              <KpiCard
                value={fmtMoney(kpis.ai_cost_usd)}
                label="Costo IA"
                hint={kpis.jobs_failed > 0 ? `${kpis.jobs_failed} errores` : "sin errores"}
              />
            </div>
          )}

          <ProblemsPanel
            recentErrors={overview?.recent_errors || []}
            errorsByCategory={overview?.errors_by_category}
          />

          {/* Drill-down: tenants (solo nivel app) */}
          {nav.level === "app" && (overview?.tenants?.length ?? 0) > 1 && (
            <div className="glass-elevated rounded-card p-5">
              <div className="flex items-center justify-between mb-3">
                <p className="text-section uppercase text-gray-500">Por tenant</p>
                <span className="text-label text-gray-500">click para entrar</span>
              </div>
              <DataTable
                dense
                columns={tenantColumns}
                rows={overview.tenants}
                rowKey={(t) => t.tenant_id}
                onRowClick={(t) => ins.drillTenant(t.tenant_id)}
                empty={<EmptyState title="Sin tenants con actividad" />}
              />
            </div>
          )}

          {/* Drill-down: usuarios */}
          <div className="glass-elevated rounded-card p-5">
            <div className="flex items-center justify-between gap-3 mb-3 flex-wrap">
              <p className="text-section uppercase text-gray-500">
                {nav.level === "tenant" ? `Usuarios de ${nav.tenantId}` : "Usuarios más activos"}
              </p>
              <div className="flex items-center gap-3">
                <FilterBar.Search
                  value={userSearch}
                  onChange={setUserSearch}
                  placeholder="Buscar usuario…"
                />
                <span className="text-label text-gray-500 hidden sm:block">click para el perfil</span>
              </div>
            </div>
            <DataTable
              dense
              columns={userColumns}
              rows={visibleUsers}
              rowKey={(u) => u.user_id}
              onRowClick={(u) => { setSelectedUserRow(u); ins.drillUser(u.user_id, u.tenant_id); }}
              empty={<EmptyState title="Sin usuarios con actividad en la ventana" />}
            />
          </div>
        </>
      )}

      {jobDetailId && (
        <JobDetailPanel jobId={jobDetailId} onClose={() => setJobDetailId(null)} />
      )}
    </div>
  );
}
