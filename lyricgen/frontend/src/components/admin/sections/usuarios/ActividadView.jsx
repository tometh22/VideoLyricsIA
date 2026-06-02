// Vista "Actividad": una fila por usuario con videos / errores / retrabajos /
// fondos / costo IA en la ventana elegida, y un drill-down on-demand al
// expandir. Datos de useActividad. Es la parte más nueva del admin viejo —
// portada con cuidado, sin perder ninguna columna ni el detalle.
import {
  fmtAgo,
  fmtDuration,
  fmtMoney,
  reworkTotal,
  ERROR_CATEGORY_LABELS,
} from "../../adminApi";
import DataTable from "../../primitives/DataTable";
import EmptyState from "../../primitives/EmptyState";
import FilterBar from "../../primitives/FilterBar";
import KpiCard from "../../primitives/KpiCard";
import StatusBadge from "../../primitives/StatusBadge";
import useActividad from "./useActividad";

const PERIOD_OPTIONS = [
  { id: 7, label: "7d" },
  { id: 30, label: "30d" },
  { id: 90, label: "90d" },
];

// Desglose de retrabajos del drill-down. Solo se muestran los no-cero.
const REWORK_FIELDS = [
  ["Variantes", "variants"],
  ["Ediciones de letra", "edits_lyrics"],
  ["Ediciones de tipografía", "edits_typography"],
  ["Fondos regenerados", "edits_background"],
  ["Metadata", "edits_metadata"],
  ["Reintentos", "retries"],
  ["Videos con corrección de letra", "corrected_jobs"],
  ["Abandonados y recreados", "abandoned_recreated"],
];

function Spinner({ className = "w-6 h-6" }) {
  return <div className={`${className} border-2 border-brand border-t-transparent rounded-full animate-spin`} />;
}

// Detalle expandible de un usuario: desglose de retrabajos + (timeline de jobs
// | errores recientes + descargas/eventos).
function ExpandedDetail({ user, detail }) {
  const rw = user.rework || {};
  const reworkChips = REWORK_FIELDS
    .map(([label, key]) => [label, rw[key] || 0])
    .filter(([, n]) => n > 0);

  return (
    <div>
      {/* Desglose de retrabajos */}
      <div className="flex flex-wrap gap-2 mb-3">
        {reworkChips.map(([label, n]) => (
          <span key={label} className="px-2 py-0.5 rounded-full bg-amber-500/10 ring-1 ring-amber-500/20 text-label text-amber-300">
            {label}: {n}
          </span>
        ))}
        {reworkTotal(rw) === 0 && (
          <span className="text-label text-gray-500">Sin retrabajos en la ventana 👌</span>
        )}
      </div>

      {!detail ? (
        <div className="flex items-center gap-2 text-caption text-gray-500 py-2">
          <Spinner className="w-3.5 h-3.5" />
          Cargando detalle…
        </div>
      ) : (
        <div className="grid lg:grid-cols-2 gap-4">
          {/* Timeline de jobs */}
          <div>
            <p className="text-section uppercase tracking-wider text-gray-500 mb-2">
              Videos ({detail.jobs.length})
            </p>
            <div className="space-y-1.5 max-h-64 overflow-y-auto pr-1">
              {detail.jobs.length === 0 && (
                <p className="text-caption text-gray-500">Sin videos en la ventana.</p>
              )}
              {detail.jobs.map((j) => (
                <div key={j.job_id} className="flex items-start gap-2 text-caption">
                  <StatusBadge status={j.status} className="shrink-0" />
                  <span className="flex-1 text-gray-300 truncate">
                    {j.artist} — {j.song_title || "(sin título)"}
                    {j.parent_job_id && <span className="text-gray-500"> · variante</span>}
                    {j.edit_count > 0 && <span className="text-amber-400"> · {j.edit_count} ediciones</span>}
                  </span>
                  <span className="shrink-0 text-gray-500">{fmtAgo(j.created_at)}</span>
                </div>
              ))}
            </div>
          </div>

          <div className="space-y-4">
            {/* Errores recientes */}
            {user.errors.recent.length > 0 && (
              <div>
                <p className="text-section uppercase tracking-wider text-gray-500 mb-2">Errores recientes</p>
                <div className="space-y-1.5">
                  {user.errors.recent.map((e) => (
                    <div key={e.job_id} className="text-caption">
                      {e.category && (
                        <span className="mr-1.5 px-1.5 py-0.5 rounded bg-red-500/10 ring-1 ring-red-500/20 text-label text-red-300">
                          {ERROR_CATEGORY_LABELS[e.category] || e.category}
                        </span>
                      )}
                      <span className="text-gray-400">{e.artist} — {e.song_title || e.job_id}:</span>{" "}
                      <span className="text-red-300 font-mono break-all">{e.error || "(sin mensaje)"}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* Descargas + eventos (lista combinada, más recientes primero) */}
            <div>
              <p className="text-section uppercase tracking-wider text-gray-500 mb-2">
                Descargas ({detail.downloads.length})
                {" · "}Eventos de edición ({detail.events.length})
              </p>
              <div className="space-y-1 max-h-40 overflow-y-auto pr-1">
                {[...detail.downloads, ...detail.events]
                  .sort((a, b) => (b.created_at || "").localeCompare(a.created_at || ""))
                  .slice(0, 30)
                  .map((ev, i) => (
                    <div key={i} className="flex items-center gap-2 text-caption">
                      <span className="shrink-0 font-mono text-gray-400">{ev.action}</span>
                      <span className="flex-1 text-gray-500 truncate">
                        {ev.detail?.job_id || ""}
                        {ev.detail?.edit_type ? ` · ${ev.detail.edit_type}` : ""}
                        {ev.detail?.file_type ? ` · ${ev.detail.file_type}` : ""}
                      </span>
                      <span className="shrink-0 text-gray-500">{fmtAgo(ev.created_at)}</span>
                    </div>
                  ))}
                {detail.downloads.length === 0 && detail.events.length === 0 && (
                  <p className="text-caption text-gray-500">Sin descargas ni eventos.</p>
                )}
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default function ActividadView() {
  const {
    activity,
    activityLoading,
    activitySinceDays,
    setActivitySinceDays,
    activityExpanded,
    activityDetail,
    toggleRow,
    activityHideInactive,
    setActivityHideInactive,
    activityTenantFilter,
    setActivityTenantFilter,
    visibleUsers,
    hiddenCount,
    loadActivity,
  } = useActividad();

  const telemetry = activity?.telemetry_enabled;

  const tenantOptions = [
    { id: "", label: "Todos" },
    ...(activity && !activity.restricted
      ? [...new Set(activity.users.map((u) => u.tenant_id).filter(Boolean))]
          .sort()
          .map((t) => ({ id: t, label: t }))
      : []),
  ];

  // Columnas de la tabla (las de telemetría se insertan solo si está activa).
  const columns = [
    {
      key: "user",
      header: "Usuario",
      render: (u) => (
        <div>
          <div className="flex items-center gap-1.5 flex-wrap">
            <span className={`font-medium ${u.is_active ? "text-white" : "text-gray-500 line-through"}`}>
              {u.username}
            </span>
            {u.sessions?.online && (
              <span className="inline-block w-2 h-2 rounded-full bg-emerald-400 animate-pulse" title="En línea ahora" />
            )}
            {u.role === "admin" && (
              <span className="text-section uppercase tracking-wide text-brand-light">admin</span>
            )}
          </div>
          <span className="block text-label text-gray-500 font-mono">{u.tenant_id}</span>
        </div>
      ),
    },
    {
      key: "last",
      header: "Última actividad",
      render: (u) => <span className="text-gray-300 whitespace-nowrap">{fmtAgo(u.last_activity)}</span>,
    },
    ...(telemetry
      ? [
          {
            key: "today",
            header: "Hoy",
            align: "right",
            render: (u) => <span className="tabular-nums text-gray-300">{fmtDuration(u.sessions?.seconds_today)}</span>,
          },
          {
            key: "week",
            header: "7 días",
            align: "right",
            render: (u) => <span className="tabular-nums text-gray-300">{fmtDuration(u.sessions?.seconds_week)}</span>,
          },
        ]
      : []),
    {
      key: "videos",
      header: "Videos",
      align: "right",
      render: (u) => (
        <span className="tabular-nums">
          <span className="text-accent font-medium">{u.videos.done}</span>
          <span className="text-gray-500"> / {u.videos.total}</span>
        </span>
      ),
    },
    {
      key: "approved",
      header: "Aprobados",
      align: "right",
      render: (u) => <span className="tabular-nums text-gray-300">{u.videos.approved}</span>,
    },
    {
      key: "in_progress",
      header: "En curso",
      align: "right",
      render: (u) => <span className="tabular-nums text-amber-400">{u.videos.in_progress}</span>,
    },
    {
      key: "errors",
      header: "Errores",
      align: "right",
      render: (u) => (
        <span className={`tabular-nums font-medium ${u.errors.count > 0 ? "text-red-400" : "text-gray-500"}`}>
          {u.errors.count}
        </span>
      ),
    },
    {
      key: "rework",
      header: "Retrabajos",
      align: "right",
      render: (u) => (
        <span className={`tabular-nums ${reworkTotal(u.rework) > 0 ? "text-amber-300" : "text-gray-500"}`}>
          {reworkTotal(u.rework)}
        </span>
      ),
    },
    {
      key: "backgrounds",
      header: "Fondos lib/IA",
      align: "right",
      render: (u) => (
        <span className="tabular-nums text-gray-300">{u.backgrounds.library} / {u.backgrounds.ai_generated}</span>
      ),
    },
    {
      key: "cost",
      header: "Costo IA",
      align: "right",
      render: (u) => <span className="tabular-nums font-mono text-white">{fmtMoney(u.ai_cost_usd)}</span>,
    },
  ];

  const refreshBtn = (
    <button
      onClick={loadActivity}
      className="ml-auto px-3 py-1 rounded-md text-caption ring-1 ring-white/[0.06] text-gray-400 hover:text-white transition-colors duration-brand"
    >
      Refrescar
    </button>
  );

  return (
    <div className="space-y-6">
      <FilterBar>
        <FilterBar.Chips
          value={activitySinceDays}
          onChange={setActivitySinceDays}
          options={PERIOD_OPTIONS}
          label="Período"
        />
        {activity && !activity.restricted && (
          <FilterBar.Select
            value={activityTenantFilter}
            onChange={setActivityTenantFilter}
            options={tenantOptions}
            label="Tenant"
          />
        )}
        <FilterBar.Toggle
          checked={activityHideInactive}
          onChange={setActivityHideInactive}
          label="Ocultar usuarios sin actividad"
        />
        {refreshBtn}
      </FilterBar>

      {activityLoading || !activity ? (
        <div className="flex items-center justify-center py-12">
          <Spinner />
        </div>
      ) : activity.restricted ? (
        <div className="glass rounded-card p-8 text-center">
          <p className="text-ui text-gray-300 font-medium mb-1">Acceso restringido</p>
          <p className="text-caption text-gray-500">
            Esta vista está limitada a los super admins de la plataforma
            (variable <span className="font-mono">SUPER_ADMIN_USERS</span>).
            Pedile acceso a Tomás si la necesitás.
          </p>
        </div>
      ) : (
        <>
          {/* KPIs agregados de lo VISIBLE (respeta filtros) */}
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
            <KpiCard
              value={visibleUsers.filter((u) => u.videos.total > 0).length}
              label={`Usuarios activos (${activity.since_days}d)`}
            />
            <KpiCard
              value={visibleUsers.reduce((s, u) => s + u.videos.total, 0)}
              label="Videos creados"
            />
            <KpiCard
              value={visibleUsers.reduce((s, u) => s + u.errors.count, 0)}
              label="Errores"
              tone="danger"
            />
            <KpiCard
              value={fmtMoney(visibleUsers.reduce((s, u) => s + u.ai_cost_usd, 0))}
              label="Costo IA"
            />
          </div>

          {/* Breakdown global de errores por categoría */}
          {Object.keys(activity.errors_by_category || {}).length > 0 && (
            <div className="glass rounded-card p-4">
              <p className="text-section uppercase tracking-wider text-gray-500 mb-2">
                Errores por categoría
              </p>
              <div className="flex flex-wrap gap-2">
                {Object.entries(activity.errors_by_category)
                  .sort((a, b) => b[1] - a[1])
                  .map(([cat, n]) => (
                    <span
                      key={cat}
                      className="px-2.5 py-1 rounded-full bg-red-500/10 ring-1 ring-red-500/20 text-caption text-red-300"
                    >
                      {ERROR_CATEGORY_LABELS[cat] || cat}: <b>{n}</b>
                    </span>
                  ))}
              </div>
            </div>
          )}

          {/* Aviso cuando el tracking de sesiones está apagado */}
          {!telemetry && (
            <div className="rounded-card bg-surface-3/30 ring-1 ring-white/[0.04] px-4 py-3">
              <p className="text-caption text-gray-500">
                El tracking de tiempo-en-app está deshabilitado
                (<span className="font-mono">TELEMETRY_ENABLED</span> apagada en el server).
                Las columnas "Hoy" / "7 días" y el indicador de "en línea" aparecen al habilitarla.
              </p>
            </div>
          )}

          {/* Tabla por usuario con drill-down expandible */}
          <div className="glass-elevated rounded-card p-5">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-section text-white font-semibold">Actividad por usuario</h3>
              <span className="text-caption text-gray-500">
                {visibleUsers.length} usuarios
                {hiddenCount > 0 && ` (${hiddenCount} ocultos sin actividad)`}
                {" · click en una fila para el detalle"}
              </span>
            </div>
            <DataTable
              dense
              columns={columns}
              rows={visibleUsers}
              rowKey={(u) => u.user_id}
              onRowClick={(u) => toggleRow(u.user_id)}
              expandedKey={activityExpanded}
              renderExpanded={(u) => (
                <ExpandedDetail user={u} detail={activityDetail[u.user_id]} />
              )}
              empty={
                <EmptyState
                  title="Sin actividad"
                  message="No hay usuarios con actividad en esta ventana o filtro."
                />
              }
            />
          </div>

          {/* Ayuda */}
          <div className="rounded-card bg-surface-3/30 ring-1 ring-white/[0.04] p-4 space-y-2">
            <p className="text-caption text-gray-300 font-medium uppercase tracking-wide">
              Cómo leer estos números
            </p>
            <ul className="text-label text-gray-500 leading-relaxed list-disc pl-4 space-y-1">
              <li>
                <b>Videos</b>: terminados / totales creados en la ventana. <b>Aprobados</b> = pasaron revisión.
              </li>
              <li>
                <b>Retrabajos</b> agrupa variantes, ediciones (letra / tipografía / fondo / metadata), reintentos,
                correcciones manuales de letra y canciones abandonadas-y-recreadas. Alto retrabajo = fricción:
                mirá el desglose en el drill-down para ver dónde.
              </li>
              <li>
                <b>Fondos lib/IA</b>: cuántos videos usaron un fondo de la librería pre-aprobada vs. cuántas
                generaciones de fondo con IA (Veo/Imagen) disparó el usuario.
              </li>
              <li>
                <b>Costo IA</b> estimado con las mismas tarifas del tab Costos.
              </li>
              <li>
                <b>Hoy / 7 días</b> (con telemetría habilitada): tiempo real con la app abierta y visible,
                medido por heartbeats del navegador cada 60 s. El punto verde = activo en los últimos 3 minutos.
              </li>
            </ul>
          </div>
        </>
      )}
    </div>
  );
}
