// Vista "Fondos": biblioteca de backgrounds (IA + subidos), globales o
// exclusivos de un tenant. Form de upload arriba, grilla de cards abajo con
// filtro server-side por dueño.
//
// Datos y mutaciones vienen de useContenido (vía props desde ContenidoSection).
import { API } from "../../adminApi";
import EmptyState from "../../primitives/EmptyState";
import FilterBar from "../../primitives/FilterBar";
import SectionHeader from "../../layout/SectionHeader";

// URL de preview con token en query — el <video>/<img> no puede mandar el
// header Authorization, así que el token viaja en la URL. CRÍTICO: misma URL
// para mp4 e imagen; el backend valida el token.
const previewUrl = (id) =>
  `${API}/backgrounds/${id}/preview?token=${encodeURIComponent(localStorage.getItem("genly_token") || "")}`;

export default function FondosView({
  backgrounds,
  bgName,
  setBgName,
  bgTags,
  setBgTags,
  bgOwnerTenant,
  setBgOwnerTenant,
  bgTenants,
  bgListFilter,
  setBgListFilter,
  bgUploading,
  handleUploadBg,
  handleDeleteBg,
}) {
  const uploadDisabled = bgUploading || !bgName.trim();

  return (
    <div>
      <SectionHeader
        title="Biblioteca de fondos"
        subtitle="Fondos pre-aprobados por tenant"
      />

      <div className="space-y-6">
        {/* Form de upload — específico de esta vista, se conserva tal cual */}
        <div className="glass-elevated rounded-card p-6">
          <h3 className="text-ui font-semibold text-white mb-4">Subir fondo</h3>
          <div className="flex gap-3 items-end flex-wrap">
            <div className="flex-1 min-w-[180px]">
              <label className="text-section uppercase text-gray-500">Nombre</label>
              <input
                type="text"
                value={bgName}
                onChange={(e) => setBgName(e.target.value)}
                placeholder="ej: Ocean Sunset Loop"
                className="w-full mt-1 px-3 py-2 rounded-button bg-surface-3/40 ring-1 ring-white/[0.06] focus:ring-brand/40 focus:outline-none text-ui text-white placeholder:text-gray-600"
              />
            </div>
            <div className="flex-1 min-w-[180px]">
              <label className="text-section uppercase text-gray-500">Tags (separados por coma)</label>
              <input
                type="text"
                value={bgTags}
                onChange={(e) => setBgTags(e.target.value)}
                placeholder="ej: ocean,sunset,calm"
                className="w-full mt-1 px-3 py-2 rounded-button bg-surface-3/40 ring-1 ring-white/[0.06] focus:ring-brand/40 focus:outline-none text-ui text-white placeholder:text-gray-600"
              />
            </div>
            <div className="min-w-[180px]">
              <label className="text-section uppercase text-gray-500">Asignar a tenant</label>
              <select
                value={bgOwnerTenant}
                onChange={(e) => setBgOwnerTenant(e.target.value)}
                className="w-full mt-1 px-3 py-2 rounded-button bg-surface-3/40 ring-1 ring-white/[0.06] focus:ring-brand/40 focus:outline-none text-ui text-white"
              >
                <option value="">Global (visible para todos)</option>
                {bgTenants.map((tid) => (
                  <option key={tid} value={tid}>{tid}</option>
                ))}
              </select>
            </div>
            <div>
              <label className={`btn-primary text-ui py-2 px-4 cursor-pointer ${uploadDisabled ? "opacity-50 pointer-events-none" : ""}`}>
                {bgUploading ? "Subiendo…" : "Subir"}
                <input
                  type="file"
                  accept=".mp4,.mov,.jpg,.jpeg,.png"
                  className="hidden"
                  disabled={uploadDisabled}
                  onChange={(e) => { if (e.target.files[0]) handleUploadBg(e.target.files[0]); e.target.value = ""; }}
                />
              </label>
            </div>
          </div>
          <p className="text-label text-gray-600 mt-2">
            MP4, MOV, JPG o PNG. Los assets <strong>globales</strong> son visibles para todos los tenants; asignarlo a un tenant lo bloquea sólo para ese tenant (ej: exclusividad de Universal Music).
          </p>
        </div>

        {/* Grilla de la biblioteca */}
        <div className="glass-elevated rounded-card p-6">
          <div className="flex items-center justify-between mb-4 gap-3 flex-wrap">
            <h3 className="text-ui font-semibold text-white">Biblioteca de fondos</h3>
            <div className="flex items-center gap-3">
              <FilterBar.Select
                value={bgListFilter}
                onChange={setBgListFilter}
                options={[
                  { id: "", label: "Todos los tenants" },
                  { id: "__global__", label: "Sólo globales" },
                  ...bgTenants.map((tid) => ({ id: tid, label: tid })),
                ]}
              />
              <span className="text-caption text-gray-500">
                {backgrounds.length} asset{backgrounds.length !== 1 ? "s" : ""}
              </span>
            </div>
          </div>

          {backgrounds.length === 0 ? (
            <EmptyState
              title="Sin fondos"
              message="Todavía no hay fondos subidos. Subí uno con el formulario de arriba."
            />
          ) : (
            <div className="grid grid-cols-3 gap-4">
              {backgrounds.map((bg) => (
                <div key={bg.id} className="glass rounded-card overflow-hidden group relative">
                  <div className="aspect-video bg-black/30 flex items-center justify-center">
                    {bg.file_type === "mp4" ? (
                      <video
                        src={previewUrl(bg.id)}
                        className="w-full h-full object-cover"
                        muted
                        autoPlay
                        loop
                        playsInline
                      />
                    ) : (
                      <img
                        src={previewUrl(bg.id)}
                        className="w-full h-full object-cover"
                        alt={bg.name}
                      />
                    )}
                  </div>
                  <div className="px-3 py-2">
                    <div className="flex items-center justify-between gap-2">
                      <p className="text-caption font-medium text-white truncate">{bg.name}</p>
                      <span
                        className={`shrink-0 px-1.5 py-0.5 rounded text-label uppercase tracking-wider ${
                          bg.owner_tenant_id
                            ? "bg-brand/15 text-brand-light ring-1 ring-brand/30"
                            : "bg-surface-3/40 text-gray-500"
                        }`}
                        title={bg.owner_tenant_id ? `Exclusivo del tenant: ${bg.owner_tenant_id}` : "Visible para todos los tenants"}
                      >
                        {bg.owner_tenant_id || "global"}
                      </span>
                    </div>
                    <div className="flex items-center justify-between mt-1">
                      <div className="flex gap-1 flex-wrap">
                        {bg.tags?.map((tag, i) => (
                          <span key={i} className="px-1.5 py-0.5 rounded bg-surface-3/40 text-label text-gray-500">{tag}</span>
                        ))}
                      </div>
                      <button
                        onClick={() => handleDeleteBg(bg.id)}
                        className="w-6 h-6 rounded-md hover:bg-red-500/10 flex items-center justify-center text-gray-600 hover:text-red-400 transition-colors duration-brand opacity-0 group-hover:opacity-100"
                        title="Borrar fondo"
                      >
                        <svg className="w-3 h-3" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                          <path d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
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
