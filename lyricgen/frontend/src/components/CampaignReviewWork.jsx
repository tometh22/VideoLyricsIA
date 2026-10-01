const REASONS = {
  voiced_gap: "Posible voz sin texto: escuchá si falta una frase",
  low_asr_content_confidence: "Texto dudoso: compará las palabras con el audio",
  isolated_tail_low_support: "Final de frase dudoso: verificá hasta dónde se escucha",
  missing_reference: "No hay una referencia válida para comparar la letra",
  empty_transcription: "No se detectó letra; comprobá si es instrumental",
  timing_analysis_pending: "El análisis de tiempos todavía no terminó",
};

export default function CampaignReviewWork({ row, timestamp }) {
  if (row.state === "discarded") return <div className="text-xs text-ink-secondary">
    <p className="font-medium text-white">{row.discard?.reason || "Descartada"}</p>
    {row.discard?.at && <p className="mt-1">{new Date(row.discard.at).toLocaleString()} · {row.discard.by_name || `Usuario ${row.discard.by}`}</p>}
    <p className="mt-1">Audio y borrador conservados. Podés recuperarla.</p>
  </div>;
  if (["approved", "exported"].includes(row.state)) return <span className="text-xs text-emerald-200">Revisión humana aprobada</span>;
  const windows = row.timing_evidence || [];
  const full = row.review_priority === "manual_full";
  const reasons = [...new Map((row.review_reasons || []).map(r => [r.code, r])).values()];
  return <div className="max-w-xl space-y-2 text-xs">
    <p className="font-semibold text-white">{full ? "Verificá la letra completa con el audio" : windows.length ? `Empezá por ${windows.length} fragmento${windows.length === 1 ? " marcado" : "s marcados"}` : "Escuchá la canción y comprobá letra y tiempos"}</p>
    <p className="text-ink-secondary">{reasons.length ? reasons.map(r => REASONS[r.code] || r.label || "Requiere comprobación con el audio").join(". ") : "El análisis no marcó problemas concretos; eso no garantiza que la canción esté correcta."}</p>
    {windows.length > 0 && <details><summary className="cursor-pointer text-cyan-200">Ver tiempos y qué comprobar</summary><ul className="mt-2 space-y-2">{windows.slice(0, 6).map((w, i) => <li key={w.id || i}><span className="font-mono text-cyan-200">{timestamp(w.start)}–{timestamp(w.end)}</span><span className="ml-2 text-ink-secondary">{(w.reasons || []).map(reason => REASONS[reason] || "Escuchá el fragmento y verificá el texto y sus tiempos").join(". ") || "Verificá texto y tiempos con el audio"}</span></li>)}</ul>{windows.length > 6 && <p className="mt-2 text-ink-tertiary">Hay {windows.length - 6} fragmentos más en el editor.</p>}</details>}
    <p className="text-ink-tertiary">{row.reference?.available ? "Referencia de audio disponible" : "Sin referencia válida"} · Confianza sin calibrar</p>
  </div>;
}
