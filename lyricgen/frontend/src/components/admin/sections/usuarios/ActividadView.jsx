// Vista "Actividad": una fila por usuario, sin scroll horizontal.
//
// Diseño (iteración por feedback de Tomás 2026-06-02):
//   - 5 columnas consolidadas (antes 11 → scroll horizontal feo): la info
//     secundaria vive como sub-línea dentro de cada celda o en el drill-down.
//   - Panel "¿Qué falló?" arriba de la tabla: cada error con su categoría,
//     usuario, canción, mensaje y cuándo — sin tener que expandir filas.
//   - Filtros como segmentos de un click (Con actividad / Con errores /
//     En línea / Todos) en vez de toggles + dropdowns.
import { useState } from "react";

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
import useActividad, { SEGMENTS } from "./useActividad";

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

function CategoryChip({ category }) {
  if (!category) return null;
  return (
    <span className="shrink-0 px-1.5 py-0.5 rounded bg-red-500/10 ring-1 ring-red-500/20 text-label text-red-300 whitespace-nowrap">
      {ERROR_CATEGORY_LABELS[category] || category}
    </span>
  );
}

// Panel "¿Qué falló?" — la respuesta directa a "algo falló, por qué y cuándo"
// sin tener que abrir el drill-down de cada usuario.
function ProblemsPanel({ recentErrors, errorsByCategory }) {
  const [expanded, setExpanded] = useState(true);
  if (recentErrors.length === 0) return null;

  return (
    <div className="glass-elevated rounded-card overflow-hidden ring-1 ring-red-500/20">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="w-full flex items-center gap-3 px-5 py-3.5 text-left hover:bg-white/[0.02] transition-colors duration-brand"
      >
        <span className="w-2 h-2 rounded-full bg-red-400 shrink-0" />
        <span className="text-ui font-semibold text-white flex-1">
          ¿Qué falló? <span className="text-red-300">({recentErrors.length} {recentErrors.length === 1 ? "error" : "errores"})</span>
        </span>
        {/* Resumen por categoría siempre visible en el header */}
        <span className="hidden sm:flex items-center gap-1.5 flex-wrap justify-end">
          {Object.entries(errorsByCategory || {})
            .sort((a, b) => b[1] - a[1])
            .slice(0, 4)
            .map(([cat, n]) => (
              <span key={cat} className="px-2 py-0.5 rounded-full bg-red-500/10 text-label text-red-300">
                {ERROR_CATEGORY_LABELS[cat] || cat}: {n}
              </span>
            ))}
        </span>
        <svg
          className={`w-4 h-4 text-gray-500 shrink-0 transition-transform duration-brand ${expanded ? "rotate-180" : ""}`}
          fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24"
        >
          <path d="M6 9l6 6 6-6" />
        </svg>
      </button>

      {expanded && (
        <div className="px-5 pb-4 divide-y divide-white/[0.04]">
          {recentErrors.slice(0, 10).map((e, i) => (
            <div key={`${e.job_id}-${i}`} className="py-2.5 flex items-start gap-3">
              <CategoryChip category={e.category} />
              <div className="flex-1 min-w-0">
                <p className="text-caption text-gray-300">
                  <span className="font-medium text-white">{e.artist} — {e.song_title || e.job_id}</span>
                  <span className="text-gray-500"> · {e.username}</span>
                </p>
                <p className="text-label font-mono text-red-300/90 break-words mt-0.5">
                  {e.error || "(sin mensaje de error)"}
                </p>
              </div>
              <span className="shrink-0 text-label text-gray-500 whitespace-nowrap">{fmtAgo(e.created_at)}</span>
            </div>
          ))}
          {recentErrors.length > 10 && (
            <p className="pt-2.5 text-label text-gray-500">
              … y {recentErrors.length - 10} más (expandí el usuario en la tabla para verlos)
            </p>
          )}
        </div>
      )}
    </div>
  );
}

// Detalle expandible de un usuario: desglose de retrabajos + fondos +
// (timeline de jobs | errores + descargas/eventos).
function ExpandedDetail({ user, detail }) {
  const rw = user.rework || {};
  const reworkChips = REWORK_FIELDS
    .map(([label, key]) => [label, rw[key] || 0])
    .filter(([, n]) => n > 0);

  return (
    <div>
      {/* Desglose de retrabajos + fondos */}
      <div className="flex flex-wrap gap-2 mb-3">
        {reworkChips.map(([label, n]) => (
          <span key={label} className="px-2 py-0.5 rounded-full bg-amber-500/10 ring-1 ring-amber-500/20 text-label text-amber-300">
            {label}: {n}
          </span>
        ))}
        {reworkTotal(rw) === 0 && (
          <span className="text-label text-gray-500">Sin retrabajos en la ventana 👌</span>
        )}
        <span className="px-2 py-0.5 rounded-full bg-surface-3/50 ring-1 ring-white/[0.06] text-label text-gray-400">
          Fondos: {user.backgrounds.library} librería · {user.backgrounds.ai_generated} IA
        </span>
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
            {/* Errores recientes de ESTE usuario */}
            {user.errors.recent.length > 0 && (
              <div>
                <p className="text-section uppercase tracking-wider text-gray-500 mb-2">Errores recientes</p>
                <div className="space-y-1.5">
                  {user.errors.recent.map((e) => (
                    <div key={e.job_id} className="text-caption">
                      <CategoryChip category={e.category} />
                      <span className="text-gray-400 ml-1.5">{e.artist} — {e.song_title || e.job_id}:</span>{" "}
                      <span className="text-red-300 font-mono break-all">{e.error || "(sin mensaje)"}</span>
                      <span className="text-gray-500"> · {fmtAgo(e.created_at)}</span>
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
    segment,
    setSegment,
    segmentCounts,
    activityTenantFilter,
    setActivityTenantFilter,
    visibleUsers,
    recentErrors,
    totalRework,
    loadActivity,
  } = useActividad();

  const telemetry = activity?.telemetry_enabled;

  // Chips de tenant (solo si hay más de uno — con un solo tenant no aportan).
  const tenants = activity && !activity.restricted
    ? [...new Set(activity.users.map((u) => u.tenant_id).filter(Boolean))].sort()
    : [];
  const tenantOptions = [
    { id: "", label: "Todos los tenants" },
    ...tenants.map((t) => ({ id: t, label: t })),
  ];

  // Segmentos con su conteo ("Con errores (2)"). "En línea" solo con telemetría.
  const segmentOptions = SEGMENTS
    .filter((s) => s.id !== "online" || telemetry)
    .map((s) => ({ id: s.id, label: `${s.label} (${segmentCounts[s.id] ?? 0})` }));

  // --- Columnas consolidadas (5, sin scroll horizontal) ---------------------
  const columns = [
    {
      key: "user",
      header: "Usuario",
      render: (u) => (
        <div className="min-w-0">
          <div className="flex items-center gap-1.5">
            <span className={`font-medium truncate ${u.is_active ? "text-white" : "text-gray-500 line-through"}`}>
              {u.username}
            </span>
            {u.sessions?.online && (
              <span className="shrink-0 inline-block w-2 h-2 rounded-full bg-emerald-400 animate-pulse" title="En línea ahora" />
            )}
            {u.role === "admin" && (
              <span className="shrink-0 text-section uppercase tracking-wide text-brand-light">admin</span>
            )}
          </div>
          <span className="block text-label text-gray-500 truncate">
            <span className="font-mono">{u.tenant_id}</span>
            <span> · {fmtAgo(u.last_activity)}</span>
          </span>
        </div>
      ),
    },
    ...(telemetry
      ? [{
          key: "time",
          header: "Tiempo en app",
          render: (u) => (
            <div className="whitespace-nowrap">
              <span className="tabular-nums text-white">{fmtDuration(u.sessions?.seconds_today)}</span>
              <span className="text-gray-500 text-label"> hoy</span>
              <span className="block text-label text-gray-500 tabular-nums">
                {fmtDuration(u.sessions?.seconds_week)} esta semana
              </span>
            </div>
          ),
        }]
      : []),
    {
      key: "videos",
      header: "Videos",
      render: (u) => (
        <div className="whitespace-nowrap">
          <span className="tabular-nums">
            <span className="text-accent font-medium">{u.videos.done}</span>
            <span className="text-gray-500"> / {u.videos.total} creados</span>
          </span>
          <span className="block text-label text-gray-500 tabular-nums">
            {u.videos.approved} aprobados
            {u.videos.in_progress > 0 && <span className="text-amber-400"> · {u.videos.in_progress} en curso</span>}
          </span>
        </div>
      ),
    },
    {
      key: "problems",
      header: "Problemas",
      render: (u) => {
        const errors = u.errors.count;
        const rework = reworkTotal(u.rework);
        if (errors === 0 && rework === 0) {
          return <span className="text-label text-gray-600">✓ sin problemas</span>;
        }
        return (
          <div className="flex items-center gap-1.5 flex-wrap">
            {errors > 0 && (
              <span className="px-2 py-0.5 rounded-full bg-red-500/10 ring-1 ring-red-500/20 text-label text-red-300 whitespace-nowrap">
                {errors} {errors === 1 ? "error" : "errores"}
              </span>
            )}
            {rework > 0 && (
              <span className="px-2 py-0.5 rounded-full bg-amber-500/10 ring-1 ring-amber-500/20 text-label text-amber-300 whitespace-nowrap">
                {rework} retrabajos
              </span>
            )}
          </div>
        );
      },
    },
    {
      key: "cost",
      header: "Costo IA",
      align: "right",
      render: (u) => <span className="tabular-nums font-mono text-white whitespace-nowrap">{fmtMoney(u.ai_cost_usd)}</span>,
    },
  ];

  return (
    <div className="space-y-5">
      {/* Filtros: período + tenant + refresh */}
      <FilterBar>
        <FilterBar.Chips
          value={activitySinceDays}
          onChange={setActivitySinceDays}
          options={PERIOD_OPTIONS}
          label="Período"
        />
        {tenants.length > 1 && (
          <FilterBar.Select
            value={activityTenantFilter}
            onChange={setActivityTenantFilter}
            options={tenantOptions}
            label="Tenant"
          />
        )}
        <button
          onClick={loadActivity}
          className="ml-auto px-3 py-1 rounded-md text-caption ring-1 ring-white/[0.06] text-gray-400 hover:text-white transition-colors duration-brand"
        >
          Refrescar
        </button>
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
          {/* KPIs de lo VISIBLE (respetan tenant + segmento) */}
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
              value={recentErrors.length}
              label="Errores"
              tone={recentErrors.length > 0 ? "danger" : "default"}
              hint={totalRework > 0 ? `+ ${totalRework} retrabajos` : undefined}
            />
            <KpiCard
              value={fmtMoney(visibleUsers.reduce((s, u) => s + u.ai_cost_usd, 0))}
              label="Costo IA"
            />
          </div>

          {/* ¿Qué falló? — errores con categoría, usuario, mensaje y cuándo */}
          <ProblemsPanel recentErrors={recentErrors} errorsByCategory={activity.errors_by_category} />

          {/* Aviso cuando el tracking de sesiones está apagado */}
          {!telemetry && (
            <div className="rounded-card bg-surface-3/30 ring-1 ring-white/[0.04] px-4 py-3">
              <p className="text-caption text-gray-500">
                El tracking de tiempo-en-app está deshabilitado
                (<span className="font-mono">TELEMETRY_ENABLED</span> apagada en el server).
                La columna "Tiempo en app" y el indicador de "en línea" aparecen al habilitarla.
              </p>
            </div>
          )}

          {/* Tabla por usuario: segmentos arriba, 5 columnas, drill-down */}
          <div className="glass-elevated rounded-card p-5">
            <div className="flex items-center justify-between gap-3 mb-4 flex-wrap">
              <FilterBar.Chips value={segment} onChange={setSegment} options={segmentOptions} />
              <span className="text-label text-gray-500">click en una fila para el detalle</span>
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
                  title="Nada por acá"
                  message={
                    segment === "errors"
                      ? "Ningún usuario tuvo errores en esta ventana 🎉"
                      : segment === "online"
                        ? "No hay nadie conectado ahora."
                        : "No hay usuarios con actividad en esta ventana o filtro."
                  }
                />
              }
            />
          </div>

          {/* Ayuda */}
          <details className="rounded-card bg-surface-3/30 ring-1 ring-white/[0.04] p-4">
            <summary className="text-caption text-gray-400 cursor-pointer select-none">
              Cómo leer estos números
            </summary>
            <ul className="text-label text-gray-500 leading-relaxed list-disc pl-4 space-y-1 mt-3">
              <li>
                <b>Videos</b>: terminados / totales creados en la ventana. <b>Aprobados</b> = pasaron revisión y siguen aprobados.
              </li>
              <li>
                <b>Problemas</b>: errores (el video falló — el detalle está en el panel "¿Qué falló?") y retrabajos
                (variantes, ediciones, reintentos, correcciones de letra, canciones recreadas — el desglose está en el drill-down).
              </li>
              <li>
                <b>Tiempo en app</b> (con telemetría): tiempo real con la app abierta y visible, medido por heartbeats
                del navegador cada 60 s. El punto verde = activo en los últimos 3 minutos.
              </li>
              <li>
                <b>Costo IA</b> estimado con las mismas tarifas de Negocio → Costos. Los fondos usados (librería vs IA)
                están en el drill-down de cada usuario.
              </li>
            </ul>
          </details>
        </>
      )}
    </div>
  );
}
