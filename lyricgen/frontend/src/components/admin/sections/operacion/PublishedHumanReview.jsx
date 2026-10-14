import { useMemo, useState } from "react";
import { splitRequestItems } from "./changeRequestWorkflow";

export default function PublishedHumanReview({ item, busy, onConfirm, onCancel }) {
  const [reason, setReason] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
  const [checked, setChecked] = useState(() => new Set());
  const points = useMemo(() => {
    const parsed = splitRequestItems(item.comment);
    return parsed.length ? parsed : [{ text: item.comment || "Pedido completo" }];
  }, [item.comment]);
  const ready = checked.size === points.length && acknowledged && Boolean(reason.trim());
  const toggle = (index) => setChecked(old => {
    const next = new Set(old);
    if (next.has(index)) next.delete(index); else next.add(index);
    return next;
  });
  return <section aria-label="Revisión humana de la publicación" className="space-y-3 rounded-2xl bg-amber-400/[0.05] p-4 ring-1 ring-amber-300/20">
    <h4 className="text-caption font-semibold text-amber-100">Revisar versión {item.published_review.published_revision} publicada y resolver</h4>
    <p className="text-label text-gray-300">Este es el video del portal. Si ya atiende el pedido, tu revisión permite cerrarlo aunque el registro de letra o ProRes marque pendientes. Si falta una corrección, corregila y publicá antes de cerrar.</p>
    <video controls preload="metadata" src={item.delivery.published_video_url}
      aria-label="Versión publicada para revisión humana" className="aspect-video w-full rounded-xl bg-black" />
    <ul className="space-y-2">
      {points.map((point, index) => <li key={index}><label className="flex items-start gap-2 text-caption text-gray-100">
        <input type="checkbox" className="mt-0.5" checked={checked.has(index)} disabled={busy}
          onChange={() => toggle(index)} aria-label={`Verificado en la publicación: ${point.text}`} />
        {point.text}
      </label></li>)}
    </ul>
    <textarea aria-label="Motivo del cierre tras revisión humana" placeholder="Qué verificaste en el video publicado (lo ve el cliente)"
      value={reason} onChange={event => setReason(event.target.value)} maxLength={2000} rows={3} disabled={busy}
      className="w-full rounded-xl bg-surface-3/40 px-3 py-2 text-caption text-white ring-1 ring-white/[0.06]" />
    <label className="flex items-start gap-2 text-caption text-gray-200">
      <input type="checkbox" checked={acknowledged} disabled={busy} onChange={event => setAcknowledged(event.target.checked)} />
      Revisé esta versión publicada y confirmo que el pedido está atendido.
    </label>
    <p className="text-label text-gray-400">Se cierra sólo este pedido. La aprobación del cliente sigue siendo independiente.</p>
    <div className="flex flex-wrap gap-2">
      <button type="button" disabled={busy || !ready}
        onClick={() => onConfirm(checked.size, { publicationToken: item.published_review.review_token, reason: reason.trim() })}
        className="rounded-xl bg-brand px-4 py-2.5 text-caption font-semibold text-white disabled:opacity-40">
        {busy ? "Cerrando…" : "Confirmar pedido resuelto"}
      </button>
      <button type="button" disabled={busy} onClick={onCancel} className="px-3 text-caption text-gray-300">Cancelar revisión</button>
    </div>
  </section>;
}
