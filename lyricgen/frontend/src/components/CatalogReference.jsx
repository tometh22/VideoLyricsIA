export default function CatalogReference({ reference }) {
  if (!reference) return null;
  const outcome = reference.audio_validation;
  const absent = reference.status === "absent";
  const used = outcome?.used === true;
  let sourceUrl = null;
  try {
    const url = new URL(reference.source_url);
    if (url.protocol === "https:" && url.hostname === "docs.google.com") sourceUrl = url.href;
  } catch { /* An absent source has no link. */ }
  return (
    <details className="mb-3 rounded-xl bg-surface-2/45 p-4 ring-1 ring-white/[0.06]">
      <summary className="cursor-pointer text-sm font-semibold text-white">Letra de la planilla</summary>
      <p className="mt-2 text-xs text-amber-200">
        {absent
          ? "No hay una letra asociada en la planilla. Revisá la transcripción contra el audio."
          : used
            ? "Se utilizó para la transcripción después de compararla con el audio. La letra y los tiempos requieren revisión humana."
            : "Disponible para consulta. No se aplicó a la transcripción; verificá que corresponda a esta grabación."}
      </p>
      {!absent && <>
        <p className="mt-2 text-xs text-ink-secondary">{reference.artist} — {reference.track}</p>
        {sourceUrl && <a href={sourceUrl} target="_blank" rel="noreferrer noopener" className="mt-2 inline-block text-xs text-brand-light">
          Ver planilla · {reference.sheet_name} · filas {(reference.row_numbers || []).join(", ")}
        </a>}
        {reference.text && <pre className="mt-3 max-h-64 overflow-auto whitespace-pre-wrap rounded-lg bg-black/20 p-3 text-xs leading-5 text-ink-secondary">{reference.text}</pre>}
      </>}
    </details>
  );
}
