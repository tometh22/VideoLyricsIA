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
    <div className="glass rounded-2xl p-5 text-center">
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
  const [invoices, setInvoices] = useState([]);
  const [search, setSearch] = useState("");
  const [loading, setLoading] = useState(true);
  const [compliance, setCompliance] = useState(null);
  const [backgrounds, setBackgrounds] = useState([]);
  const [bgUploading, setBgUploading] = useState(false);
  const [bgName, setBgName] = useState("");
  const [bgTags, setBgTags] = useState("");

  // Create user modal
  const [showCreate, setShowCreate] = useState(false);
  const [newUser, setNewUser] = useState({ username: "", password: "", email: "", plan_id: "100", role: "user" });
  const [createError, setCreateError] = useState("");

  useEffect(() => {
    loadStats();
  }, []);

  useEffect(() => {
    if (tab === "users") loadUsers();
    if (tab === "jobs") loadJobs();
    if (tab === "invoices") loadInvoices();
    if (tab === "compliance") loadCompliance();
    if (tab === "backgrounds") loadBackgrounds();
  }, [tab]);

  const loadStats = async () => {
    try {
      const res = await fetch(`${API}/admin/stats`, { headers: authHeaders() });
      setStats(await res.json());
    } catch {} finally { setLoading(false); }
  };

  const loadCompliance = async () => {
    try {
      const res = await fetch(`${API}/compliance/status`, { headers: authHeaders() });
      setCompliance(await res.json());
    } catch {}
  };

  const loadBackgrounds = async () => {
    try {
      const res = await fetch(`${API}/admin/backgrounds`, { headers: authHeaders() });
      setBackgrounds(await res.json());
    } catch {}
  };

  const handleUploadBg = async (file) => {
    if (!file || !bgName.trim()) return;
    setBgUploading(true);
    const formData = new FormData();
    formData.append("file", file);
    formData.append("name", bgName.trim());
    formData.append("tags", bgTags.trim());
    try {
      await fetch(`${API}/admin/backgrounds`, { method: "POST", headers: authHeaders(), body: formData });
      setBgName("");
      setBgTags("");
      loadBackgrounds();
    } catch {}
    setBgUploading(false);
  };

  const handleDeleteBg = async (id) => {
    if (!window.confirm("Delete this background?")) return;
    await fetch(`${API}/admin/backgrounds/${id}`, { method: "DELETE", headers: authHeaders() });
    loadBackgrounds();
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
      const res = await fetch(`${API}/admin/jobs?limit=100`, { headers: authHeaders() });
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
      setNewUser({ username: "", password: "", email: "", plan_id: "100", role: "user" });
      loadUsers();
    } catch (err) {
      setCreateError(err.message);
    }
  };

  const handleToggleUser = async (userId, isActive) => {
    await fetch(`${API}/admin/users/${userId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify({ is_active: !isActive }),
    });
    loadUsers();
  };

  const handleChangePlan = async (userId, planId) => {
    await fetch(`${API}/admin/users/${userId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify({ plan_id: planId }),
    });
    loadUsers();
  };

  const handleToggleAI = async (userId, isAuthorized) => {
    const endpoint = isAuthorized ? "revoke-ai" : "authorize-ai";
    await fetch(`${API}/admin/users/${userId}/${endpoint}`, {
      method: "POST",
      headers: authHeaders(),
    });
    loadUsers();
  };

  const tabs = [
    { id: "overview", label: "Overview" },
    { id: "users", label: "Users" },
    { id: "jobs", label: "Jobs" },
    { id: "invoices", label: "Invoices" },
    { id: "backgrounds", label: "Backgrounds" },
    { id: "compliance", label: "Compliance" },
  ];

  if (loading) return (
    <div className="w-full max-w-5xl animate-fade-in">
      <div className="grid grid-cols-4 gap-4 mb-8">
        {[1,2,3,4].map(i => <div key={i} className="glass rounded-2xl p-5 h-20 animate-pulse" />)}
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

      {/* Tabs */}
      <div className="flex gap-1 mb-8 glass rounded-xl p-1 w-fit">
        {tabs.map(t => (
          <button key={t.id} onClick={() => setTab(t.id)}
            className={`px-5 py-2 rounded-lg text-sm font-medium transition-all ${
              tab === t.id ? "bg-brand text-white" : "text-gray-400 hover:text-white"
            }`}>
            {t.label}
          </button>
        ))}
      </div>

      {/* Overview */}
      {tab === "overview" && stats && (
        <div className="space-y-8">
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
            <div className="glass-elevated rounded-2xl p-6">
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
            <div className="glass-elevated rounded-2xl p-6">
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

          <div className="glass rounded-2xl overflow-hidden">
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
                      <div className="flex gap-1">
                        <button onClick={() => handleToggleAI(u.id, u.ai_authorized)}
                          className={`text-[10px] px-2 py-1 rounded-lg font-medium ${u.ai_authorized ? "text-amber-400 hover:bg-amber-500/10" : "text-accent hover:bg-accent/10"}`}>
                          {u.ai_authorized ? "Revoke AI" : "Auth AI"}
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
          <p className="text-xs text-gray-500">{jobsTotal} jobs total</p>
          <div className="glass rounded-2xl overflow-hidden">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-white/[0.06]">
                  <th className="text-left px-4 py-3 text-xs text-gray-500 font-medium">Job ID</th>
                  <th className="text-left px-4 py-3 text-xs text-gray-500 font-medium">Artist</th>
                  <th className="text-left px-4 py-3 text-xs text-gray-500 font-medium">File</th>
                  <th className="text-left px-4 py-3 text-xs text-gray-500 font-medium">Tenant</th>
                  <th className="text-left px-4 py-3 text-xs text-gray-500 font-medium">Status</th>
                  <th className="text-left px-4 py-3 text-xs text-gray-500 font-medium">Created</th>
                </tr>
              </thead>
              <tbody>
                {jobs.map(j => (
                  <tr key={j.job_id} className="border-b border-white/[0.03]">
                    <td className="px-4 py-3 font-mono text-xs text-gray-400">{j.job_id}</td>
                    <td className="px-4 py-3">{j.artist}</td>
                    <td className="px-4 py-3 text-gray-400 truncate max-w-[200px]">{j.filename}</td>
                    <td className="px-4 py-3 text-xs text-gray-500">{j.tenant_id}</td>
                    <td className="px-4 py-3">
                      <span className={`text-xs px-2 py-1 rounded-lg font-medium ${
                        j.status === "done" ? "bg-accent/10 text-accent" :
                        j.status === "error" ? "bg-red-500/10 text-red-400" :
                        "bg-brand/10 text-brand"
                      }`}>{j.status}</span>
                    </td>
                    <td className="px-4 py-3 text-xs text-gray-500">
                      {j.created_at ? new Date(j.created_at * 1000).toLocaleString("es-AR") : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Invoices */}
      {tab === "invoices" && (
        <div className="space-y-4">
          <div className="glass rounded-2xl overflow-hidden">
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
          <div className="glass-elevated rounded-2xl p-6">
            <h3 className="text-sm font-semibold mb-4">Upload Background</h3>
            <div className="flex gap-3 items-end">
              <div className="flex-1">
                <label className="text-[10px] text-gray-500 uppercase tracking-wider">Name</label>
                <input
                  type="text" value={bgName} onChange={(e) => setBgName(e.target.value)}
                  placeholder="e.g. Ocean Sunset Loop"
                  className="w-full mt-1 px-3 py-2 rounded-lg bg-surface-1 border border-white/[0.06] focus:border-brand/50 focus:outline-none text-sm text-white placeholder-gray-500"
                />
              </div>
              <div className="flex-1">
                <label className="text-[10px] text-gray-500 uppercase tracking-wider">Tags (comma-separated)</label>
                <input
                  type="text" value={bgTags} onChange={(e) => setBgTags(e.target.value)}
                  placeholder="e.g. ocean,sunset,calm"
                  className="w-full mt-1 px-3 py-2 rounded-lg bg-surface-1 border border-white/[0.06] focus:border-brand/50 focus:outline-none text-sm text-white placeholder-gray-500"
                />
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
            <p className="text-[10px] text-gray-600 mt-2">MP4, MOV, JPG, or PNG. These will be available for all users to select when generating videos.</p>
          </div>

          {/* Library grid */}
          <div className="glass-elevated rounded-2xl p-6">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-sm font-semibold">Background Library</h3>
              <span className="text-xs text-gray-500">{backgrounds.length} asset{backgrounds.length !== 1 ? "s" : ""}</span>
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
                      <p className="text-xs font-medium text-white truncate">{bg.name}</p>
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
              <div className="glass-elevated rounded-2xl p-6">
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

              <div className="glass-elevated rounded-2xl p-6">
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
    </div>
  );
}
