import { useEffect, useMemo, useState } from "react";
import { API, fetchJson } from "../../adminApi";
import { linesNear, splitRequestItems } from "./changeRequestWorkflow";

const clock = (seconds) => `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, "0")}`;

/**
 * Cada punto del pedido contra lo que dice el video publicado. "Dar por
 * resuelto" sólo se habilita cuando el operador marcó todos: que exista un
 * corte corregido no prueba que estén todas las correcciones.
 */
export default function PublishedRequestChecklist({ requestId, comment, onReadyChange, onSeek }) {
  const items = useMemo(() => {
    const parsed = splitRequestItems(comment);
    return parsed.length ? parsed : [{ text: "Revisé el pedido completo en el video", times: [] }];
  }, [comment]);
  const [checked, setChecked] = useState(() => new Set());
  const [segments, setSegments] = useState(null);
  const [loadError, setLoadError] = useState(false);

  useEffect(() => {
    let alive = true;
    fetchJson(`${API}/admin/change-requests/${requestId}/review`)
      .then((review) => { if (alive) setSegments(Array.isArray(review?.segments) ? review.segments : []); })
      .catch(() => { if (alive) setLoadError(true); });
    return () => { alive = false; };
  }, [requestId]);

  const ready = checked.size === items.length;
  useEffect(() => { onReadyChange?.(ready, checked.size); }, [ready, checked.size, onReadyChange]);

  const toggle = (index) => setChecked((old) => {
    const next = new Set(old);
    if (next.has(index)) next.delete(index); else next.add(index);
    return next;
  });

  return (
    <section aria-label="Revisar el pedido punto por punto"
      className="rounded-2xl bg-amber-400/[0.05] p-4 ring-1 ring-amber-300/20">
      <div className="flex items-center justify-between gap-3">
        <p className="text-label font-semibold uppercase tracking-[0.16em] text-amber-100">Revisá cada punto</p>
        <span className="text-label text-gray-400">{checked.size} de {items.length}</span>
      </div>
      <p className="mt-1 text-label text-gray-400">
        Marcá sólo lo que ya está en el video publicado. Si falta algo, corregilo en el editor y volvé a publicar.
      </p>
      {loadError && <p className="mt-2 text-label text-amber-200">No pude cargar la letra del video: revisalo en el reproductor.</p>}
      <ol className="mt-3 space-y-3">
        {items.map((item, index) => {
          const lines = linesNear(segments, item.times);
          return (
            <li key={index} className="rounded-xl bg-black/20 p-3">
              <label className="flex cursor-pointer items-start gap-2 text-caption text-gray-100">
                <input type="checkbox" className="mt-0.5" checked={checked.has(index)} onChange={() => toggle(index)}
                  aria-label={`Está en el video: ${item.text}`} />
                <span>{item.text}</span>
              </label>
              {item.times.length > 0 && segments && (
                lines.length ? (
                  <ul className="mt-2 space-y-1 border-l border-white/10 pl-3" aria-label={`En el video cerca de ${clock(item.times[0])}`}>
                    {lines.map((line, lineIndex) => (
                      <li key={lineIndex} className="text-label text-gray-300">
                        <button type="button" onClick={() => onSeek?.(line.start)}
                          className="mr-2 font-mono text-brand-light hover:text-white">{clock(Number(line.start) || 0)}</button>
                        {line.text}
                      </li>
                    ))}
                  </ul>
                ) : <p className="mt-2 border-l border-white/10 pl-3 text-label text-amber-200">El video no tiene letra en {clock(item.times[0])}.</p>
              )}
            </li>
          );
        })}
      </ol>
    </section>
  );
}
