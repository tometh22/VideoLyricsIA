import { CATEGORY_LABELS, clock, headline, totalsOf, totalsText } from "../lib/publishReview";
import { Button, Modal } from "./campaigns/ui";

const SHOWN_LINES = 8;
const SHOWN_WARNINGS = 6;

function SongSummary({ summary, open }) {
  const unheard = summary.unheard || [];
  const warnings = summary.warnings || [];
  const hiddenLines = Math.max(0, (summary.lines_unheard || 0) - Math.min(SHOWN_LINES, unheard.length));
  const hiddenWarnings = Math.max(0, (summary.warnings_count || 0) - Math.min(SHOWN_WARNINGS, warnings.length));
  return <details open={open} className="rounded-xl bg-black/20 p-3 ring-1 ring-white/[0.07]">
    <summary className="cursor-pointer text-sm">
      <span className="font-medium text-white">{summary.title || summary.job_id}</span>
      {summary.artist ? <span className="text-ink-secondary"> · {summary.artist}</span> : null}
      <span className="block text-xs text-amber-100">{headline(summary)}</span>
    </summary>
    <div className="mt-2 space-y-3 text-xs text-ink-secondary">
      {summary.listening_known === false && <p>
        No hay datos de escucha confiables para esta canción: se muestran sólo los avisos.
      </p>}
      {unheard.length > 0 && <div>
        <p className="font-medium text-ink-primary">Nadie las escuchó en el editor</p>
        <ul className="mt-1 space-y-0.5" aria-label={`Líneas sin escuchar de ${summary.title || summary.job_id}`}>
          {unheard.slice(0, SHOWN_LINES).map((line) => <li key={line.line_index} className="truncate">
            <span className="font-mono text-cyan-200">{clock(line.start)}</span> {line.text}
          </li>)}
        </ul>
        {hiddenLines > 0 && <p className="mt-1">y {hiddenLines} más.</p>}
      </div>}
      {warnings.length > 0 && <div>
        <p className="font-medium text-ink-primary">Avisos de la Revisión rápida sin aplicar ni descartar</p>
        <ul className="mt-1 space-y-1.5" aria-label={`Avisos sin mirar de ${summary.title || summary.job_id}`}>
          {warnings.slice(0, SHOWN_WARNINGS).map((warning) => {
            const first = warning.occurrences?.[0];
            return <li key={warning.id}>
              <span className="rounded bg-white/10 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-ink-primary">{CATEGORY_LABELS[warning.category] || warning.category}</span>
              {" "}{warning.title}{warning.count > 1 ? ` (${warning.count} líneas)` : ""}
              {first && <span className="block truncate">
                <span className="font-mono text-cyan-200">{clock(first.start)}</span> {first.before}
                {first.after && first.after !== first.before ? <> → <span className="text-white">{first.after}</span></> : null}
              </span>}
            </li>;
          })}
        </ul>
        {hiddenWarnings > 0 && <p className="mt-1">y {hiddenWarnings} más.</p>}
      </div>}
      <a className="inline-block text-brand-light underline" href={`/videos/${encodeURIComponent(summary.job_id)}/edit-lyrics`} target="_blank" rel="noreferrer">
        Abrir la letra en el editor ↗
      </a>
    </div>
  </details>;
}

/**
 * Lo que nadie escuchó y lo que nadie miró, antes de publicar. No bloquea:
 * "Publicar igual" es un clic y queda registrado (quién, cuántas líneas sin
 * escuchar, cuántos avisos) en el audit log local.
 */
export default function PublishReviewSummary({
  summaries = [], totals, busy = false, onConfirm, onCancel,
  confirmLabel = "Publicar igual", cancelLabel = "Volver a revisar", embedded = false, hideActions = false,
}) {
  const total = totals || totalsOf(summaries);
  return <section aria-label="Resumen antes de publicar" className={embedded ? "space-y-3" : "space-y-4"}>
    <div>
      <p className="text-[11px] font-semibold uppercase tracking-[.18em] text-amber-200">Antes de publicar</p>
      <p className="mt-1 text-sm text-white">{totalsText(total)}.</p>
      <p className="mt-1 text-xs text-ink-secondary">
        La mayoría de los errores que reclama el cliente están en líneas que nadie escuchó o en avisos que nadie miró.
        Podés volver y corregir, o publicar igual: queda registrado.
      </p>
    </div>
    <div className={`${embedded ? "max-h-64" : "max-h-[50vh]"} space-y-2 overflow-y-auto`}>
      {summaries.map((summary) => <SongSummary key={summary.job_id} summary={summary} open={summaries.length === 1} />)}
    </div>
    {!hideActions && <div className="flex flex-wrap justify-end gap-2">
      {onCancel && <Button variant="ghost" disabled={busy} onClick={onCancel}>{cancelLabel}</Button>}
      <Button variant="primary" disabled={busy} onClick={onConfirm}>{busy ? "Enviando…" : confirmLabel}</Button>
    </div>}
  </section>;
}

export function PublishReviewDialog({ review, busy = false, onConfirm, onClose, confirmLabel }) {
  if (!review) return null;
  return <Modal label="Resumen antes de publicar" busy={busy} onClose={onClose} width="max-w-xl">
    <PublishReviewSummary summaries={review.summaries} totals={review.totals} busy={busy}
      onConfirm={onConfirm} onCancel={onClose} confirmLabel={confirmLabel} />
  </Modal>;
}
