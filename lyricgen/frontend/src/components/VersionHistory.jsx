import { useEffect, useRef, useState } from "react";

const REASON = { autosave: "Autoguardado", manual: "Edición manual", restore: "Restauración", approve: "Aprobada", conflict: "Conflicto preservado", migration: "Migración" };

export default function VersionHistory({ open, loadVersions, onRestore, onClose }) {
  const [versions, setVersions] = useState([]);
  const [loading, setLoading] = useState(false);
  const panelRef = useRef(null);
  useEffect(() => {
    if (!open) return;
    setLoading(true);
    Promise.resolve(loadVersions?.()).then(setVersions).finally(() => setLoading(false));
  }, [loadVersions, open]);
  useEffect(() => {
    if (!open) return undefined;
    const previous = document.activeElement;
    panelRef.current?.focus();
    const onKeyDown = (event) => {
      if (event.key === "Escape") { event.preventDefault(); onClose?.(); return; }
      if (event.key !== "Tab") return;
      const focusable = [...panelRef.current.querySelectorAll("button:not([disabled])")];
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => { document.removeEventListener("keydown", onKeyDown); previous?.focus?.(); };
  }, [onClose, open]);
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-[80] flex justify-end bg-black/55" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose?.(); }}>
      <aside ref={panelRef} role="dialog" aria-modal="true" aria-label="Historial de versiones" tabIndex={-1} className="h-full w-full max-w-md overflow-y-auto bg-surface-1 p-5 shadow-2xl ring-1 ring-white/10 outline-none">
        <div className="flex items-center justify-between"><h2 className="text-lg font-semibold text-white">Historial de versiones</h2><button type="button" onClick={onClose} aria-label="Cerrar historial" className="h-9 w-9 rounded-lg text-ink-secondary hover:bg-white/5 hover:text-white">×</button></div>
        {loading ? <p className="mt-6 text-sm text-ink-tertiary">Cargando…</p> : (
          <div className="mt-5 space-y-2">
            {versions.map((version) => <div key={version.id} className="rounded-xl bg-white/[0.035] p-3 ring-1 ring-white/[0.07]">
              <div className="flex items-center justify-between gap-3">
                <div>
                  <p className="text-sm font-medium text-white">Revisión {version.revision}</p>
                  <p className="mt-0.5 text-[10px] text-ink-tertiary">{REASON[version.reason] || version.reason}{version.created_by?.username ? ` · ${version.created_by.username}` : ""}</p>
                </div>
                <div className="flex items-center gap-2">
                  {version.is_approved && <span className="rounded-md bg-emerald-400/10 px-2 py-1 text-[9px] font-semibold text-emerald-300">APROBADA</span>}
                  <button type="button" onClick={() => onRestore?.(version.id)} className="rounded-lg bg-brand/15 px-2.5 py-1.5 text-[10px] font-medium text-brand-light hover:bg-brand/25">Restaurar</button>
                </div>
              </div>
            </div>)}
            {!versions.length && <p className="text-sm text-ink-tertiary">Todavía no hay checkpoints.</p>}
          </div>
        )}
      </aside>
    </div>
  );
}
