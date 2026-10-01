const LABELS = {
  complete: "Completa", partial: "Parcial", pending: "Pendiente",
  blocked: "Bloqueada", stale: "Desactualizada",
};
const BLOCKERS = {
  missing_independent_audio: "Faltan escuchas de una de las familias de modelos.",
  empty_transcription: "El documento no tiene texto transcripto.",
  audio_unavailable: "El audio no está disponible para revisar.",
  source_changed: "La candidata quedó desactualizada porque cambió el audio o el documento.",
  tool_failure: "Una herramienta no pudo completar el análisis.",
  budget_hold: "El análisis está detenido por el límite de presupuesto autorizado.",
  candidate_unavailable: "La candidata todavía no está disponible en el editor.",
  active_human_review: "Hay una revisión humana en curso; sus cambios se conservan.",
  existing_proposal_preserved: "Ya hay otra propuesta; se conserva y no se reemplaza.",
  review_pending: "La revisión asistida está pendiente.",
  review_blocked: "No se pudo completar la revisión asistida.",
};

/** Counts come from the complete source-bound roster, never the filtered page. */
export function CampaignReviewerSummary({ status }) {
  if (status?.enabled !== true) return null;
  return <section aria-label="Estado de revisión asistida del lote" className="rounded-2xl bg-cyan-500/5 p-5 ring-1 ring-cyan-400/20">
    <h2 className="font-semibold text-white">Revisión asistida · {status.counters?.complete ?? "—"}/{status.total ?? "—"} completas</h2>
    <p className="mt-1 text-sm text-ink-secondary">Inspección acústica y reconciliación del lote completo. No equivale a aprobación humana ni certifica ausencia de errores.</p>
    <dl className="mt-3 grid grid-cols-2 gap-3 md:grid-cols-5">{Object.entries(LABELS).map(([key, label]) =>
      <div key={key} className="rounded-xl bg-black/20 p-3"><dt className="text-xs text-ink-tertiary">{label}</dt><dd className="text-xl font-bold text-white">{status.counters?.[key] ?? "—"}</dd></div>)}</dl>
    <p className="mt-3 text-sm text-ink-secondary">Candidatas disponibles: {status.candidate_count ?? "—"} · Canciones con cambios: {status.changed_song_count ?? "—"} · Sin cambios respaldados: {status.unchanged_song_count ?? "—"}</p>
    <p className="mt-1 text-xs text-ink-tertiary">Abrí una canción para escuchar el audio, comparar la candidata y revisar sus dudas. Letra y timing solamente; no se generan fondos ni videos.</p>
  </section>;
}

export function CampaignReviewerRow({ status, jobId, onOpen }) {
  if (!status) return null;
  const knownStatus = Object.hasOwn(LABELS, status.status);
  const available = status.status === "complete" && status.candidate_available === true && Boolean(jobId);
  return <div className="mt-1 space-y-1 text-xs text-ink-secondary">
    <span>Revisión asistida: {knownStatus ? LABELS[status.status] : "Estado no confirmado"}</span>
    {status.blocker && <p className="text-amber-200">{BLOCKERS[status.blocker] || "No se pudo completar la revisión; el motivo todavía no está clasificado."}</p>}
    {available && <>
      <p>{status.changes_count ?? "—"} cambios · {status.doubts_count ?? "—"} dudas</p>
      <button type="button" onClick={() => onOpen(`/review/${encodeURIComponent(jobId)}`)} className="rounded-lg bg-cyan-400/10 px-3 py-1.5 text-cyan-200">Ver candidata · audio y comparación</button>
    </>}
    {status.status === "complete" && !available && <p>Candidata aún no disponible en el editor.</p>}
    {status.status === "stale" && <p>La canción cambió; esta candidata no se puede usar.</p>}
  </div>;
}
