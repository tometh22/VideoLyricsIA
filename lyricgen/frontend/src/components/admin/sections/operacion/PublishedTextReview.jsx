import { useState } from "react";

export default function PublishedTextReview({ review, checklistReady, busy, onConfirm }) {
  const [reason, setReason] = useState("");
  const [reviewed, setReviewed] = useState(false);
  return (
    <section aria-label="Revisión manual de las diferencias" className="space-y-3 rounded-2xl bg-amber-400/[0.05] p-4 ring-1 ring-amber-300/20">
      <h4 className="text-caption font-semibold text-amber-100">Hay diferencias con el texto del pedido</h4>
      <p className="text-label text-gray-300">
        Estas frases no coinciden literalmente. Si el pedido tiene un error de escritura, revisá el video y explicá la diferencia para cerrar con la versión ya publicada.
      </p>
      <ul className="space-y-1 text-caption text-amber-100">
        {review.missing.map((text, index) => <li key={index}>«{text}»</li>)}
      </ul>
      <p className="text-label text-gray-400">Si falta una corrección en el video, hacela en el editor y volvé a publicar.</p>
      <textarea
        aria-label="Motivo de la revisión manual"
        placeholder="Ej.: el pedido dice ‘despirerta’; verifiqué ‘despierta’ en el video publicado."
        value={reason}
        onChange={(event) => setReason(event.target.value)}
        maxLength={2000}
        rows={3}
        className="w-full rounded-xl bg-surface-3/40 px-3 py-2 text-caption text-white ring-1 ring-white/[0.06] focus:outline-none focus:ring-brand/40"
      />
      <label className="flex items-start gap-2 text-caption text-gray-200">
        <input type="checkbox" checked={reviewed} onChange={(event) => setReviewed(event.target.checked)} className="mt-0.5" />
        Revisé estas diferencias en el video publicado y confirmo que el pedido está atendido.
      </label>
      {!checklistReady && <p className="text-label text-amber-200">Revisá y marcá todos los puntos del pedido antes de cerrar.</p>}
      <button type="button" disabled={busy || !checklistReady || !reviewed || !reason.trim()}
        onClick={() => onConfirm(reason.trim())}
        className="rounded-xl bg-brand px-4 py-2.5 text-caption font-semibold text-white disabled:opacity-40">
        {busy ? "Cerrando…" : "Resolver con revisión manual"}
      </button>
    </section>
  );
}
