import { useEffect, useRef, useState } from "react";

export default function ConflictDialog({ conflict, currentUserId, onUseServer, onSaveLocal, onCancel }) {
  const [busy, setBusy] = useState(false);
  const dialogRef = useRef(null);

  useEffect(() => {
    if (!conflict) return undefined;
    const previous = document.activeElement;
    dialogRef.current?.focus();
    const onKey = (event) => {
      if (event.key === "Escape") onCancel?.();
      if (event.key !== "Tab") return;
      const nodes = [...dialogRef.current.querySelectorAll("button:not([disabled])")];
      if (!nodes.length) return;
      const first = nodes[0];
      const last = nodes[nodes.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("keydown", onKey); previous?.focus?.(); };
  }, [conflict, onCancel]);

  if (!conflict) return null;
  const actor = conflict.updatedBy?.username;
  // Audit 2026-08-13: updated_by is "whoever last wrote this document", not
  // "someone other than you" — a reconciliation between the job row and the
  // durable editor document (e.g. after a background/typography edit that
  // carried a lyrics snapshot) can bump the revision without a genuine
  // second human involved, and the message below used to blame that on
  // "actor" even when actor IS the current user. Confirmed reproduced by
  // two different accounts (UMG Chile operator + platform admin) seeing
  // their own username presented as "someone else". Say something true
  // instead of guessing who else it was.
  const isSelf = currentUserId != null && conflict.updatedBy?.id != null
    && String(conflict.updatedBy.id) === String(currentUserId);
  const changeDescription = isSelf
    ? "Guardaste cambios desde otra pestaña, dispositivo, o al hacer otro cambio (fondo, tipografía) sobre este video. "
    : actor
      ? `${actor} guardó cambios mientras estabas editando. `
      : "Otro integrante guardó cambios. ";
  const run = async (action) => {
    setBusy(true);
    try { await action?.(); } finally { setBusy(false); }
  };
  return (
    <div className="my-4 w-full" role="presentation">
      <div ref={dialogRef} role="dialog" aria-labelledby="editor-conflict-title" tabIndex={-1} className="w-full rounded-2xl bg-amber-500/[0.08] p-5 ring-1 ring-amber-400/30 outline-none">
        <p className="text-[10px] font-semibold uppercase tracking-[0.18em] text-amber-300">Conflicto detectado</p>
        <h2 id="editor-conflict-title" className="mt-2 text-xl font-semibold text-white">Hay una versión más nueva</h2>
        <p className="mt-2 text-sm leading-relaxed text-amber-100/75">
          {changeDescription}
          La revisión del equipo es la {conflict.serverRevision}. Nada se sobrescribirá automáticamente.
        </p>
        <div className="mt-4 flex flex-wrap gap-2">
          <button type="button" disabled={busy} onClick={() => run(onUseServer)} className="rounded-lg bg-white/[0.08] px-3 py-2 text-xs font-medium text-white ring-1 ring-white/10 hover:bg-white/[0.14] disabled:opacity-50">Usar versión del equipo</button>
          <button type="button" disabled={busy} onClick={() => run(onSaveLocal)} className="rounded-lg bg-brand px-3 py-2 text-xs font-semibold text-white hover:bg-brand-light disabled:opacity-50">Guardar mi versión como nueva revisión</button>
        </div>
        <button type="button" disabled={busy} onClick={onCancel} className="mt-3 w-full rounded-lg px-3 py-2 text-xs text-ink-tertiary hover:text-white disabled:opacity-50">Cancelar (seguís viendo tu versión, sin guardar hasta que elijas una opción)</button>
      </div>
    </div>
  );
}
