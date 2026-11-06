import { useState } from "react";
import { createPortal } from "react-dom";
import useDialogA11y from "../../../../hooks/useDialogA11y";

export default function ChangeRequestRenderReview({ review, busy, onConfirm, onClose }) {
  const [confirmed, setConfirmed] = useState(false);
  const dialogRef = useDialogA11y({ onClose, closeOnEscape: !busy });
  return createPortal(<div className="fixed inset-0 z-[200] flex items-center justify-center bg-black/80 p-4">
    <section ref={dialogRef} tabIndex={-1} role="dialog" aria-modal="true" aria-labelledby="render-review-title"
      className="flex max-h-[90vh] w-full max-w-4xl flex-col rounded-2xl bg-surface-2 p-6 text-white shadow-xl">
      <h2 id="render-review-title" className="text-xl font-semibold">Revisar letra y confirmar render</h2>
      <p className="mt-2 whitespace-pre-wrap text-sm text-gray-300">Pedido del cliente: {review.comment}</p>
      <p className="mt-2 text-sm text-gray-400">Esta es la letra guardada que se usará para el video. El render no publica en el portal.</p>
      <ol className="my-4 overflow-y-auto rounded-xl bg-black/20 p-4" aria-label="Letra guardada para renderizar">
        {(review.segments || []).map((segment, index) => <li key={index} className="border-b border-white/5 py-2">
          <span className="mr-3 text-xs text-gray-400">{Math.floor(segment.start / 60)}:{String(Math.floor(segment.start % 60)).padStart(2, "0")}</span>
          {segment.text}
        </li>)}
      </ol>
      <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={confirmed}
        onChange={event => setConfirmed(event.target.checked)} disabled={busy} />Revisé la letra y quiero generar el corte con estos cambios.</label>
      <div className="mt-4 flex justify-end gap-3">
        <button type="button" onClick={onClose} disabled={busy}>Volver</button>
        <button type="button" onClick={onConfirm} disabled={!confirmed || busy || !review.segments?.length}
          className="rounded-xl bg-brand px-4 py-3 font-semibold disabled:opacity-40">{busy ? "Enviando…" : "Aprobar y re-renderizar"}</button>
      </div>
    </section>
  </div>, document.body);
}
