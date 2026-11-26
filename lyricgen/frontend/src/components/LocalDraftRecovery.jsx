import { useEffect, useRef, useState } from "react";
import { segmentsEquivalent } from "../editorMerge";

const time = value => Number.isFinite(value)
  ? `${Math.floor(value / 60)}:${(value % 60).toFixed(1).padStart(4, "0")}` : "—";
const date = value => {
  const parsed = new Date(value);
  return value && Number.isFinite(parsed.getTime())
    ? parsed.toLocaleString("es-AR", { dateStyle: "short", timeStyle: "short" }) : null;
};

export default function LocalDraftRecovery({ recovery, revision, remote = [], onRecover, onDiscard, onBack }) {
  const root = useRef(null);
  const primary = useRef(null);
  const [showAll, setShowAll] = useState(false);
  const [confirmEmpty, setConfirmEmpty] = useState(false);
  const local = Array.isArray(recovery.local) ? recovery.local : [];
  const differences = Array.from({ length: Math.max(remote.length, local.length) }, (_, index) => index)
    .filter(index => !remote[index] || !local[index] || !segmentsEquivalent([remote[index]], [local[index]]));
  const displayed = showAll ? differences : differences.slice(0, 3);
  useEffect(() => {
    const opener = document.activeElement;
    root.current?.showModal?.();
    primary.current?.focus();
    return () => opener?.focus?.();
  }, []);
  const download = () => {
    const url = URL.createObjectURL(new Blob([recovery.raw], { type: "application/json" }));
    const a = document.createElement("a"); a.href = url; a.download = "borrador-local.json"; a.click();
    setTimeout(() => URL.revokeObjectURL(url), 0);
  };
  const row = (segment) => segment ? <>
    <p className="mb-1 font-mono text-xs text-gray-400">{time(segment.start)} – {time(segment.end)}</p>
    <p className="break-words whitespace-pre-wrap text-sm leading-relaxed">{segment.text || "Línea sin texto"}</p>
  </> : <p className="text-sm text-gray-400">Sin línea en esta copia</p>;
  const different = recovery.kind === "different";
  return <dialog ref={root} open={typeof HTMLDialogElement !== "undefined" && !HTMLDialogElement.prototype.showModal ? true : undefined}
    onCancel={event => { event.preventDefault(); onBack(); }} aria-modal="true" aria-labelledby="local-draft-heading" aria-describedby="local-draft-explanation"
    className="m-auto w-[min(760px,calc(100vw-24px))] max-h-[calc(100dvh-24px)] overflow-hidden rounded-2xl border border-white/15 bg-surface-1 p-0 text-gray-100 shadow-2xl backdrop:bg-black/75">
    <section className="flex max-h-[calc(100dvh-26px)] min-h-0 flex-col">
      <header className="shrink-0 border-b border-white/10 px-5 py-4">
        <h2 id="local-draft-heading" className="text-lg font-semibold">Encontramos un borrador anterior</h2>
        <p id="local-draft-explanation" className="mt-2 text-sm leading-relaxed text-gray-300">
          {different
            ? "Hay una copia de una edición anterior en este navegador que no coincide con la guardada en Genly. La versión de Genly está intacta; no reemplazamos nada."
            : "La copia de este navegador no se puede recuperar de forma segura. La versión guardada en Genly está intacta."}
        </p>
      </header>
      <div className="min-h-0 overflow-y-auto px-5 py-4" data-testid="draft-comparison-scroll">
        {different ? <>
          <div className="mb-4 grid grid-cols-2 gap-3 text-sm">
            <div className="rounded-xl bg-white/5 p-3"><h3 className="font-medium">Guardado en Genly</h3><p className="mt-1 text-gray-400">{remote.length} líneas</p></div>
            <div className="rounded-xl bg-violet-400/10 p-3"><h3 className="font-medium">Copia de este navegador</h3><p className="mt-1 text-gray-400">{local.length} líneas{date(recovery.updatedAt) ? ` · ${date(recovery.updatedAt)}` : ""}</p></div>
          </div>
          {local.length === 0 && <div className="mb-4 rounded-xl border border-amber-300/30 bg-amber-300/5 p-3 text-sm">
            <p className="font-medium text-amber-200">La copia local está vacía. No contiene letra para recuperar.</p>
            <p className="mt-1 text-gray-300">Puede corresponder a una eliminación de líneas que no llegó a guardarse. Para seguir con tu canción, usá la versión de Genly.</p>
            <label className="mt-3 flex items-start gap-2"><input type="checkbox" checked={confirmEmpty} onChange={event => setConfirmEmpty(event.target.checked)} className="mt-1" />Quiero usar la copia vacía y eliminar las {remote.length} líneas guardadas.</label>
          </div>}
          {local.length > 0 && <>
          <p className="mb-3 text-sm font-medium">{differences.length} posiciones de línea con diferencias</p>
          <div className="space-y-3">
            {displayed.map(index => <div key={index} className="rounded-xl border border-white/10 p-3">
              <h4 className="mb-2 text-xs font-medium text-gray-400">Línea {index + 1}</h4>
              <div className="grid gap-3 sm:grid-cols-2">
                <div><p className="mb-1 text-xs text-gray-400 sm:hidden">Genly</p>{row(remote[index])}</div>
                <div><p className="mb-1 text-xs text-violet-300 sm:hidden">Este navegador</p>{row(local[index])}</div>
              </div>
            </div>)}
          </div>
          {differences.length > 3 && <button type="button" className="mt-3 min-h-10 rounded-lg px-3 text-sm text-violet-300 ring-1 ring-white/15" aria-expanded={showAll} onClick={() => setShowAll(value => !value)}>{showAll ? "Mostrar menos" : `Ver las ${differences.length} diferencias`}</button>}
          </>}
          <p className="mt-4 text-sm leading-relaxed text-gray-300">Usar Genly conserva la letra guardada y archiva la copia local en este navegador. {local.length > 0 ? "Usar el borrador recupera sus líneas y las guarda como una nueva edición; no aprueba el video." : "La copia vacía sólo se puede usar si confirmás que querés eliminar las líneas."}</p>
        </> : <>
          <p className="text-sm text-amber-200">{recovery.message}</p>
          <p className="mt-3 text-sm text-gray-300">{typeof recovery.raw === "string" ? "Podés descargar una copia antes de continuar con Genly." : "Salí del editor y revisá los permisos de almacenamiento del navegador. No borramos tu copia."}</p>
        </>}
        <details className="mt-4 text-xs text-gray-400"><summary className="cursor-pointer py-2">Información para soporte</summary><p>Versión de Genly: revisión {revision} · Base del borrador: revisión {recovery.baseRevision ?? "desconocida"}</p>
          {typeof recovery.raw === "string" && <button type="button" className="mt-2 min-h-10 rounded-lg px-3 ring-1 ring-white/15" onClick={download}>Descargar borrador anterior</button>}
        </details>
      </div>
      <footer className="shrink-0 border-t border-white/10 bg-surface-1 px-5 py-3">
        <div className="flex flex-wrap gap-2">
          {typeof recovery.raw === "string" && <button ref={primary} type="button" className="min-h-11 rounded-xl bg-violet-600 px-4 py-2 text-sm font-semibold text-white" onClick={onDiscard}>Usar versión de Genly</button>}
          {different && <button type="button" disabled={local.length === 0 && !confirmEmpty} className="min-h-11 rounded-xl px-4 py-2 text-sm ring-1 ring-white/20 disabled:opacity-40" onClick={onRecover}>Usar borrador local</button>}
          <button ref={typeof recovery.raw !== "string" ? primary : undefined} type="button" className="min-h-11 rounded-xl px-3 py-2 text-sm text-gray-300" onClick={onBack}>Salir del editor</button>
        </div>
      </footer>
    </section>
  </dialog>;
}
