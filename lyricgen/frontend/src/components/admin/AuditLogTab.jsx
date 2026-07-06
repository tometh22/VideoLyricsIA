import { useState, useEffect, useCallback } from "react";

const API = import.meta.env.VITE_API_URL || "";

function authHeaders() {
  const token = localStorage.getItem("genly_token");
  return token ? { Authorization: `Bearer ${token}` } : {};
}

const PAGE = 50;

const ACTION_FILTERS = [
  { code: "", label: "Todas las acciones" },
  { code: "job.youtube", label: "Publicaciones YouTube" },
  { code: "youtube.channel", label: "Canales YouTube" },
  { code: "publish.", label: "Aprobaciones" },
  { code: "job.approve", label: "Aprobación de videos" },
  { code: "job.reject", label: "Rechazos" },
  { code: "admin.", label: "Acciones de admin" },
  { code: "reaper.", label: "Reaper" },
];

function actionPillClass(action) {
  if (action.startsWith("publish.deny") || action.startsWith("job.reject") || action.startsWith("reaper")) {
    return "bg-red-500/10 text-red-400 ring-red-500/25";
  }
  if (action.startsWith("publish.approve") || action.startsWith("job.approve") || action.includes("publish")) {
    return "bg-accent/10 text-accent ring-accent/25";
  }
  if (action.startsWith("admin.")) return "bg-amber-500/10 text-amber-400 ring-amber-500/25";
  return "bg-brand/10 text-brand-light ring-brand/25";
}

export default function AuditLogTab() {
  const [entries, setEntries] = useState([]);
  const [total, setTotal] = useState(0);
  const [action, setAction] = useState("");
  const [userId, setUserId] = useState("");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [expanded, setExpanded] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  const buildQuery = useCallback((offset) => {
    const params = new URLSearchParams({ limit: String(PAGE), offset: String(offset) });
    if (action) params.set("action", action);
    if (userId.trim()) params.set("user_id", userId.trim());
    if (dateFrom) params.set("date_from", `${dateFrom}T00:00:00Z`);
    if (dateTo) params.set("date_to", `${dateTo}T23:59:59Z`);
    return params.toString();
  }, [action, userId, dateFrom, dateTo]);

  const load = useCallback(async (offset = 0, append = false) => {
    setLoading(true);
    setError(false);
    try {
      const res = await fetch(`${API}/admin/audit?${buildQuery(offset)}`, { headers: authHeaders() });
      if (!res.ok) throw new Error();
      const data = await res.json();
      setTotal(data.total);
      setEntries((prev) => (append ? [...prev, ...data.entries] : data.entries));
    } catch {
      setError(true);
    }
    setLoading(false);
  }, [buildQuery]);

  // Debounced reload on filter change.
  useEffect(() => {
    const timer = setTimeout(() => load(0, false), 300);
    return () => clearTimeout(timer);
  }, [load]);

  // fetch+blob instead of <a href>: the endpoint needs the Authorization
  // header, which plain links can't send.
  const exportCsv = async () => {
    try {
      const res = await fetch(`${API}/admin/audit/export.csv?${buildQuery(0)}`, { headers: authHeaders() });
      if (!res.ok) throw new Error();
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "audit_log.csv";
      a.click();
      URL.revokeObjectURL(url);
    } catch {
      setError(true);
    }
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-3">
        <div>
          <label className="block text-[10px] text-gray-600 uppercase tracking-wider mb-1">Acción</label>
          <select value={action} onChange={(e) => setAction(e.target.value)}
            className="input-field text-xs h-9 pr-8">
            {ACTION_FILTERS.map((f) => (
              <option key={f.code} value={f.code}>{f.label}</option>
            ))}
          </select>
        </div>
        <div>
          <label className="block text-[10px] text-gray-600 uppercase tracking-wider mb-1">User ID</label>
          <input value={userId} onChange={(e) => setUserId(e.target.value)}
            className="input-field text-xs h-9 w-24" placeholder="—" />
        </div>
        <div>
          <label className="block text-[10px] text-gray-600 uppercase tracking-wider mb-1">Desde</label>
          <input type="date" value={dateFrom} onChange={(e) => setDateFrom(e.target.value)}
            className="input-field text-xs h-9 [color-scheme:dark]" />
        </div>
        <div>
          <label className="block text-[10px] text-gray-600 uppercase tracking-wider mb-1">Hasta</label>
          <input type="date" value={dateTo} onChange={(e) => setDateTo(e.target.value)}
            className="input-field text-xs h-9 [color-scheme:dark]" />
        </div>
        <div className="ml-auto flex items-center gap-3">
          <span className="text-xs text-gray-600">{total} eventos</span>
          <button onClick={exportCsv} className="btn-secondary text-xs h-9 px-4 inline-flex items-center">
            Exportar CSV
          </button>
        </div>
      </div>

      {error && (
        <div className="rounded-xl bg-red-500/10 border border-red-500/20 px-4 py-3 text-center">
          <p className="text-sm text-red-400">No se pudo cargar el audit log.</p>
          <button onClick={() => load(0, false)}
            className="mt-1 text-xs text-gray-400 hover:text-white underline">Reintentar</button>
        </div>
      )}

      <div className="glass rounded-card overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead>
            <tr className="text-[10px] text-gray-600 uppercase tracking-wider border-b border-white/[0.04]">
              <th className="px-4 py-3">Fecha</th>
              <th className="px-4 py-3">Usuario</th>
              <th className="px-4 py-3">Acción</th>
              <th className="px-4 py-3">Detalle</th>
            </tr>
          </thead>
          <tbody>
            {entries.map((e) => (
              <>
                <tr key={e.id}
                  onClick={() => setExpanded(expanded === e.id ? null : e.id)}
                  className="border-b border-white/[0.03] hover:bg-white/[0.02] cursor-pointer">
                  <td className="px-4 py-2.5 text-xs text-gray-400 whitespace-nowrap">
                    {e.created_at ? new Date(e.created_at).toLocaleString() : "—"}
                  </td>
                  <td className="px-4 py-2.5 text-xs text-gray-400">{e.user_id ?? "—"}</td>
                  <td className="px-4 py-2.5">
                    <span className={`px-2 py-0.5 rounded-full text-[10px] font-medium ring-1 ${actionPillClass(e.action)}`}>
                      {e.action}
                    </span>
                  </td>
                  <td className="px-4 py-2.5 text-xs text-gray-500 max-w-[280px] truncate">
                    {e.detail ? JSON.stringify(e.detail) : "—"}
                  </td>
                </tr>
                {expanded === e.id && (
                  <tr key={`${e.id}-detail`} className="border-b border-white/[0.03] bg-surface-3/20">
                    <td colSpan={4} className="px-4 py-3">
                      <pre className="text-[11px] text-gray-400 whitespace-pre-wrap break-all">
                        {JSON.stringify(e.detail, null, 2)}
                      </pre>
                    </td>
                  </tr>
                )}
              </>
            ))}
            {!loading && entries.length === 0 && !error && (
              <tr><td colSpan={4} className="px-4 py-8 text-center text-sm text-gray-600">
                Sin eventos para estos filtros
              </td></tr>
            )}
          </tbody>
        </table>
      </div>

      {entries.length < total && (
        <div className="text-center">
          <button onClick={() => load(entries.length, true)} disabled={loading}
            className="btn-secondary text-xs py-2 px-5 disabled:opacity-50">
            {loading ? "Cargando..." : "Cargar más"}
          </button>
        </div>
      )}
    </div>
  );
}
