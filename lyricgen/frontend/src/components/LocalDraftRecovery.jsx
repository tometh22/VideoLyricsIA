import { useEffect, useRef } from "react";

export default function LocalDraftRecovery({ recovery, revision, remote, onRecover, onDiscard, onBack }) {
  const root = useRef(null);
  useEffect(() => { root.current?.querySelector("button")?.focus(); }, []);
  const show = (rows) => rows.map((s, i) => `${i + 1}. ${s.start}–${s.end} s\n${s.text}`).join("\n\n");
  const download = () => {
    const url = URL.createObjectURL(new Blob([recovery.raw], { type: "application/json" }));
    const a = document.createElement("a"); a.href = url; a.download = "borrador-local.json"; a.click();
    URL.revokeObjectURL(url);
  };
  return <div className="fixed inset-0 z-[90] flex items-center justify-center bg-black/75 p-4" role="dialog" aria-modal="true" aria-labelledby="local-draft-heading" ref={root} onKeyDown={(e) => {
    if (e.key !== "Tab") return;
    const buttons = [...root.current.querySelectorAll("button")];
    if (e.shiftKey && document.activeElement === buttons[0]) { e.preventDefault(); buttons.at(-1)?.focus(); }
    if (!e.shiftKey && document.activeElement === buttons.at(-1)) { e.preventDefault(); buttons[0]?.focus(); }
  }}>
    <section className="max-h-[90vh] w-full max-w-4xl overflow-auto rounded-2xl border border-amber-300/30 bg-gray-950 p-6 text-gray-100">
      <h2 id="local-draft-heading" className="text-lg font-semibold">Revisar copia local</h2>
      <p className="mt-2">La revisión {revision} está guardada en el servidor. Abrir esta comparación no modifica su aprobación.</p>
      {recovery.kind === "different" ? <>
        <p className="mt-2 text-amber-200">Esta copia del navegador contiene diferencias pendientes de recuperar. Elegí qué conservar antes de editar.</p>
        <p className="mt-2 text-sm">Copia basada en revisión {recovery.baseRevision ?? "desconocida"}{recovery.updatedAt ? ` · ${recovery.updatedAt}` : ""}.</p>
        <div className="mt-4 grid gap-4 sm:grid-cols-2">
          <div><h3>Versión guardada</h3><pre className="mt-2 whitespace-pre-wrap text-sm">{show(remote)}</pre></div>
          <div><h3>Copia local para recuperar</h3><pre className="mt-2 whitespace-pre-wrap text-sm">{show(recovery.local)}</pre></div>
        </div>
        <details className="mt-3"><summary>Ver contenido completo de ambas copias</summary><pre className="whitespace-pre-wrap break-all text-xs">{JSON.stringify({ servidor: remote, copiaLocal: recovery.local }, null, 2)}</pre></details>
        <p className="mt-3 text-sm">Recuperar carga esta copia para editar y guardar sus cambios. Si proviene de una revisión anterior, revisá también lo que cambió en el servidor.</p>
      </> : <p className="mt-3 text-amber-200">{recovery.message} La copia se conserva; no podemos confirmar si contiene cambios adicionales. No se intentó guardar ni reemplazar la revisión del servidor.</p>}
      <div className="mt-5 flex flex-wrap gap-3">
        {recovery.kind === "different" && <><button type="button" className="rounded-lg bg-violet-700 px-4 py-2" onClick={onRecover}>Recuperar para editar</button><button type="button" className="rounded-lg border px-4 py-2" onClick={onDiscard}>Descartar copia local y usar guardada</button></>}
        {typeof recovery.raw === "string" && <button type="button" className="rounded-lg border px-4 py-2" onClick={download}>Descargar copia local</button>}
        <button type="button" className="rounded-lg border px-4 py-2" onClick={onBack}>Volver sin cambiar nada</button>
      </div>
    </section>
  </div>;
}
