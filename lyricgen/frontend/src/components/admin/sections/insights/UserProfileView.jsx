// Nivel Usuario del panel Insights: el perfil completo de comportamiento de
// UNA persona — timeline de videos CON sus elecciones de estilo por job,
// retrabajos, errores, descargas, sesiones, logins y fondos de biblioteca.
// Reemplaza el drill-down anidado de la vieja ActividadView con una página
// entera (el reclamo era "denso, no práctico de ver").
import { fmtAgo, fmtDuration, fmtMoney } from "../../adminApi";
import DataTable from "../../primitives/DataTable";
import EmptyState from "../../primitives/EmptyState";
import KpiCard from "../../primitives/KpiCard";
import StatusBadge from "../../primitives/StatusBadge";

// Resumen compacto de las elecciones de un job: "Karaoke · Anton · oscuro · Auto"
function choicesLine(c) {
  if (!c) return null;
  const parts = [
    c.lyrics_animation && c.lyrics_animation !== "none" ? `anim ${c.lyrics_animation}` : null,
    c.line_transition && c.line_transition !== "none" ? `trans ${c.line_transition}` : null,
    c.font || null,
    c.style || null,
    c.effect || null,
    c.title_template && c.title_template !== "auto" ? `título ${c.title_template}` : null,
    c.has_custom_colors ? "colores custom" : null,
  ].filter(Boolean);
  return parts.length ? parts.join(" · ") : "todo default";
}

const BG_BADGE = {
  library_as_is: "fondo: biblioteca",
  library_variation: "fondo: variación",
  ai_video: "fondo: Veo",
  ai_image: "fondo: IA imagen",
};

export default function UserProfileView({ user, detail, summaryRow }) {
  if (!detail) return null;
  const u = detail.user || user || {};
  const jobs = detail.jobs || [];
  const downloads = detail.downloads || [];
  const events = detail.events || [];
  const sessions = detail.sessions;
  const logins = detail.logins || [];
  const libraryUsage = detail.library_usage || [];
  const row = summaryRow || {};

  const jobColumns = [
    {
      key: "song",
      header: "Video",
      render: (j) => (
        <div className="min-w-0">
          <div className="flex items-center gap-1.5">
            <span className="text-white font-medium truncate">
              {j.artist} — {j.song_title || "(sin título)"}
            </span>
            {j.parent_job_id && <span className="shrink-0 text-label text-gray-500">variante</span>}
          </div>
          <span className="block text-label text-gray-500 truncate">
            {choicesLine(j.choices) || "sin parámetros registrados"}
            {j.choices?.background_source && BG_BADGE[j.choices.background_source]
              ? ` · ${BG_BADGE[j.choices.background_source]}`
              : ""}
          </span>
        </div>
      ),
    },
    {
      key: "status",
      header: "Estado",
      render: (j) => (
        <div className="whitespace-nowrap">
          <StatusBadge status={j.status} />
          {j.edit_count > 0 && (
            <span className="block text-label text-amber-400 mt-0.5">{j.edit_count} re-render{j.edit_count > 1 ? "s" : ""}</span>
          )}
        </div>
      ),
    },
    {
      key: "when",
      header: "Creado",
      align: "right",
      render: (j) => (
        <span className="text-label text-gray-500 whitespace-nowrap">{fmtAgo(j.created_at)}</span>
      ),
    },
  ];

  return (
    <div className="space-y-5">
      {/* Identidad */}
      <div className="glass-elevated rounded-card p-5 flex items-start justify-between gap-4 flex-wrap">
        <div>
          <div className="flex items-center gap-2">
            <h3 className="text-lg font-bold text-white">{u.username}</h3>
            {row.online && (
              <span className="inline-block w-2 h-2 rounded-full bg-emerald-400 animate-pulse" title="En línea ahora" />
            )}
            {u.role === "admin" && (
              <span className="text-section uppercase tracking-wide text-brand-light">admin</span>
            )}
          </div>
          <p className="text-caption text-gray-500">
            {u.email} · <span className="font-mono">{u.tenant_id}</span> · plan {u.plan_id || u.plan || "—"}
            {u.is_active === false && <span className="text-red-400"> · desactivado</span>}
          </p>
        </div>
        {logins.length > 0 && (
          <div className="text-right">
            <p className="text-label text-gray-500">Último login</p>
            <p className="text-caption text-gray-300">
              {fmtAgo(logins[0].created_at)}
              <span className="text-gray-500"> · {logins[0].ip_address || "ip ?"}</span>
            </p>
          </div>
        )}
      </div>

      {/* KPIs personales (del ranking del overview, si está) */}
      {row.user_id && (
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
          <KpiCard value={`${row.done ?? 0}/${row.jobs ?? 0}`} label="Videos terminados / creados" />
          <KpiCard
            value={row.rework_events ?? 0}
            label="Retrabajos"
            tone={row.rework_events > 0 ? "warn" : "default"}
            hint={`${row.edits_total ?? 0} re-renders · ${row.retries ?? 0} reintentos · ${row.corrected_jobs ?? 0} correcciones`}
          />
          <KpiCard
            value={row.failed ?? 0}
            label="Errores"
            tone={row.failed > 0 ? "danger" : "default"}
          />
          <KpiCard value={fmtMoney(row.ai_cost_usd ?? 0)} label="Costo IA" />
        </div>
      )}

      {/* Timeline de jobs con elecciones */}
      <div className="glass rounded-card p-5">
        <p className="text-section uppercase text-gray-500 mb-3">
          Videos ({jobs.length}) — con sus elecciones de estilo
        </p>
        <DataTable
          dense
          columns={jobColumns}
          rows={jobs}
          rowKey={(j) => j.job_id}
          empty={<EmptyState title="Sin videos en la ventana" />}
        />
      </div>

      <div className="grid lg:grid-cols-2 gap-5">
        {/* Descargas + eventos de edición */}
        <div className="glass rounded-card p-5">
          <p className="text-section uppercase text-gray-500 mb-3">
            Descargas ({downloads.length}) · Eventos de edición ({events.length})
          </p>
          <div className="space-y-1 max-h-56 overflow-y-auto pr-1">
            {[...downloads, ...events]
              .sort((a, b) => (b.created_at || "").localeCompare(a.created_at || ""))
              .slice(0, 40)
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
            {downloads.length === 0 && events.length === 0 && (
              <p className="text-caption text-gray-500">Sin descargas ni eventos.</p>
            )}
          </div>
        </div>

        {/* Sesiones + fondos de biblioteca */}
        <div className="space-y-5">
          <div className="glass rounded-card p-5">
            <p className="text-section uppercase text-gray-500 mb-3">Sesiones</p>
            {sessions === null || sessions === undefined ? (
              <p className="text-caption text-gray-500">
                Tracking de sesiones apagado (<span className="font-mono">TELEMETRY_ENABLED</span>).
              </p>
            ) : sessions.length === 0 ? (
              <p className="text-caption text-gray-500">Sin sesiones en la ventana.</p>
            ) : (
              <div className="space-y-1 max-h-40 overflow-y-auto pr-1">
                {sessions.slice(0, 15).map((s) => (
                  <div key={s.id} className="flex items-center gap-2 text-caption">
                    <span className="text-gray-300">{fmtAgo(s.started_at)}</span>
                    <span className="flex-1 text-gray-500">
                      ~{fmtDuration((s.heartbeats || 1) * 60)} en la app
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>

          <div className="glass rounded-card p-5">
            <p className="text-section uppercase text-gray-500 mb-3">
              Fondos de biblioteca usados ({libraryUsage.length})
            </p>
            {libraryUsage.length === 0 ? (
              <p className="text-caption text-gray-500">No usó la biblioteca en la ventana.</p>
            ) : (
              <div className="space-y-1 max-h-40 overflow-y-auto pr-1">
                {libraryUsage.slice(0, 20).map((bu, i) => (
                  <div key={i} className="flex items-center gap-2 text-caption">
                    <span className="text-gray-300 truncate">{bu.name || `asset ${bu.asset_id}`}</span>
                    <span className="shrink-0 text-label text-gray-500">
                      {bu.mode === "variation" ? "variación" : "tal cual"}
                    </span>
                    <span className="flex-1 text-right text-gray-500">{fmtAgo(bu.used_at)}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
