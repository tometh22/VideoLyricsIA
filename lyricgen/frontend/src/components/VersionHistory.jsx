import { useState } from "react";

const reasonLabel = {
  autosave: "Guardado automático",
  manual: "Guardado manual",
  restore: "Restauración",
  approve: "Aprobación",
};

export default function VersionHistory({ versions = [], fetchVersions, onRestore }) {
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);

  const toggle = async () => {
    const next = !open;
    setOpen(next);
    if (next && fetchVersions) {
      setLoading(true);
      await fetchVersions();
      setLoading(false);
    }
  };

  return (
    <div className="relative">
      <button type="button" onClick={toggle} className="h-8 px-3 rounded-lg text-[11px] text-gray-300 ring-1 ring-white/[0.10] hover:text-white hover:bg-white/[0.05]">
        {open ? "Cerrar historial" : "Historial"}
      </button>
      {open && (
        <div className="absolute right-0 top-10 z-30 w-[min(360px,calc(100vw-2rem))] rounded-xl bg-surface-1 ring-1 ring-white/[0.12] shadow-2xl p-2">
          <div className="px-2 py-1.5 text-[10px] uppercase tracking-wider text-gray-500">Versiones guardadas</div>
          {loading && <p className="px-2 py-4 text-xs text-gray-500">Cargando…</p>}
          {!loading && !versions.length && <p className="px-2 py-4 text-xs text-gray-500">Todavía no hay versiones.</p>}
          {!loading && versions.map((version) => (
            <div key={version.id} className="flex items-center gap-2 rounded-lg px-2 py-2 hover:bg-white/[0.05]">
              <div className="min-w-0 flex-1">
                <p className="text-xs text-white">v{version.revision} · {reasonLabel[version.reason] || version.reason}</p>
                <p className="text-[10px] text-gray-500 truncate">{version.created_by?.username || "Equipo"} · {version.created_at ? new Date(version.created_at).toLocaleString() : ""}</p>
              </div>
              <button type="button" onClick={() => onRestore?.(version)} disabled={version.is_approved} className="shrink-0 text-[10px] px-2 py-1 rounded-md text-brand-light hover:bg-brand/15 disabled:opacity-30 disabled:cursor-not-allowed">
                Restaurar
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
