import { useState, useEffect } from "react";
import { useI18n } from "../i18n";

const API = import.meta.env.VITE_API_URL || "";

function authHeaders() {
  const token = localStorage.getItem("genly_token");
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function fmtDate(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleDateString("es-AR", { day: "2-digit", month: "short", year: "numeric" });
}

function StatCard({ value, label, color = "text-white" }) {
  return (
    <div className="glass rounded-card p-5 text-center">
      <p className={`text-3xl font-bold ${color}`}>{value}</p>
      <p className="text-[11px] text-gray-500 mt-1 uppercase tracking-wider">{label}</p>
    </div>
  );
}

export default function AdminPanel({ onBack }) {
  const { t } = useI18n();
  const [tab, setTab] = useState("overview");
  const [stats, setStats] = useState(null);
  const [users, setUsers] = useState([]);
  const [usersTotal, setUsersTotal] = useState(0);
  const [jobs, setJobs] = useState([]);
  const [jobsTotal, setJobsTotal] = useState(0);
  const [jobsTenantFilter, setJobsTenantFilter] = useState("");
  const [jobsStatusFilter, setJobsStatusFilter] = useState("");
  const [jobsAutoRefresh, setJobsAutoRefresh] = useState(true);
  const [invoices, setInvoices] = useState([]);
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const [compliance, setCompliance] = useState(null);
  const [backgrounds, setBackgrounds] = useState([]);
  const [bgUploading, setBgUploading] = useState(false);
  const [bgName, setBgName] = useState("");
  const [bgTags, setBgTags] = useState("");
  // Empty string here means "global / visible to all". Anything else is
  // a tenant_id the asset gets locked to. UMG exclusivity hangs off this.
  const [bgOwnerTenant, setBgOwnerTenant] = useState("");
  // Tenants that have at least one user; populated from
  // /admin/background-tenants so we don't hardcode the UMG name.
  const [bgTenants, setBgTenants] = useState([]);
  // Library list filter: "" = all, "__global__" = unowned, anything else =
  // exact tenant match. Server-side via the same endpoint.
  const [bgListFilter, setBgListFilter] = useState("");

  // Cost panel — populated by GET /admin/margin. Period selector lets the
  // operator switch between fresh (7d) and stable-average (90d) views;
  // revenue per video defaults to $8 (Universal contract: $2k / 250
  // videos) and is editable so we can model other deals.
  const [costSinceDays, setCostSinceDays] = useState(30);
  const [costRevenuePerVideo, setCostRevenuePerVideo] = useState(8);
  const [costDashboard, setCostDashboard] = useState(null);
  const [costLoading, setCostLoading] = useState(false);
  const loadCostDashboard = async () => {
    setCostLoading(true);
    try {
      const u = `${API}/admin/margin?since_days=${costSinceDays}` +
        `&revenue_per_video_usd=${costRevenuePerVideo}`;
      const res = await fetch(u, { headers: authHeaders() });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setCostDashboard(await res.json());
    } catch (err) {
      flashError(`No pude cargar el panel de costos: ${err.message || err}`);
    } finally {
      setCostLoading(false);
    }
  };

  // Inline error banner — usado por handlers que hacen mutaciones
  // (delete bg, toggle user, change plan, etc.) y necesitan informar
  // al operador cuando el backend rechaza. Auto-clear en 8 s.
  // Antes los handlers ignoraban res.ok → operador creía que su
  // acción se cumplió cuando el server había rechazado. Audit lo
  // detectó. Ver CONTRIBUTING.md §4.
  const [adminError, setAdminError] = useState(null);
  const flashError = (msg) => {
    setAdminError(msg);
    setTimeout(() => setAdminError((cur) => (cur === msg ? null : cur)), 8000);
  };

  // Create user modal
  const [showCreate, setShowCreate] = useState(false);
  const [newUser, setNewUser] = useState({ username: "", password: "", email: "", plan_id: "100", role: "user", tenant_id: "" });
  const [createError, setCreateError] = useState("");

  // Change requests panel — UMG (and any portal user) leaves comments
  // on a delivery version via "Solicitar cambios". They land in
  // delivery_change_requests pending. The operator reviews them here.
  const [crStatusFilter, setCrStatusFilter] = useState("pending");
  const [crItems, setCrItems] = useState([]);
  const [crPending, setCrPending] = useState(0);
  const [crResolved, setCrResolved] = useState(0);
  const [crLoading, setCrLoading] = useState(false);
  // Per-row state for the inline "marcar resuelto" note input. Key is
  // the change-request id, value is the typed note.
  const [crResolveDraft, setCrResolveDraft] = useState({});
  const [crResolvingId, setCrResolvingId] = useState(null);
  const loadChangeRequests = async () => {
    setCrLoading(true);
    try {
      const res = await fetch(
        `${API}/admin/change-requests?status=${crStatusFilter}&limit=200`,
        { headers: authHeaders() },
      );
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setCrItems(data.items || []);
      setCrPending(data.pending_count || 0);
      setCrResolved(data.resolved_count || 0);
    } catch (err) {
      flashError(`No pude cargar los cambios: ${err.message || err}`);
    } finally {
      setCrLoading(false);
    }
  };
  const resolveChangeRequest = async (id) => {
    setCrResolvingId(id);
    try {
      const note = (crResolveDraft[id] || "").trim();
      const res = await fetch(`${API}/admin/change-requests/${id}/resolve`, {
        method: "POST",
        headers: { ...authHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ resolution_note: note }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || `Error ${res.status}`);
      }
      setCrResolveDraft((d) => { const n = { ...d }; delete n[id]; return n; });
      await loadChangeRequests();
    } catch (err) {
      flashError(`No pude marcar como resuelto: ${err.message || err}`);
    } finally {
      setCrResolvingId(null);
    }
  };
  const reopenChangeRequest = async (id) => {
    setCrResolvingId(id);
    try {
      const res = await fetch(`${API}/admin/change-requests/${id}/reopen`, {
        method: "POST",
        headers: authHeaders(),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || `Error ${res.status}`);
      }
      await loadChangeRequests();
    } catch (err) {
      flashError(`No pude reabrir: ${err.message || err}`);
    } finally {
      setCrResolvingId(null);
    }
  };

  useEffect(() => {
    loadStats();
  }, []);

  useEffect(() => {
    if (tab === "users") loadUsers();
    if (tab === "jobs") loadJobs();
    if (tab === "invoices") loadInvoices();
    if (tab === "compliance") loadCompliance();
    if (tab === "backgrounds") loadBackgrounds();
    if (tab === "costs") loadCostDashboard();
    if (tab === "changes") loadChangeRequests();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab, jobsTenantFilter, jobsStatusFilter, bgListFilter, costSinceDays, costRevenuePerVideo, crStatusFilter]);

  // Auto-refresh the Jobs tab every 5s so admin sees real-time progress
  // of running renders (current_step, progress %). Pauses when the tab
  // is hidden so we don't hammer the API for an inactive screen.
  useEffect(() => {
    if (tab !== "jobs" || !jobsAutoRefresh) return;
    const iv = setInterval(() => {
      if (typeof document !== "undefined" && document.hidden) return;
      loadJobs();
    }, 5000);
    return () => clearInterval(iv);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab, jobsAutoRefresh, jobsTenantFilter, jobsStatusFilter]);

  const loadStats = async () => {
    try {
      const res = await fetch(`${API}/admin/stats`, { headers: authHeaders() });
      setStats(await res.json());
    } catch {} finally { setLoading(false); }
  };

  const [health, setHealth] = useState(null);
  const loadHealth = async () => {
    try {
      const res = await fetch(`${API}/health`);
      setHealth(await res.json());
    } catch {
      setHealth({ status: "error", _fetch_failed: true });
    }
  };
  const [stuckJobs, setStuckJobs] = useState({ count: 0, jobs: [] });
  const loadStuckJobs = async () => {
    try {
      const res = await fetch(`${API}/admin/stuck-jobs?threshold_min=100`, { headers: authHeaders() });
      if (res.ok) setStuckJobs(await res.json());
    } catch {}
  };
  const [reaperRunning, setReaperRunning] = useState(false);
  const [reaperResult, setReaperResult] = useState(null);
  const runReaperNow = async () => {
    if (reaperRunning) return;
    setReaperRunning(true);
    setReaperResult(null);
    try {
      const res = await fetch(`${API}/admin/runbook/reaper-now?threshold_min=100`, {
        method: "POST",
        headers: authHeaders(),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || `Error ${res.status}`);
      }
      const data = await res.json();
      setReaperResult(data);
      // Refresh the stuck count immediately so the banner reflects reality.
      loadStuckJobs();
    } catch (err) {
      setReaperResult({ error: String(err.message || err) });
    } finally {
      setReaperRunning(false);
    }
  };
  useEffect(() => {
    if (tab !== "overview") return;
    loadHealth();
    loadStuckJobs();
    const iv = setInterval(() => { loadHealth(); loadStuckJobs(); }, 15000);
    return () => clearInterval(iv);
  }, [tab]);

  const loadCompliance = async () => {
    try {
      const res = await fetch(`${API}/compliance/status`, { headers: authHeaders() });
      setCompliance(await res.json());
    } catch {}
  };

  const loadBackgrounds = async () => {
    try {
      const q = bgListFilter ? `?owner_tenant_id=${encodeURIComponent(bgListFilter)}` : "";
      const res = await fetch(`${API}/admin/backgrounds${q}`, { headers: authHeaders() });
      setBackgrounds(await res.json());
    } catch {}
    try {
      const res = await fetch(`${API}/admin/background-tenants`, { headers: authHeaders() });
      const data = await res.json();
      setBgTenants(Array.isArray(data?.tenants) ? data.tenants : []);
    } catch {}
  };

  const handleUploadBg = async (file) => {
    if (!file || !bgName.trim()) return;
    setBgUploading(true);
    const formData = new FormData();
    formData.append("file", file);
    formData.append("name", bgName.trim());
    formData.append("tags", bgTags.trim());
    if (bgOwnerTenant) formData.append("owner_tenant_id", bgOwnerTenant);
    try {
      const res = await fetch(`${API}/admin/backgrounds`, { method: "POST", headers: authHeaders(), body: formData });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || `Error ${res.status}`);
      }
      setBgName("");
      setBgTags("");
      // Reset the tenant selector too so the next asset doesn't silently
      // inherit the previous assignment (e.g. a Global upload landing in
      // UMG's library because the dropdown was sticky).
      setBgOwnerTenant("");
      loadBackgrounds();
    } catch (err) {
      flashError(`Subida de background falló: ${err.message || err}`);
    }
    setBgUploading(false);
  };

  const handleDeleteBg = async (id) => {
    if (!window.confirm("Delete this background?")) return;
    try {
      const res = await fetch(`${API}/admin/backgrounds/${id}`, { method: "DELETE", headers: authHeaders() });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || `Error ${res.status}`);
      }
      loadBackgrounds();
    } catch (err) {
      flashError(`Borrar background falló: ${err.message || err}`);
    }
  };

  const loadUsers = async () => {
    try {
      const q = search ? `&search=${encodeURIComponent(search)}` : "";
      const res = await fetch(`${API}/admin/users?limit=100${q}`, { headers: authHeaders() });
      const data = await res.json();
      setUsers(data.users || []);
      setUsersTotal(data.total || 0);
    } catch {}
  };

  const loadJobs = async () => {
    try {
      const tenantQ = jobsTenantFilter ? `&tenant_id=${encodeURIComponent(jobsTenantFilter)}` : "";
      const statusQ = jobsStatusFilter ? `&status=${encodeURIComponent(jobsStatusFilter)}` : "";
      const res = await fetch(`${API}/admin/jobs?limit=200${tenantQ}${statusQ}`, { headers: authHeaders() });
      const data = await res.json();
      setJobs(data.jobs || []);
      setJobsTotal(data.total || 0);
    } catch {}
  };

  const loadInvoices = async () => {
    try {
      const res = await fetch(`${API}/admin/invoices?limit=100`, { headers: authHeaders() });
      const data = await res.json();
      setInvoices(data.invoices || []);
    } catch {}
  };

  const handleCreateUser = async (e) => {
    e.preventDefault();
    setCreateError("");
    try {
      const res = await fetch(`${API}/admin/users`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify(newUser),
      });
      if (!res.ok) {
        const data = await res.json();
        throw new Error(data.detail || "Error");
      }
      setShowCreate(false);
      setNewUser({ username: "", password: "", email: "", plan_id: "100", role: "user", tenant_id: "", allow_overage: false, ai_authorized: false });
      loadUsers();
    } catch (err) {
      setCreateError(err.message);
    }
  };

  // Helper para los 4 PATCH/POST sobre /admin/users/{id} que comparten
  // el mismo manejo de error: chequear res.ok, extraer data.detail,
  // mostrar en el banner. loadUsers() se llama incluso en error porque
  // queremos invalidar cualquier optimistic update local.
  const _patchUser = async (userId, body, errorLabel) => {
    try {
      const res = await fetch(`${API}/admin/users/${userId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || `Error ${res.status}`);
      }
      loadUsers();
    } catch (err) {
      flashError(`${errorLabel}: ${err.message || err}`);
      loadUsers();  // revierte cualquier optimistic local
    }
  };

  const handleToggleUser = (userId, isActive) =>
    _patchUser(userId, { is_active: !isActive }, "Toggle activo falló");

  const handleToggleOverage = (userId, currentValue) =>
    _patchUser(userId, { allow_overage: !currentValue }, "Toggle overage falló");

  const handleChangePlan = (userId, planId) =>
    _patchUser(userId, { plan_id: planId }, "Cambiar plan falló");

  const handleToggleAI = async (userId, isAuthorized) => {
    const endpoint = isAuthorized ? "revoke-ai" : "authorize-ai";
    try {
      const res = await fetch(`${API}/admin/users/${userId}/${endpoint}`, {
        method: "POST",
        headers: authHeaders(),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || `Error ${res.status}`);
      }
      loadUsers();
    } catch (err) {
      flashError(`Toggle AI auth falló: ${err.message || err}`);
      loadUsers();
    }
  };

  const tabs = [
    { id: "overview", label: "Overview" },
    { id: "users", label: "Users" },
    { id: "jobs", label: "Jobs" },
    { id: "changes", label: "Cambios", badge: crPending || (stats?.deliveries?.pending_change_requests ?? 0) },
    { id: "invoices", label: "Invoices" },
    { id: "backgrounds", label: "Backgrounds" },
    { id: "compliance", label: "Compliance" },
    { id: "costs", label: "Costos" },
  ];

  if (loading) return (
    <div className="w-full max-w-5xl animate-fade-in">
      <div className="grid grid-cols-4 gap-4 mb-8">
        {[1,2,3,4].map(i => <div key={i} className="glass rounded-card p-5 h-20 animate-pulse" />)}
      </div>
    </div>
  );

  return (
    <div className="w-full max-w-5xl animate-fade-in">
      {/* Header */}
      <div className="flex items-center gap-3 mb-8">
        <button onClick={onBack}
          className="w-9 h-9 rounded-xl glass flex items-center justify-center text-gray-400 hover:text-white transition-colors">
          <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
            <path d="M19 12H5M12 19l-7-7 7-7" />
          </svg>
        </button>
        <div>
          <h1 className="text-2xl font-bold">Admin Panel</h1>
          <p className="text-sm text-gray-500">Platform management</p>
        </div>
      </div>

      {/* Banner de error de acción admin — visible cuando una mutación
          (delete, toggle, change-plan, etc.) es rechazada por el backend.
          Reemplaza el silencio del bug anterior donde el operador creía
          que la acción se cumplió. Ver flashError(). */}
      {adminError && (
        <div className="mb-4 rounded-card bg-red-500/[0.08] ring-1 ring-red-500/30 px-4 py-3 flex items-start gap-3">
          <svg className="w-5 h-5 text-red-400 shrink-0 mt-0.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
            <circle cx="12" cy="12" r="10" />
            <line x1="12" y1="8" x2="12" y2="12" />
            <line x1="12" y1="16" x2="12.01" y2="16" />
          </svg>
          <div className="flex-1 text-sm text-red-200">{adminError}</div>
          <button
            type="button"
            onClick={() => setAdminError(null)}
            className="text-xs text-red-300 hover:text-red-100 px-2 py-1"
          >
            ✕
          </button>
        </div>
      )}

      {/* Tabs */}
      <div className="flex gap-1 mb-8 glass rounded-xl p-1 w-fit">
        {tabs.map(t => (
          <button key={t.id} onClick={() => setTab(t.id)}
            className={`px-5 py-2 rounded-lg text-sm font-medium transition-all flex items-center gap-2 ${
              tab === t.id ? "bg-brand text-white" : "text-gray-400 hover:text-white"
            }`}>
            {t.label}
            {t.badge > 0 && (
              <span className={`text-[10px] font-bold rounded-full px-1.5 min-w-[1.25rem] text-center ${
                tab === t.id ? "bg-white/20 text-white" : "bg-amber-500/20 text-amber-300"
              }`}>
                {t.badge}
              </span>
            )}
          </button>
        ))}
      </div>

      {/* Overview */}
      {tab === "overview" && stats && (
        <div className="space-y-8">
          {/* Stuck-job banner — shows immediately when zombies exist
              even before the reaper next pass kills them. */}
          {stuckJobs.count > 0 && (
            <div className="rounded-card bg-red-500/[0.08] ring-1 ring-red-500/30 px-5 py-4 flex items-center gap-3 flex-wrap">
              <svg className="w-5 h-5 text-red-300 shrink-0" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                <path d="M12 9v4M12 17h.01"/><circle cx="12" cy="12" r="10"/>
              </svg>
              <div className="flex-1 min-w-0">
                <p className="text-sm font-semibold text-red-200">
                  {stuckJobs.count} {stuckJobs.count === 1 ? "job zombie" : "jobs zombies"} detectado{stuckJobs.count > 1 ? "s" : ""}
                </p>
                <p className="text-xs text-red-300/80 mt-0.5">
                  En "processing" hace más de {stuckJobs.threshold_min} min sin avanzar. El reaper los va a marcar como error en el próximo ciclo (≤5 min), o forzá ahora ↓. Tenants:{" "}
                  {[...new Set(stuckJobs.jobs.map(j => j.tenant_id))].slice(0, 5).join(", ")}
                </p>
              </div>
              <div className="flex items-center gap-2 shrink-0">
                <button
                  onClick={runReaperNow}
                  disabled={reaperRunning}
                  className="text-xs px-3 py-1.5 rounded-lg bg-red-500/20 hover:bg-red-500/30 text-red-100 font-medium disabled:opacity-50 disabled:cursor-not-allowed"
                  title="Marca todos los zombies como error inmediatamente"
                >
                  {reaperRunning ? "Ejecutando…" : "Forzar reaper ahora"}
                </button>
                <button onClick={() => setTab("jobs")} className="text-xs text-red-200 hover:text-white underline shrink-0">
                  Ver
                </button>
              </div>
            </div>
          )}

          {/* Reaper result toast — sticks visible after a manual run so
              the admin sees confirmation of what the runbook did. */}
          {reaperResult && (
            <div className={`rounded-card ring-1 px-5 py-3 flex items-center gap-3 text-sm ${
              reaperResult.error
                ? "bg-red-500/[0.08] ring-red-500/30 text-red-200"
                : reaperResult.count > 0
                  ? "bg-amber-500/[0.06] ring-amber-500/25 text-amber-200"
                  : "bg-accent/[0.06] ring-accent/20 text-accent"
            }`}>
              <svg className="w-4 h-4 shrink-0" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                {reaperResult.error
                  ? <><circle cx="12" cy="12" r="10"/><path d="M15 9l-6 6M9 9l6 6"/></>
                  : <><path d="M22 11.08V12a10 10 0 11-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></>
                }
              </svg>
              <div className="flex-1">
                {reaperResult.error
                  ? <>Falló el runbook: {reaperResult.error}</>
                  : reaperResult.count > 0
                    ? <>Reaper ejecutado · {reaperResult.count} job{reaperResult.count > 1 ? "s" : ""} marcado{reaperResult.count > 1 ? "s" : ""} como error.</>
                    : <>Reaper ejecutado · ningún zombie encontrado.</>
                }
              </div>
              <button onClick={() => setReaperResult(null)} className="text-xs opacity-60 hover:opacity-100">×</button>
            </div>
          )}

          {/* System status — live, refreshes every 15s ─────────── */}
          {health && (() => {
            const statusColor = health.status === "ok"
              ? "text-accent" : health.status === "degraded"
              ? "text-amber-400" : "text-red-400";
            const statusBg = health.status === "ok"
              ? "bg-accent/[0.06] ring-accent/20" : health.status === "degraded"
              ? "bg-amber-500/[0.06] ring-amber-500/25" : "bg-red-500/[0.06] ring-red-500/30";
            const Pill = ({ ok, label, value }) => (
              <div className="flex items-center gap-2 text-xs">
                <span className={`w-2 h-2 rounded-full ${ok ? "bg-accent" : "bg-red-400"}`} />
                <span className="text-gray-400">{label}</span>
                <span className="font-mono text-white">{value}</span>
              </div>
            );
            const queue = health.queue_depth || {};
            const totalQueue = (queue.enterprise ?? 0) + (queue.default ?? 0);
            return (
              <div className={`rounded-card ring-1 ${statusBg} px-5 py-4`}>
                <div className="flex items-center justify-between mb-3">
                  <div className="flex items-center gap-2">
                    <span className={`text-xs font-bold uppercase tracking-wider ${statusColor}`}>
                      System {health.status}
                    </span>
                    {health.degraded_reason && (
                      <span className="text-xs text-amber-300">· {health.degraded_reason}</span>
                    )}
                  </div>
                  <span className="text-[10px] text-gray-500">live · refresh 15s</span>
                </div>
                <div className="grid grid-cols-2 md:grid-cols-4 gap-x-4 gap-y-2">
                  <Pill ok={health.db === "up"} label="Postgres" value={health.db || "?"} />
                  <Pill ok={health.redis === "up"} label="Redis" value={health.redis || "?"} />
                  <Pill ok={health.r2 === "configured"} label="R2 storage" value={health.r2 || "?"} />
                  <Pill ok={(health.workers_alive || 0) > 0} label="Workers" value={health.workers_alive ?? "?"} />
                  <Pill ok={totalQueue < 50} label="Cola jobs" value={`${totalQueue} (ent ${queue.enterprise ?? 0} + def ${queue.default ?? 0})`} />
                  <Pill ok={(health.disk_free_gb ?? 0) > 10} label="Disco libre" value={`${health.disk_free_gb ?? "?"} GB`} />
                  <Pill ok={!!health.api_keys?.openai} label="OpenAI" value={health.api_keys?.openai ? "ok" : "missing"} />
                  <Pill ok={!!health.api_keys?.vertex} label="Vertex (Veo+Gemini)" value={health.api_keys?.vertex ? "ok" : "missing"} />
                </div>
              </div>
            );
          })()}

          {/* Compliance warning banner */}
          {stats.jobs?.pending_review > 0 && (
            <div className="rounded-2xl bg-amber-500/5 border border-amber-500/20 px-5 py-4 flex items-center gap-3">
              <svg className="w-5 h-5 text-amber-400 shrink-0" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                <circle cx="12" cy="12" r="10" /><path d="M12 8v4M12 16h.01" />
              </svg>
              <p className="text-sm text-amber-300">
                <span className="font-bold">{stats.jobs.pending_review}</span> video{stats.jobs.pending_review > 1 ? "s" : ""} pending review — approval required before download/publish (UMG Guideline 16)
              </p>
            </div>
          )}

          <div className="grid grid-cols-2 md:grid-cols-5 gap-4">
            <StatCard value={stats.users?.total || 0} label="Total Users" color="text-brand" />
            <StatCard value={stats.jobs?.done || 0} label="Videos Generated" color="text-accent" />
            <StatCard value={stats.jobs?.pending_review || 0} label="Pending Review" color="text-amber-400" />
            <StatCard value={stats.jobs?.processing || 0} label="Processing" />
            <StatCard value={`$${(stats.revenue?.total || 0).toLocaleString()}`} label="Total Revenue" color="text-brand-light" />
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
            {/* Monthly */}
            <div className="glass-elevated rounded-card p-6">
              <h3 className="text-sm font-semibold mb-4">This Month</h3>
              <div className="space-y-3">
                <div className="flex justify-between">
                  <span className="text-xs text-gray-400">Videos</span>
                  <span className="text-xs font-bold">{stats.jobs?.this_month || 0}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-xs text-gray-400">Revenue</span>
                  <span className="text-xs font-bold text-accent">${(stats.revenue?.this_month || 0).toLocaleString()}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-xs text-gray-400">Errors</span>
                  <span className="text-xs font-bold text-red-400">{stats.jobs?.errors || 0}</span>
                </div>
              </div>
            </div>

            {/* Plan distribution */}
            <div className="glass-elevated rounded-card p-6">
              <h3 className="text-sm font-semibold mb-4">Plan Distribution</h3>
              <div className="space-y-2">
                {Object.entries(stats.plans || {}).map(([plan, count]) => (
                  <div key={plan} className="flex items-center justify-between">
                    <span className="text-xs text-gray-400">Plan {plan}</span>
                    <div className="flex items-center gap-2">
                      <div className="w-20 h-1.5 bg-surface-3/50 rounded-full overflow-hidden">
                        <div className="h-full bg-brand rounded-full"
                          style={{ width: `${Math.min(100, (count / Math.max(1, stats.users?.total)) * 100)}%` }} />
                      </div>
                      <span className="text-xs font-bold w-6 text-right">{count}</span>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Users */}
      {tab === "users" && (
        <div className="space-y-4">
          <div className="flex items-center gap-3">
            <input type="text" value={search} onChange={e => setSearch(e.target.value)}
              onKeyDown={e => e.key === "Enter" && loadUsers()}
              className="input-field flex-1 !py-2.5 text-sm" placeholder="Search users..." />
            <button onClick={loadUsers} className="btn-secondary !py-2.5 text-sm">Search</button>
            <button onClick={() => setShowCreate(true)} className="btn-primary !py-2.5 text-sm">+ New User</button>
          </div>

          <p className="text-xs text-gray-500">{usersTotal} users total</p>

          <div className="glass rounded-card overflow-hidden">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-white/[0.06]">
                  <th className="text-left px-4 py-3 text-xs text-gray-500 font-medium">User</th>
                  <th className="text-left px-4 py-3 text-xs text-gray-500 font-medium">Email</th>
                  <th className="text-left px-4 py-3 text-xs text-gray-500 font-medium">Plan</th>
                  <th className="text-left px-4 py-3 text-xs text-gray-500 font-medium">Jobs</th>
                  <th className="text-left px-4 py-3 text-xs text-gray-500 font-medium">Created</th>
                  <th className="text-left px-4 py-3 text-xs text-gray-500 font-medium">Actions</th>
                </tr>
              </thead>
              <tbody>
                {users.map(u => (
                  <tr key={u.id} className={`border-b border-white/[0.03] ${!u.is_active ? "opacity-40" : ""}`}>
                    <td className="px-4 py-3">
                      <div className="flex items-center gap-2">
                        <div className="w-6 h-6 rounded-lg bg-brand/20 flex items-center justify-center">
                          <span className="text-[10px] font-bold text-brand uppercase">{u.username?.[0]}</span>
                        </div>
                        <span className="font-medium">{u.username}</span>
                        {u.role === "admin" && <span className="text-[9px] bg-brand/20 text-brand px-1.5 py-0.5 rounded-full font-bold uppercase">Admin</span>}
                        {u.ai_authorized && <span className="text-[9px] bg-accent/15 text-accent px-1.5 py-0.5 rounded-full font-bold uppercase">AI</span>}
                        {u.allow_overage && <span className="text-[9px] bg-amber-500/15 text-amber-300 px-1.5 py-0.5 rounded-full font-bold uppercase" title="Puede pasar el cap mensual y se factura el extra">Overage</span>}
                      </div>
                    </td>
                    <td className="px-4 py-3 text-gray-400">{u.email || "—"}</td>
                    <td className="px-4 py-3">
                      <select value={u.plan} onChange={e => handleChangePlan(u.id, e.target.value)}
                        className="bg-surface-1 border border-white/[0.06] rounded-lg px-2 py-1 text-xs">
                        {["free","100","250","500","1000","unlimited"].map(p => (
                          <option key={p} value={p}>{p}</option>
                        ))}
                      </select>
                    </td>
                    <td className="px-4 py-3 text-gray-400">{u.job_count || 0}</td>
                    <td className="px-4 py-3 text-gray-500 text-xs">{fmtDate(u.created_at)}</td>
                    <td className="px-4 py-3">
                      <div className="flex gap-1 flex-wrap">
                        <button onClick={() => handleToggleAI(u.id, u.ai_authorized)}
                          className={`text-[10px] px-2 py-1 rounded-lg font-medium ${u.ai_authorized ? "text-amber-400 hover:bg-amber-500/10" : "text-accent hover:bg-accent/10"}`}>
                          {u.ai_authorized ? "Revoke AI" : "Auth AI"}
                        </button>
                        <button onClick={() => handleToggleOverage(u.id, u.allow_overage)}
                          className={`text-[10px] px-2 py-1 rounded-lg font-medium ${u.allow_overage ? "text-amber-400 hover:bg-amber-500/10" : "text-gray-400 hover:bg-white/[0.04]"}`}
                          title="Permitir pasar el cap mensual con cargo por video extra">
                          {u.allow_overage ? "Stop Overage" : "Allow Overage"}
                        </button>
                        <button onClick={() => handleToggleUser(u.id, u.is_active)}
                          className={`text-[10px] px-2 py-1 rounded-lg ${u.is_active ? "text-red-400 hover:bg-red-500/10" : "text-accent hover:bg-accent/10"}`}>
                          {u.is_active ? "Disable" : "Enable"}
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* Create user modal */}
          {showCreate && (
            <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
              <div className="glass rounded-3xl p-8 w-full max-w-md animate-fade-in">
                <h3 className="text-lg font-bold mb-6">Create User</h3>
                <form onSubmit={handleCreateUser} className="space-y-4">
                  <input type="text" placeholder="Username" value={newUser.username}
                    onChange={e => setNewUser({...newUser, username: e.target.value})}
                    className="input-field !py-3 text-sm" required />
                  <input type="email" placeholder="Email" value={newUser.email}
                    onChange={e => setNewUser({...newUser, email: e.target.value})}
                    className="input-field !py-3 text-sm" />
                  <input type="password" placeholder="Password" value={newUser.password}
                    onChange={e => setNewUser({...newUser, password: e.target.value})}
                    className="input-field !py-3 text-sm" required />
                  <input type="text" placeholder="Tenant ID (deja vacío para que sea único por user)"
                    value={newUser.tenant_id}
                    onChange={e => setNewUser({...newUser, tenant_id: e.target.value})}
                    className="input-field !py-3 text-sm" />
                  <p className="text-[11px] text-gray-500 -mt-2">
                    Mismo tenant ID = el equipo comparte historial / videos. Vacío = aislado.
                  </p>
                  <select value={newUser.plan_id}
                    onChange={e => setNewUser({...newUser, plan_id: e.target.value})}
                    className="input-field !py-3 text-sm">
                    {["free","100","250","500","1000","unlimited"].map(p => (
                      <option key={p} value={p}>Plan {p}</option>
                    ))}
                  </select>
                  <select value={newUser.role}
                    onChange={e => setNewUser({...newUser, role: e.target.value})}
                    className="input-field !py-3 text-sm">
                    <option value="user">User</option>
                    <option value="admin">Admin</option>
                  </select>
                  <label className="flex items-start gap-2 text-sm text-gray-300 cursor-pointer select-none">
                    <input
                      type="checkbox"
                      checked={newUser.allow_overage || false}
                      onChange={e => setNewUser({...newUser, allow_overage: e.target.checked})}
                      className="accent-amber-500 mt-0.5"
                    />
                    <span>
                      Permitir overage
                      <span className="block text-[11px] text-gray-500 mt-0.5">
                        El usuario puede pasar el cap mensual; los videos extra se facturan al cierre.
                      </span>
                    </span>
                  </label>
                  <label className="flex items-start gap-2 text-sm text-gray-300 cursor-pointer select-none">
                    <input
                      type="checkbox"
                      checked={newUser.ai_authorized || false}
                      onChange={e => setNewUser({...newUser, ai_authorized: e.target.checked})}
                      className="accent-brand mt-0.5"
                    />
                    <span>
                      Autorizar uso de IA
                      <span className="block text-[11px] text-gray-500 mt-0.5">
                        Permite generar variaciones y fondos con IA (UMG Guideline 5). Si lo dejás sin tildar podés autorizarlo después desde la lista.
                      </span>
                    </span>
                  </label>
                  {createError && <p className="text-sm text-red-400">{createError}</p>}
                  <div className="flex gap-3 pt-2">
                    <button type="submit" className="btn-primary flex-1 !py-3 text-sm">Create</button>
                    <button type="button" onClick={() => setShowCreate(false)} className="btn-secondary flex-1 !py-3 text-sm">Cancel</button>
                  </div>
                </form>
              </div>
            </div>
          )}
        </div>
      )}

      {/* Jobs */}
      {tab === "jobs" && (
        <div className="space-y-4">
          <div className="flex items-center justify-between gap-3 flex-wrap">
            <p className="text-xs text-gray-500">
              {jobsTotal} jobs
              {jobsTenantFilter && <> en tenant <span className="text-brand">{jobsTenantFilter}</span></>}
              {jobsStatusFilter && <> con status <span className="text-brand">{jobsStatusFilter}</span></>}
            </p>
            <div className="flex items-center gap-3">
              <select
                value={jobsStatusFilter}
                onChange={e => setJobsStatusFilter(e.target.value)}
                className="input-field !py-2 text-xs"
              >
                <option value="">Todos los status</option>
                <option value="done">done (aprobado)</option>
                <option value="pending_review">pending_review</option>
                <option value="processing">processing</option>
                <option value="queued">queued</option>
                <option value="error">error</option>
                <option value="rejected">rejected</option>
                <option value="validation_failed">validation_failed</option>
              </select>
              <input
                type="text"
                placeholder="Filtrar por tenant_id (ej: universal_music)"
                value={jobsTenantFilter}
                onChange={e => setJobsTenantFilter(e.target.value.trim())}
                className="input-field !py-2 text-xs w-72"
              />
              <label className="flex items-center gap-1.5 text-xs text-gray-400 cursor-pointer select-none">
                <input
                  type="checkbox"
                  checked={jobsAutoRefresh}
                  onChange={e => setJobsAutoRefresh(e.target.checked)}
                  className="accent-brand"
                />
                Auto-refresh 5s
              </label>
            </div>
          </div>
          <div className="glass rounded-card overflow-hidden overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-white/[0.06]">
                  <th className="text-left px-4 py-3 text-xs text-gray-500 font-medium">Job ID</th>
                  <th className="text-left px-4 py-3 text-xs text-gray-500 font-medium">Artist</th>
                  <th className="text-left px-4 py-3 text-xs text-gray-500 font-medium">File</th>
                  <th className="text-left px-4 py-3 text-xs text-gray-500 font-medium">Tenant</th>
                  <th className="text-left px-4 py-3 text-xs text-gray-500 font-medium">Status</th>
                  <th className="text-left px-4 py-3 text-xs text-gray-500 font-medium">Step</th>
                  <th className="text-left px-4 py-3 text-xs text-gray-500 font-medium w-[140px]">Progress</th>
                  <th className="text-left px-4 py-3 text-xs text-gray-500 font-medium">Created</th>
                </tr>
              </thead>
              <tbody>
                {jobs.map(j => (
                  <tr key={j.job_id} className="border-b border-white/[0.03]">
                    <td className="px-4 py-3 font-mono text-xs text-gray-400">{j.job_id?.slice(0, 8)}…</td>
                    <td className="px-4 py-3">{j.artist}</td>
                    <td className="px-4 py-3 text-gray-400 truncate max-w-[200px]">{j.filename}</td>
                    <td className="px-4 py-3 text-xs text-gray-500">{j.tenant_id}</td>
                    <td className="px-4 py-3">
                      <span className={`text-xs px-2 py-1 rounded-lg font-medium ${
                        j.status === "done" ? "bg-accent/10 text-accent" :
                        j.status === "error" ? "bg-red-500/10 text-red-400" :
                        j.status === "validation_failed" ? "bg-red-500/10 text-red-300" :
                        j.status === "pending_review" ? "bg-amber-500/10 text-amber-300" :
                        "bg-brand/10 text-brand"
                      }`}>{j.status}</span>
                    </td>
                    <td className="px-4 py-3 text-xs text-gray-400">
                      {j.current_step || "—"}
                    </td>
                    <td className="px-4 py-3">
                      {typeof j.progress === "number" && j.status !== "done" && j.status !== "error" ? (
                        <div className="flex items-center gap-2">
                          <div className="flex-1 h-1.5 bg-surface-3/60 rounded-full overflow-hidden min-w-[60px]">
                            <div
                              className="h-full bg-gradient-to-r from-brand to-brand-light transition-all"
                              style={{ width: `${Math.max(2, Math.min(100, j.progress))}%` }}
                            />
                          </div>
                          <span className="text-[10px] text-gray-500 tabular-nums w-8 text-right">{j.progress}%</span>
                        </div>
                      ) : (
                        <span className="text-xs text-gray-600">—</span>
                      )}
                    </td>
                    <td className="px-4 py-3 text-xs text-gray-500 whitespace-nowrap">
                      {j.created_at ? new Date(j.created_at * 1000).toLocaleString("es-AR") : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Cambios — change requests from the portal (UMG asks for edits) */}
      {tab === "changes" && (
        <div className="space-y-5">
          {/* Headline counters */}
          <div className="grid grid-cols-2 gap-4 max-w-md">
            <StatCard value={crPending} label="Pendientes" color={crPending > 0 ? "text-amber-300" : "text-gray-400"} />
            <StatCard value={crResolved} label="Resueltos" color="text-emerald-300" />
          </div>

          {/* Filter pills */}
          <div className="flex items-center gap-2">
            {[
              { id: "pending", label: "Pendientes" },
              { id: "resolved", label: "Resueltos" },
              { id: "all", label: "Todos" },
            ].map(opt => (
              <button
                key={opt.id}
                onClick={() => setCrStatusFilter(opt.id)}
                className={`text-xs px-3 py-1.5 rounded-full font-medium transition ${
                  crStatusFilter === opt.id
                    ? "bg-brand text-white"
                    : "bg-surface-3/40 text-gray-400 hover:text-white hover:bg-surface-3/70"
                }`}
              >
                {opt.label}
              </button>
            ))}
            <button
              onClick={loadChangeRequests}
              className="ml-auto text-xs text-gray-400 hover:text-white px-3 py-1.5"
              title="Recargar"
            >
              {crLoading ? "Cargando…" : "↻ Recargar"}
            </button>
          </div>

          {/* List */}
          {crLoading && crItems.length === 0 ? (
            <div className="glass rounded-card p-8 text-center text-sm text-gray-500">
              Cargando cambios…
            </div>
          ) : crItems.length === 0 ? (
            <div className="glass rounded-card p-8 text-center text-sm text-gray-500">
              {crStatusFilter === "pending"
                ? "No hay pedidos de cambio pendientes. ✓"
                : crStatusFilter === "resolved"
                ? "Todavía no se resolvió ningún pedido."
                : "Sin pedidos de cambio."}
            </div>
          ) : (
            <div className="space-y-3">
              {crItems.map(item => {
                const d = item.delivery;
                const isResolved = !!item.resolved_at;
                const submitted = item.submitted_at
                  ? new Date(item.submitted_at).toLocaleString("es-AR", { dateStyle: "short", timeStyle: "short" })
                  : "—";
                const resolved = item.resolved_at
                  ? new Date(item.resolved_at).toLocaleString("es-AR", { dateStyle: "short", timeStyle: "short" })
                  : null;
                const draft = crResolveDraft[item.id] || "";
                return (
                  <div
                    key={item.id}
                    className={`glass rounded-card p-5 border-l-4 ${
                      isResolved ? "border-emerald-500/60 opacity-75" : "border-amber-400"
                    }`}
                  >
                    {/* Top: delivery context */}
                    <div className="flex items-start justify-between gap-3 mb-3 flex-wrap">
                      <div className="min-w-0">
                        <p className="text-[10px] uppercase tracking-wider text-brand font-bold mb-0.5">
                          {d?.artist || "(sin artista)"}
                        </p>
                        <h3 className="text-base font-bold leading-snug">
                          {d?.song || "(canción eliminada)"}
                        </h3>
                        <div className="flex items-center gap-2 mt-1 flex-wrap text-[11px] text-gray-500">
                          {d?.label && <span>{d.label}</span>}
                          {d?.frame_size && (
                            <>
                              <span>·</span>
                              <span className="text-brand-light">{d.frame_size}</span>
                            </>
                          )}
                          {d?.job_id && (
                            <>
                              <span>·</span>
                              <span className="font-mono">job {d.job_id}</span>
                            </>
                          )}
                          {d?.tenant && (
                            <>
                              <span>·</span>
                              <span>{d.tenant}</span>
                            </>
                          )}
                          {d?.removed_at && (
                            <span className="text-red-300">· entrega eliminada</span>
                          )}
                        </div>
                      </div>
                      <span
                        className={`text-[10px] font-bold uppercase px-2 py-1 rounded-full shrink-0 ${
                          isResolved
                            ? "bg-emerald-500/15 text-emerald-300"
                            : "bg-amber-500/15 text-amber-300"
                        }`}
                      >
                        {isResolved ? "✓ Resuelto" : "⏳ Pendiente"}
                      </span>
                    </div>

                    {/* Comment */}
                    <div className="rounded-lg bg-surface-3/30 ring-1 ring-white/[0.04] p-3 text-sm leading-relaxed whitespace-pre-wrap">
                      {item.comment}
                    </div>

                    {/* Submission meta */}
                    <p className="text-[11px] text-gray-500 mt-2">
                      UMG envió este pedido el {submitted}
                    </p>

                    {/* Resolution section */}
                    {isResolved ? (
                      <div className="mt-3 pt-3 border-t border-white/[0.06] flex items-start justify-between gap-3 flex-wrap">
                        <div className="text-[11px] text-gray-400 min-w-0">
                          <span className="text-emerald-300 font-medium">Resuelto</span>
                          {item.resolved_by && <> por <b>{item.resolved_by}</b></>}
                          {" "}el {resolved}
                          {item.resolution_note && (
                            <p className="mt-1 text-gray-300 whitespace-pre-wrap">
                              <span className="text-gray-500">Respuesta: </span>
                              {item.resolution_note}
                            </p>
                          )}
                        </div>
                        <button
                          onClick={() => reopenChangeRequest(item.id)}
                          disabled={crResolvingId === item.id}
                          className="text-[11px] text-amber-300 hover:text-amber-200 disabled:opacity-50"
                        >
                          Reabrir
                        </button>
                      </div>
                    ) : (
                      <div className="mt-3 pt-3 border-t border-white/[0.06] space-y-2">
                        <input
                          type="text"
                          placeholder="Respuesta opcional (ej: re-renderizado con la línea corregida)"
                          value={draft}
                          onChange={(e) =>
                            setCrResolveDraft({ ...crResolveDraft, [item.id]: e.target.value })
                          }
                          className="input-field !py-2 text-xs w-full"
                          maxLength={2000}
                        />
                        <div className="flex justify-end">
                          <button
                            onClick={() => resolveChangeRequest(item.id)}
                            disabled={crResolvingId === item.id}
                            className="btn-primary !py-1.5 !px-3 text-xs disabled:opacity-50"
                          >
                            {crResolvingId === item.id ? "Guardando…" : "Marcar resuelto"}
                          </button>
                        </div>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}

      {/* Invoices */}
      {tab === "invoices" && (
        <div className="space-y-4">
          <div className="glass rounded-card overflow-hidden">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-white/[0.06]">
                  <th className="text-left px-4 py-3 text-xs text-gray-500 font-medium">Date</th>
                  <th className="text-left px-4 py-3 text-xs text-gray-500 font-medium">Description</th>
                  <th className="text-left px-4 py-3 text-xs text-gray-500 font-medium">Amount</th>
                  <th className="text-left px-4 py-3 text-xs text-gray-500 font-medium">Status</th>
                  <th className="text-left px-4 py-3 text-xs text-gray-500 font-medium">Invoice</th>
                </tr>
              </thead>
              <tbody>
                {invoices.map(inv => (
                  <tr key={inv.id} className="border-b border-white/[0.03]">
                    <td className="px-4 py-3 text-xs text-gray-400">{fmtDate(inv.created_at)}</td>
                    <td className="px-4 py-3">{inv.description || "—"}</td>
                    <td className="px-4 py-3 font-medium">${inv.amount?.toFixed(2)}</td>
                    <td className="px-4 py-3">
                      <span className={`text-xs px-2 py-1 rounded-lg font-medium ${
                        inv.status === "paid" ? "bg-accent/10 text-accent" :
                        inv.status === "failed" ? "bg-red-500/10 text-red-400" :
                        "bg-amber-500/10 text-amber-400"
                      }`}>{inv.status}</span>
                    </td>
                    <td className="px-4 py-3">
                      {inv.invoice_url ? (
                        <a href={inv.invoice_url} target="_blank" rel="noopener noreferrer"
                          className="text-xs text-brand hover:text-brand-light">View</a>
                      ) : "—"}
                    </td>
                  </tr>
                ))}
                {invoices.length === 0 && (
                  <tr><td colSpan={5} className="px-4 py-8 text-center text-gray-500">No invoices yet</td></tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Backgrounds */}
      {tab === "backgrounds" && (
        <div className="space-y-6">
          {/* Upload form */}
          <div className="glass-elevated rounded-card p-6">
            <h3 className="text-sm font-semibold mb-4">Upload Background</h3>
            <div className="flex gap-3 items-end flex-wrap">
              <div className="flex-1 min-w-[180px]">
                <label className="text-[10px] text-gray-500 uppercase tracking-wider">Name</label>
                <input
                  type="text" value={bgName} onChange={(e) => setBgName(e.target.value)}
                  placeholder="e.g. Ocean Sunset Loop"
                  className="w-full mt-1 px-3 py-2 rounded-lg bg-surface-1 border border-white/[0.06] focus:border-brand/50 focus:outline-none text-sm text-white placeholder-gray-500"
                />
              </div>
              <div className="flex-1 min-w-[180px]">
                <label className="text-[10px] text-gray-500 uppercase tracking-wider">Tags (comma-separated)</label>
                <input
                  type="text" value={bgTags} onChange={(e) => setBgTags(e.target.value)}
                  placeholder="e.g. ocean,sunset,calm"
                  className="w-full mt-1 px-3 py-2 rounded-lg bg-surface-1 border border-white/[0.06] focus:border-brand/50 focus:outline-none text-sm text-white placeholder-gray-500"
                />
              </div>
              <div className="min-w-[180px]">
                <label className="text-[10px] text-gray-500 uppercase tracking-wider">Assign to tenant</label>
                <select
                  value={bgOwnerTenant}
                  onChange={(e) => setBgOwnerTenant(e.target.value)}
                  className="w-full mt-1 px-3 py-2 rounded-lg bg-surface-1 border border-white/[0.06] focus:border-brand/50 focus:outline-none text-sm text-white"
                >
                  <option value="">Global (visible to all)</option>
                  {bgTenants.map((tid) => (
                    <option key={tid} value={tid}>{tid}</option>
                  ))}
                </select>
              </div>
              <div>
                <label className={`btn-primary text-sm py-2 px-4 cursor-pointer ${bgUploading || !bgName.trim() ? "opacity-50 pointer-events-none" : ""}`}>
                  {bgUploading ? "Uploading..." : "Upload"}
                  <input
                    type="file" accept=".mp4,.mov,.jpg,.jpeg,.png" className="hidden"
                    disabled={bgUploading || !bgName.trim()}
                    onChange={(e) => { if (e.target.files[0]) handleUploadBg(e.target.files[0]); e.target.value = ""; }}
                  />
                </label>
              </div>
            </div>
            <p className="text-[10px] text-gray-600 mt-2">
              MP4, MOV, JPG, or PNG. <strong>Global</strong> assets are visible to every tenant; assigning to a tenant locks the asset to that tenant only (e.g. Universal Music exclusivity).
            </p>
          </div>

          {/* Library grid */}
          <div className="glass-elevated rounded-card p-6">
            <div className="flex items-center justify-between mb-4 gap-3 flex-wrap">
              <h3 className="text-sm font-semibold">Background Library</h3>
              <div className="flex items-center gap-3">
                <select
                  value={bgListFilter}
                  onChange={(e) => setBgListFilter(e.target.value)}
                  className="px-2 py-1.5 rounded-lg bg-surface-1 border border-white/[0.06] focus:border-brand/50 focus:outline-none text-xs text-white"
                >
                  <option value="">All tenants</option>
                  <option value="__global__">Global only</option>
                  {bgTenants.map((tid) => (
                    <option key={tid} value={tid}>{tid}</option>
                  ))}
                </select>
                <span className="text-xs text-gray-500">{backgrounds.length} asset{backgrounds.length !== 1 ? "s" : ""}</span>
              </div>
            </div>
            {backgrounds.length === 0 ? (
              <p className="text-center text-gray-500 text-sm py-8">No backgrounds uploaded yet</p>
            ) : (
              <div className="grid grid-cols-3 gap-4">
                {backgrounds.map((bg) => (
                  <div key={bg.id} className="glass rounded-xl overflow-hidden group relative">
                    <div className="aspect-video bg-black/30 flex items-center justify-center">
                      {bg.file_type === "mp4" ? (
                        <video
                          src={`${API}/backgrounds/${bg.id}/preview?token=${encodeURIComponent(localStorage.getItem("genly_token") || "")}`}
                          className="w-full h-full object-cover"
                          muted autoPlay loop playsInline
                        />
                      ) : (
                        <img
                          src={`${API}/backgrounds/${bg.id}/preview?token=${encodeURIComponent(localStorage.getItem("genly_token") || "")}`}
                          className="w-full h-full object-cover"
                          alt={bg.name}
                        />
                      )}
                    </div>
                    <div className="px-3 py-2">
                      <div className="flex items-center justify-between gap-2">
                        <p className="text-xs font-medium text-white truncate">{bg.name}</p>
                        <span
                          className={`shrink-0 px-1.5 py-0.5 rounded text-[9px] uppercase tracking-wider ${
                            bg.owner_tenant_id
                              ? "bg-brand/15 text-brand-light ring-1 ring-brand/30"
                              : "bg-surface-1 text-gray-500"
                          }`}
                          title={bg.owner_tenant_id ? `Exclusive to tenant: ${bg.owner_tenant_id}` : "Visible to all tenants"}
                        >
                          {bg.owner_tenant_id || "global"}
                        </span>
                      </div>
                      <div className="flex items-center justify-between mt-1">
                        <div className="flex gap-1 flex-wrap">
                          {bg.tags?.map((tag, i) => (
                            <span key={i} className="px-1.5 py-0.5 rounded bg-surface-1 text-[9px] text-gray-500">{tag}</span>
                          ))}
                        </div>
                        <button
                          onClick={() => handleDeleteBg(bg.id)}
                          className="w-6 h-6 rounded-md hover:bg-red-500/10 flex items-center justify-center text-gray-600 hover:text-red-400 transition-colors opacity-0 group-hover:opacity-100"
                        >
                          <svg className="w-3 h-3" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                            <path d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/>
                          </svg>
                        </button>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}

      {/* Compliance */}
      {tab === "compliance" && (
        <div className="space-y-6">
          {!compliance ? (
            <div className="flex items-center justify-center py-12">
              <div className="w-6 h-6 border-2 border-brand border-t-transparent rounded-full animate-spin" />
            </div>
          ) : (
            <>
              <div className="glass-elevated rounded-card p-6">
                <h3 className="text-sm font-semibold mb-1">UMG Compliance Status</h3>
                <p className="text-[11px] text-gray-500 mb-5">{compliance.guidelines_version}</p>

                <div className="space-y-3">
                  {Object.entries(compliance.checks || {}).map(([key, check]) => (
                    <div key={key} className={`rounded-xl px-4 py-3 border ${
                      check.status === "ok" || check.status === "confirmed"
                        ? "border-accent/20 bg-accent/5"
                        : check.status === "pending"
                        ? "border-amber-500/30 bg-amber-500/5"
                        : "border-red-500/20 bg-red-500/5"
                    }`}>
                      <div className="flex items-start gap-3">
                        <div className={`w-6 h-6 rounded-full flex items-center justify-center shrink-0 mt-0.5 ${
                          check.status === "ok" || check.status === "confirmed"
                            ? "bg-accent/20"
                            : check.status === "pending"
                            ? "bg-amber-500/20"
                            : "bg-red-500/20"
                        }`}>
                          {(check.status === "ok" || check.status === "confirmed") && (
                            <svg className="w-3.5 h-3.5 text-accent" fill="none" stroke="currentColor" strokeWidth="2.5" viewBox="0 0 24 24">
                              <polyline points="20 6 9 17 4 12" />
                            </svg>
                          )}
                          {check.status === "pending" && (
                            <svg className="w-3.5 h-3.5 text-amber-400" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                              <circle cx="12" cy="12" r="10" /><path d="M12 8v4M12 16h.01" />
                            </svg>
                          )}
                        </div>
                        <div className="flex-1 min-w-0">
                          <p className="text-xs font-medium text-white">{key.replace(/_/g, " ").replace("guideline ", "Guideline ")}</p>
                          <p className="text-[11px] text-gray-400 mt-0.5">{check.detail}</p>
                        </div>
                        <span className={`text-[10px] font-bold uppercase px-2 py-0.5 rounded shrink-0 ${
                          check.status === "ok" || check.status === "confirmed"
                            ? "bg-accent/10 text-accent"
                            : check.status === "pending"
                            ? "bg-amber-500/10 text-amber-400"
                            : "bg-red-500/10 text-red-400"
                        }`}>
                          {check.status}
                        </span>
                      </div>
                    </div>
                  ))}
                </div>
              </div>

              <div className="glass-elevated rounded-card p-6">
                <h3 className="text-sm font-semibold mb-4">Data Policy</h3>
                <p className="text-[11px] text-gray-400 mb-4">
                  View the full data policy at <button
                    onClick={() => window.open(`${API}/compliance/data-policy`, "_blank")}
                    className="text-brand hover:text-brand-light underline">
                    /compliance/data-policy
                  </button>
                </p>
                <div className="flex gap-3">
                  <a
                    href={`${API}/compliance/data-policy`}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="btn-secondary text-xs py-2 px-4"
                  >
                    View Data Policy JSON
                  </a>
                  <a
                    href={`${API}/compliance/status`}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="btn-secondary text-xs py-2 px-4"
                  >
                    View Compliance Status JSON
                  </a>
                </div>
              </div>
            </>
          )}
        </div>
      )}

      {/* Costos — panel de margen del operador */}
      {tab === "costs" && (
        <div className="space-y-6">
          {/* Period + revenue selectors */}
          <div className="glass rounded-card p-4 flex items-center gap-4 flex-wrap">
            <div className="flex items-center gap-2">
              <span className="text-[11px] text-gray-500 uppercase tracking-wide">Período</span>
              {[7, 30, 90].map((d) => (
                <button
                  key={d}
                  onClick={() => setCostSinceDays(d)}
                  className={`px-3 py-1 rounded-md text-xs ring-1 transition-colors ${
                    costSinceDays === d
                      ? "bg-brand/20 ring-brand/40 text-white"
                      : "ring-white/[0.06] text-gray-400 hover:text-white"
                  }`}
                >
                  {d}d
                </button>
              ))}
            </div>
            <div className="flex items-center gap-2 ml-auto">
              <span className="text-[11px] text-gray-500 uppercase tracking-wide">Revenue / video</span>
              <span className="text-xs text-gray-400">USD</span>
              <input
                type="number"
                step="0.5"
                min="0"
                value={costRevenuePerVideo}
                onChange={(e) => setCostRevenuePerVideo(Math.max(0, Number(e.target.value) || 0))}
                className="w-20 bg-surface-3/40 ring-1 ring-white/[0.06] focus:ring-brand/40 focus:outline-none rounded-md px-2 py-1 text-xs text-white text-right"
              />
            </div>
          </div>

          {costLoading || !costDashboard ? (
            <div className="flex items-center justify-center py-12">
              <div className="w-6 h-6 border-2 border-brand border-t-transparent rounded-full animate-spin" />
            </div>
          ) : (
            <>
              {/* Headline cards: spend, deliverable count, cost/video, margin */}
              <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
                <div className="glass-elevated rounded-card p-5">
                  <p className="text-[10px] text-gray-500 uppercase tracking-wider mb-1">Gasto IA total</p>
                  <p className="text-2xl font-bold tabular-nums">${costDashboard.total_cost.toFixed(2)}</p>
                  <p className="text-[11px] text-gray-500 mt-1">{costDashboard.total_calls} calls · últimos {costDashboard.since_days}d</p>
                </div>
                <div className="glass-elevated rounded-card p-5">
                  <p className="text-[10px] text-gray-500 uppercase tracking-wider mb-1">Videos deliverable</p>
                  <p className="text-2xl font-bold tabular-nums">{costDashboard.video_counts.deliverable}</p>
                  <p className="text-[11px] text-gray-500 mt-1">{costDashboard.video_counts.done} done · {costDashboard.video_counts.pending_review} pending</p>
                </div>
                <div className="glass-elevated rounded-card p-5">
                  <p className="text-[10px] text-gray-500 uppercase tracking-wider mb-1">Costo / deliverable</p>
                  <p className="text-2xl font-bold tabular-nums">
                    {costDashboard.cost_per_deliverable !== null
                      ? `$${costDashboard.cost_per_deliverable.toFixed(2)}`
                      : "—"}
                  </p>
                  <p className="text-[11px] text-gray-500 mt-1">
                    incluye rejects + retries
                  </p>
                </div>
                <div className="glass-elevated rounded-card p-5">
                  <p className="text-[10px] text-gray-500 uppercase tracking-wider mb-1">Margen estimado</p>
                  <p className="text-2xl font-bold tabular-nums text-accent">
                    {costDashboard.margin_per_video !== null
                      ? `$${costDashboard.margin_per_video.toFixed(2)}`
                      : "—"}
                  </p>
                  <p className="text-[11px] text-gray-500 mt-1">
                    /video · total ${costDashboard.margin_total !== null
                      ? costDashboard.margin_total.toFixed(2)
                      : "—"}
                  </p>
                </div>
              </div>

              {/* Rejection rate + video counts breakdown */}
              <div className="glass-elevated rounded-card p-5">
                <div className="flex items-center justify-between mb-4">
                  <h3 className="text-sm font-semibold">Salud del pipeline</h3>
                  <span className="text-[11px] text-gray-500">% rejects + status counts</span>
                </div>
                <div className="grid grid-cols-2 lg:grid-cols-5 gap-3">
                  <div>
                    <p className="text-[10px] text-gray-500 uppercase tracking-wider mb-0.5">Done</p>
                    <p className="text-base font-bold text-accent tabular-nums">{costDashboard.video_counts.done}</p>
                  </div>
                  <div>
                    <p className="text-[10px] text-gray-500 uppercase tracking-wider mb-0.5">Pending</p>
                    <p className="text-base font-bold text-amber-400 tabular-nums">{costDashboard.video_counts.pending_review}</p>
                  </div>
                  <div>
                    <p className="text-[10px] text-gray-500 uppercase tracking-wider mb-0.5">Rejected</p>
                    <p className="text-base font-bold text-red-400 tabular-nums">{costDashboard.video_counts.rejected}</p>
                  </div>
                  <div>
                    <p className="text-[10px] text-gray-500 uppercase tracking-wider mb-0.5">Error</p>
                    <p className="text-base font-bold text-red-500 tabular-nums">{costDashboard.video_counts.error}</p>
                  </div>
                  <div>
                    <p className="text-[10px] text-gray-500 uppercase tracking-wider mb-0.5">% rejects</p>
                    <p className="text-base font-bold tabular-nums">
                      {costDashboard.rejection_rate !== null
                        ? `${(costDashboard.rejection_rate * 100).toFixed(1)}%`
                        : "—"}
                    </p>
                  </div>
                </div>
              </div>

              {/* Per-provider breakdown */}
              <div className="glass-elevated rounded-card p-5">
                <div className="flex items-center justify-between mb-4">
                  <h3 className="text-sm font-semibold">Desglose por proveedor</h3>
                  <span className="text-[11px] text-gray-500">{costDashboard.by_provider.length} buckets</span>
                </div>
                <div className="space-y-2">
                  {costDashboard.by_provider.map((p) => {
                    const pct = costDashboard.total_cost > 0
                      ? (p.cost / costDashboard.total_cost) * 100
                      : 0;
                    return (
                      <div key={p.provider} className="flex items-center gap-3">
                        <span className="w-20 text-xs font-medium capitalize">{p.provider}</span>
                        <div className="flex-1 h-2 rounded-full bg-surface-3/40 overflow-hidden">
                          <div
                            className="h-full bg-brand/60"
                            style={{ width: `${Math.min(100, pct)}%` }}
                          />
                        </div>
                        <span className="w-20 text-[11px] text-gray-400 tabular-nums text-right">
                          {p.calls} calls
                        </span>
                        <span className="w-20 text-xs font-mono tabular-nums text-right">
                          ${p.cost.toFixed(2)}
                        </span>
                        <span className="w-12 text-[11px] text-gray-500 tabular-nums text-right">
                          {pct.toFixed(0)}%
                        </span>
                      </div>
                    );
                  })}
                </div>
              </div>

              {/* Per-tenant breakdown */}
              <div className="glass-elevated rounded-card p-5">
                <div className="flex items-center justify-between mb-4">
                  <h3 className="text-sm font-semibold">Costo por tenant</h3>
                  <span className="text-[11px] text-gray-500">{costDashboard.by_tenant.length} tenants</span>
                </div>
                <div className="overflow-x-auto">
                  <table className="w-full text-[11px]">
                    <thead>
                      <tr className="text-gray-500 uppercase tracking-wide text-[10px]">
                        <th className="text-left font-medium pb-2 pr-3">Tenant</th>
                        <th className="text-right font-medium pb-2 px-3">Calls</th>
                        <th className="text-right font-medium pb-2 px-3">Gasto</th>
                        <th className="text-right font-medium pb-2 px-3">Done</th>
                        <th className="text-right font-medium pb-2 px-3">Pending</th>
                        <th className="text-right font-medium pb-2 px-3">Rejected</th>
                        <th className="text-right font-medium pb-2 px-3">Deliverable</th>
                        <th className="text-right font-medium pb-2 px-3">$/deliver</th>
                        <th className="text-right font-medium pb-2 pl-3">% rejects</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-white/[0.04]">
                      {costDashboard.by_tenant.map((t) => (
                        <tr key={t.tenant_id} className="hover:bg-white/[0.02]">
                          <td className="py-2 pr-3 font-mono text-white">{t.tenant_id || "—"}</td>
                          <td className="py-2 px-3 text-right tabular-nums text-gray-300">{t.calls}</td>
                          <td className="py-2 px-3 text-right tabular-nums font-mono text-white">${t.cost.toFixed(2)}</td>
                          <td className="py-2 px-3 text-right tabular-nums text-accent">{t.done}</td>
                          <td className="py-2 px-3 text-right tabular-nums text-amber-400">{t.pending_review}</td>
                          <td className="py-2 px-3 text-right tabular-nums text-red-400">{t.rejected}</td>
                          <td className="py-2 px-3 text-right tabular-nums text-gray-300">{t.deliverable}</td>
                          <td className="py-2 px-3 text-right tabular-nums font-mono text-gray-300">
                            {t.cost_per_deliverable !== null ? `$${t.cost_per_deliverable.toFixed(2)}` : "—"}
                          </td>
                          <td className="py-2 pl-3 text-right tabular-nums text-gray-400">
                            {t.rejection_rate !== null ? `${(t.rejection_rate * 100).toFixed(1)}%` : "—"}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>

              {/* Per-user breakdown */}
              <div className="glass-elevated rounded-card p-5">
                <div className="flex items-center justify-between mb-4">
                  <h3 className="text-sm font-semibold">Costo por usuario</h3>
                  <span className="text-[11px] text-gray-500">{costDashboard.by_user.length} usuarios</span>
                </div>
                <div className="overflow-x-auto">
                  <table className="w-full text-[11px]">
                    <thead>
                      <tr className="text-gray-500 uppercase tracking-wide text-[10px]">
                        <th className="text-left font-medium pb-2 pr-3">Usuario</th>
                        <th className="text-left font-medium pb-2 px-3">Tenant</th>
                        <th className="text-right font-medium pb-2 px-3">Calls</th>
                        <th className="text-right font-medium pb-2 px-3">Gasto</th>
                        <th className="text-right font-medium pb-2 px-3">Done</th>
                        <th className="text-right font-medium pb-2 px-3">Pending</th>
                        <th className="text-right font-medium pb-2 px-3">Rejected</th>
                        <th className="text-right font-medium pb-2 px-3">$/deliver</th>
                        <th className="text-right font-medium pb-2 pl-3">% rejects</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-white/[0.04]">
                      {costDashboard.by_user.map((u) => (
                        <tr key={`${u.user_id}|${u.tenant_id}`} className="hover:bg-white/[0.02]">
                          <td className="py-2 pr-3 text-white">
                            {u.username || <span className="text-gray-500 italic">user #{u.user_id ?? "—"}</span>}
                          </td>
                          <td className="py-2 px-3 font-mono text-gray-400">{u.tenant_id || "—"}</td>
                          <td className="py-2 px-3 text-right tabular-nums text-gray-300">{u.calls}</td>
                          <td className="py-2 px-3 text-right tabular-nums font-mono text-white">${u.cost.toFixed(2)}</td>
                          <td className="py-2 px-3 text-right tabular-nums text-accent">{u.done}</td>
                          <td className="py-2 px-3 text-right tabular-nums text-amber-400">{u.pending_review}</td>
                          <td className="py-2 px-3 text-right tabular-nums text-red-400">{u.rejected}</td>
                          <td className="py-2 px-3 text-right tabular-nums font-mono text-gray-300">
                            {u.cost_per_deliverable !== null ? `$${u.cost_per_deliverable.toFixed(2)}` : "—"}
                          </td>
                          <td className="py-2 pl-3 text-right tabular-nums text-gray-400">
                            {u.rejection_rate !== null ? `${(u.rejection_rate * 100).toFixed(1)}%` : "—"}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>

              {/* Per-tool detail (collapsed by default mental load — just a small table) */}
              <details className="glass rounded-card p-5">
                <summary className="text-xs text-gray-400 cursor-pointer select-none">
                  Detalle por modelo ({costDashboard.by_tool.length} tools)
                </summary>
                <div className="mt-4 space-y-1.5">
                  {costDashboard.by_tool.map((t) => (
                    <div key={`${t.tool_name}|${t.tool_provider}`} className="flex items-center gap-3 text-[11px]">
                      <span className="flex-1 font-mono text-gray-300 truncate">{t.tool_name}</span>
                      <span className="text-gray-500">{t.tool_provider}</span>
                      <span className="w-16 text-right tabular-nums">{t.calls}×</span>
                      <span className="w-16 text-right tabular-nums font-mono">${t.rate_per_call.toFixed(3)}</span>
                      <span className="w-20 text-right tabular-nums font-mono text-white">${t.cost.toFixed(2)}</span>
                    </div>
                  ))}
                </div>
              </details>

              <div className="rounded-card bg-surface-3/30 ring-1 ring-white/[0.04] p-4 space-y-2">
                <p className="text-[11px] text-gray-300 font-medium uppercase tracking-wide">
                  Cómo leer estos números
                </p>
                <ul className="text-[10px] text-gray-500 leading-relaxed list-disc pl-4 space-y-1">
                  <li>
                    <b>Veo Fast</b> a $0.80/call (palindrome loop 8s) · <b>Veo Standard</b> $3.20.
                  </li>
                  <li>
                    <b>Whisper</b> cobrado como API de OpenAI a ~$0.006/min de audio (estimado en $0.021/call · canción promedio ~3.5 min). Las canciones más largas pueden costar +50%.
                  </li>
                  <li>
                    <b>Margen</b> calculado contra revenue editable arriba (default $8/video = contrato Universal $2k / 250 videos). No incluye costos de infra (Railway + R2 ≈ $50/mes fijo) ni Stripe fees.
                  </li>
                  <li>
                    <b>Costo / deliverable</b> incluye rejects y retries — por eso es mayor que el marginal de un render limpio.
                  </li>
                  <li>
                    <b>Veo in-flight</b> (calls con duration NULL): se cuentan como costo aunque Google probablemente no las facture si el render no completó. Sobre-estimación máxima ~$1.60.
                  </li>
                  <li>
                    Provenance de Whisper antes del 2026-05-13 fue backfilled con rows sintéticas (un row por job que llegó a status done/pending/rejected/editing). Jobs nuevos quedan tracked automáticamente.
                  </li>
                </ul>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}
