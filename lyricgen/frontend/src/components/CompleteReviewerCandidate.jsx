function stamp(value) {
  return Number.isFinite(Number(value)) ? `${Number(value).toFixed(2)} s` : "—";
}

const sameRows = (a, b) => Array.isArray(a) && Array.isArray(b) && a.length === b.length
  && a.every((row, i) => row.text === b[i].text && Number(row.start) === Number(b[i].start)
    && Number(row.end) === Number(b[i].end));

function doubtLabel(row) {
  const labels = {
    context_truncation_possible: "El fragmento puede no contener la frase completa",
    lexical_or_occurrence_hypothesis_requires_audio_evidence: "Comprobar palabra o repetición de la frase",
    phrase_association_unresolved: "No se pudo localizar esta frase de forma inequívoca",
    tokenization_requires_editorial_review: "La separación de palabras necesita decisión editorial",
    proposed_occurrence_not_unique: "La propuesta podría corresponder a otra repetición",
    human_protection: "Se conserva tu edición humana",
  };
  return labels[row.reason] || labels[row.content_decision?.replace(/^held:/, "")] || labels[row.discrepancy_class]
    || (row.reason && !row.reason.includes("_") ? row.reason : "Comprobar esta frase contra el audio");
}

/** Read-only full-song companion; adoption remains the existing operator flow. */
export default function CompleteReviewerCandidate({ candidate, currentRevision, currentSegments, onSeek }) {
  if (!candidate || candidate.source?.segments_revision !== currentRevision
      || !sameRows(candidate.baseline, currentSegments)) return null;
  const rows = candidate.segments || [];
  const details = candidate.review_details || {};
  const doubts = [...(details.localized_doubts || []), ...(details.line_diagnostics || []).filter(row =>
    row.discrepancy_class !== "normalized_text_match_not_certification")];
  const seek = row => {
    const start = Number(row.start ?? row.global_start ?? rows[row.line_index]?.start);
    if (Number.isFinite(start)) onSeek?.(Math.max(0, start - 1));
  };
  return <section aria-label="Revisión acústica completa" className="mb-4 rounded-xl border border-cyan-400/20 p-4">
    <h3 className="font-semibold text-cyan-200">Candidata de revisión completa</h3>
    <p className="mt-1 text-sm text-gray-300">{candidate.changes?.length
      ? `${candidate.changes.length} cambios respaldados propuestos. ${candidate.adoption_status === "matching_existing_proposal"
        ? "La propuesta asociada corresponde a esta candidata; podés usar su acción de incorporación."
        : candidate.adoption_status === "existing_different_proposal_preserved"
          ? "Las propuestas actuales son distintas y se conservan. Esta candidata aún no está disponible para incorporar."
          : "Candidata disponible para escuchar y comparar; su incorporación aún no está habilitada."}`
      : "Sin cambios respaldados; no certifica exactitud."} Esta vista no modifica ni aprueba la canción.</p>
    <p className="text-xs text-gray-400">Escuchá con contexto: la reproducción continúa después de la frase.</p>
    <p className="text-xs text-gray-400">Las líneas conservadas y los finales sin reparación no están certificados.</p>
    <details className="mt-3"><summary>Ver letra y timing de toda la candidata ({rows.length} líneas)</summary>
      <ol className="mt-2 space-y-2">{rows.map((row, i) => <li key={i} className="rounded bg-white/5 p-2">
        <button type="button" onClick={() => seek(row)} aria-label={`Escuchar línea ${i + 1}`}
          className="mr-2 text-cyan-300">▶ {i + 1} · {stamp(row.start)}–{stamp(row.end)}</button>
        <span>{row.text}</span>
        {(candidate.baseline[i]?.text !== row.text || candidate.baseline[i]?.start !== row.start
          || candidate.baseline[i]?.end !== row.end) && <p className="text-xs text-gray-400">
          Actual: {stamp(candidate.baseline[i]?.start)}–{stamp(candidate.baseline[i]?.end)} · {candidate.baseline[i]?.text}
        </p>}
      </li>)}</ol>
    </details>
    <details className="mt-3"><summary>Dudas y límites de la revisión ({doubts.length})</summary>
      <ul>{doubts.map((row, i) => <li key={i} className="mt-2 text-sm">
        <button type="button" className="mr-2 text-cyan-300" onClick={() => seek(row)}>Escuchar duda {i + 1}</button>
        {row.line_index != null ? `Línea ${row.line_index + 1}: ` : ""}
        {doubtLabel(row)}
      </li>)}</ul>
      {(details.uncovered_singing_hypotheses || []).map((row, i) => <p key={`outside-${i}`} className="mt-2 text-sm">
        <button type="button" className="mr-2 text-cyan-300" onClick={() => seek(row)}>Escuchar posible voz sin cartel {i + 1}</button>
        {row.text} · Hipótesis, no omisión confirmada.
      </p>)}
      {!!details.invalid_annotations?.length && <p>Anotaciones no utilizables: {details.invalid_annotations.reduce((n, row) => n + row.count, 0)}.</p>}
    </details>
  </section>;
}
