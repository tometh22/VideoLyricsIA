// Drill de una barra de adopción: ¿QUIÉNES usan esta feature con este
// valor y en qué videos? (iteración "profundidad", 2026-06-11).
import { useEffect, useState } from "react";

import { API, fetchJson, fmtAgo } from "../../adminApi";
import DataTable from "../../primitives/DataTable";
import EmptyState from "../../primitives/EmptyState";
import StatusBadge from "../../primitives/StatusBadge";

export default function FeatureDetailPanel({ drill, days, tenantId, onClose, onUserClick, onJobClick }) {
  const [detail, setDetail] = useState(null);

  useEffect(() => {
    let alive = true;
    setDetail(null);
    const p = new URLSearchParams({ value: drill.value, days: String(days) });
    if (tenantId) p.set("tenant_id", tenantId);
    fetchJson(`${API}/admin/insights/feature/${drill.feature}?${p}`)
      .then((d) => { if (alive) setDetail(d); })
      .catch(() => { if (alive) setDetail({ users: [], jobs: [], total_jobs: 0 }); });
    return () => { alive = false; };
  }, [drill.feature, drill.value, days, tenantId]);

  return (
    <div className="glass-elevated rounded-card p-5 ring-1 ring-brand/25">
      <div className="flex items-center justify-between gap-3 mb-3 flex-wrap">
        <p className="text-section uppercase text-gray-500">
          Quién usa <span className="text-brand-light normal-case">{drill.label || drill.value}</span>
          {detail && <span className="text-gray-500"> · {detail.total_jobs} videos</span>}
        </p>
        <button
          type="button"
          onClick={onClose}
          className="text-caption text-gray-400 hover:text-white px-2 py-1"
        >
          ✕ cerrar drill
        </button>
      </div>

      {!detail ? (
        <p className="text-caption text-gray-500">Cargando…</p>
      ) : (
        <div className="grid lg:grid-cols-2 gap-5">
          <div>
            <p className="text-label uppercase tracking-wider text-gray-500 mb-2">Usuarios</p>
            <DataTable
              dense
              columns={[
                { key: "username", header: "Usuario", render: (u) => <span className="text-white">{u.username}</span> },
                { key: "count", header: "Videos", align: "right" },
                {
                  key: "last_used", header: "Último uso", align: "right",
                  render: (u) => <span className="text-label text-gray-500">{fmtAgo(u.last_used)}</span>,
                },
              ]}
              rows={detail.users}
              rowKey={(u) => u.user_id}
              onRowClick={onUserClick ? (u) => onUserClick(u) : undefined}
              empty={<EmptyState title="Nadie la usó en la ventana" />}
            />
          </div>
          <div>
            <p className="text-label uppercase tracking-wider text-gray-500 mb-2">
              Videos (últimos {detail.jobs.length})
            </p>
            <DataTable
              dense
              columns={[
                {
                  key: "song", header: "Video",
                  render: (j) => (
                    <div className="min-w-0">
                      <span className="text-white truncate block">{j.artist} — {j.song_title || "(sin título)"}</span>
                      <span className="text-label text-gray-500">{j.username}</span>
                    </div>
                  ),
                },
                { key: "status", header: "Estado", render: (j) => <StatusBadge status={j.status} /> },
                {
                  key: "created_at", header: "Creado", align: "right",
                  render: (j) => <span className="text-label text-gray-500">{fmtAgo(j.created_at)}</span>,
                },
              ]}
              rows={detail.jobs}
              rowKey={(j) => j.job_id}
              onRowClick={onJobClick ? (j) => onJobClick(j.job_id) : undefined}
              empty={<EmptyState title="Sin videos" />}
            />
          </div>
        </div>
      )}
    </div>
  );
}
