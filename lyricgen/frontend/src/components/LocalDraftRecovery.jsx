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
      <h2 id="local-draft-heading" className="text-lg font-semibold">Encontramos un borrador anterior</h2>
      <p className="mt-2">La versión guardada en Genly (revisión {revision}) está intacta.</p>
      {recovery.kind === "different" ? <>
        <p className="mt-2 text-amber-200">También hay cambios de una edición anterior guardados solamente en este navegador. Elegí cuál versión querés abrir.</p>
        <p className="mt-2 text-sm">Borrador basado en revisión {recovery.baseRevision ?? "desconocida"}{recovery.updatedAt ? ` · ${recovery.updatedAt}` : ""}.</p>
        <div className="mt-4 grid gap-4 sm:grid-cols-2">
          <div><h3>Versión guardada en Genly</h3><pre className="mt-2 whitespace-pre-wrap text-sm">{show(remote)}</pre></div>
          <div><h3>Borrador de este navegador</h3><pre className="mt-2 whitespace-pre-wrap text-sm">{show(recovery.local)}</pre></div>
        </div>
        <details className="mt-3"><summary>Ver contenido completo de ambas copias</summary><pre className="whitespace-pre-wrap break-all text-xs">{JSON.stringify({ servidor: remote, copiaLocal: recovery.local }, null, 2)}</pre></details>
        <p className="mt-3 text-sm">“Recuperar cambios” abre el borrador para que lo revises. “Usar versión de Genly” descarta ese borrador del navegador. Ninguna opción cambia la aprobación hasta que vuelvas a guardar.</p>
      </> : <>
        <p className="mt-3 text-amber-200">Encontramos un borrador automático de una edición anterior, pero ya no podemos abrirlo de forma segura.</p>
        <p className="mt-2 text-sm">Motivo: {recovery.message}</p>
        {typeof recovery.raw === "string"
          ? <p className="mt-2 text-sm">Podés seguir con la versión guardada en Genly. Esto elimina solamente el borrador de este navegador; no cambia la revisión ni su aprobación.</p>
          : <p className="mt-2 text-sm">Salí del editor y revisá los permisos de almacenamiento del navegador para poder resolverlo sin perder información.</p>}
      </>}
      <div className="mt-5 flex flex-wrap gap-3">
        {recovery.kind === "different" && <button type="button" className="rounded-lg bg-violet-700 px-4 py-2" onClick={onRecover}>Recuperar cambios</button>}
        {typeof recovery.raw === "string" && <button type="button" className={`${recovery.kind === "unreadable" ? "bg-violet-700" : "border"} rounded-lg px-4 py-2`} onClick={onDiscard}>Usar versión de Genly</button>}
        {typeof recovery.raw === "string" && <button type="button" className="rounded-lg border px-4 py-2" onClick={download}>Descargar borrador anterior</button>}
        <button type="button" className="rounded-lg border px-4 py-2" onClick={onBack}>Salir del editor</button>
      </div>
    </section>
  </div>;
}
