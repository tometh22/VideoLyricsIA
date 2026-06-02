// Sección "Contenido" del Admin Panel v2 — sub-tabs "fondos" y "compliance".
//
// LEGACY: este archivo es un PORT directo de las tabs "backgrounds" y
// "compliance" del monolito AdminPanel.jsx. No se rediseñó nada: la estructura
// JSX, las labels y el comportamiento son idénticos al admin viejo. PR B lo
// rediseña — por ahora solo tiene que funcionar 100% dentro del shell nuevo.
//
// Los únicos cambios respecto del original son de plomería:
//   - API / authHeaders salen de adminApi (no más helpers locales)
//   - flashError sale de useAdmin() (no más estado local del banner)
//   - cada sub-tab carga su data al montar (montar ES el trigger; no hay
//     dispatcher de cambio-de-tab como en el monolito)
import { useEffect, useState } from "react";

import { API, authHeaders } from "../../adminApi";
import { useAdmin } from "../../AdminContext";
import SectionHeader from "../../layout/SectionHeader";

export default function ContenidoSection({ subTab }) {
  const { flashError } = useAdmin();

  // --- Fondos ---------------------------------------------------------------
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

  const loadBackgrounds = async () => {
    try {
      const q = bgListFilter ? `?owner_tenant_id=${encodeURIComponent(bgListFilter)}` : "";
      const res = await fetch(`${API}/admin/backgrounds${q}`, { headers: authHeaders() });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setBackgrounds(await res.json());
    } catch (err) {
      flashError(`No pude cargar los fondos: ${err.message || err}`);
    }
    try {
      const res = await fetch(`${API}/admin/background-tenants`, { headers: authHeaders() });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setBgTenants(Array.isArray(data?.tenants) ? data.tenants : []);
    } catch (err) {
      flashError(`No pude cargar los tenants de fondos: ${err.message || err}`);
    }
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

  // Recarga al montar y cada vez que cambia el filtro de la biblioteca
  // (el filtro es server-side via el mismo endpoint).
  useEffect(() => {
    if (subTab !== "fondos") return;
    loadBackgrounds();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [subTab, bgListFilter]);

  // --- Compliance -----------------------------------------------------------
  const [compliance, setCompliance] = useState(null);

  const loadCompliance = async () => {
    try {
      const res = await fetch(`${API}/compliance/status`, { headers: authHeaders() });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setCompliance(await res.json());
    } catch (err) {
      flashError(`No pude cargar el estado de compliance: ${err.message || err}`);
    }
  };

  useEffect(() => {
    if (subTab !== "compliance") return;
    loadCompliance();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [subTab]);

  // --- Render ---------------------------------------------------------------
  if (subTab === "fondos") {
    return (
      <div>
        <SectionHeader
          title="Biblioteca de fondos"
          subtitle="Fondos IA y subidos: globales (visibles para todos) o exclusivos de un tenant."
        />
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
      </div>
    );
  }

  if (subTab === "compliance") {
    return (
      <div>
        <SectionHeader
          title="Compliance UMG"
          subtitle="Estado de los checks de cumplimiento y enlaces a la política de datos."
        />
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
      </div>
    );
  }

  return null;
}
