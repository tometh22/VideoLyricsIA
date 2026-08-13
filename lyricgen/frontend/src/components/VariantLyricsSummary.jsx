/**
 * Paso final del wizard de "Crear variante" (App.jsx VariantWizardRoute).
 *
 * Reemplaza al LyricsEditor, que en este flujo NO puede montarse:
 *
 *  1. El POST /jobs/{id}/variant no lleva `segments` — el backend reusa
 *     `segments_json` del padre tal cual. Un editor de letra acá sería
 *     editable-e-ignorado (lo que el PR #977 prohíbe explícitamente).
 *  2. Peor todavía: el autosave del editor (POST /save-segments, keyeado
 *     por transcribeJobId) le escribiría los cambios AL JOB PADRE — un
 *     video ya aprobado y posiblemente entregado al cliente.
 *
 * Así que la letra se muestra en modo lectura, la copia lo dice, y el CTA
 * dispara la creación de la variante.
 */
export default function VariantLyricsSummary({
  segments = [],
  artist = "",
  songTitle = "",
  submitting = false,
  onCreate,
  onBack,
  t,
}) {
  const tr = (key, fallback) => (t ? (t(key) || fallback) : fallback);
  const lines = Array.isArray(segments) ? segments : [];

  const fmt = (s) => {
    const n = Number(s);
    if (!Number.isFinite(n) || n < 0) return "0:00";
    const m = Math.floor(n / 60);
    const sec = Math.floor(n % 60);
    return `${m}:${String(sec).padStart(2, "0")}`;
  };

  return (
    <div className="w-full max-w-2xl mx-auto animate-fade-in">
      <div className="rounded-card bg-surface-2/40 ring-1 ring-white/[0.04] p-5">
        <div className="flex items-start gap-3 mb-4">
          <span className="w-8 h-8 rounded-xl bg-accent/10 ring-1 ring-accent/25 flex items-center justify-center shrink-0">
            <svg className="w-4 h-4 text-accent" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24" aria-hidden="true">
              <path d="M12 11v4M8 11V7a4 4 0 118 0v4M5 11h14v9a1 1 0 01-1 1H6a1 1 0 01-1-1v-9z" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </span>
          <div className="min-w-0">
            <h3 className="text-[15px] font-semibold text-white">
              {tr("variant.lyrics_locked_title", "Las lyrics aprobadas se mantienen idénticas")}
            </h3>
            <p className="text-[12px] text-ink-secondary mt-1 leading-relaxed">
              {tr(
                "variant.lyrics_locked_desc",
                "Una variante reusa la letra y los tiempos ya aprobados del video original — no se re-transcribe nada. Si querés corregir la letra, editá el video original en vez de crear una variante.",
              )}
            </p>
            {(artist || songTitle) && (
              <p className="text-[11px] text-gray-500 mt-2 truncate">
                {artist}{songTitle ? ` — ${songTitle}` : ""}
                {lines.length > 0 && ` · ${lines.length} ${tr("variant.lyrics_lines", "líneas")}`}
              </p>
            )}
          </div>
        </div>

        <div
          className="max-h-[46vh] overflow-y-auto rounded-xl bg-surface-1/60 ring-1 ring-white/[0.05] divide-y divide-white/[0.04]"
          aria-readonly="true"
        >
          {lines.length === 0 ? (
            <p className="text-[12px] text-gray-500 px-4 py-6 text-center">
              {tr("variant.lyrics_empty", "Este video no tiene líneas guardadas.")}
            </p>
          ) : (
            lines.map((s, i) => (
              <div key={i} className="flex items-baseline gap-3 px-4 py-2">
                <span className="text-[10px] font-mono tabular-nums text-gray-600 shrink-0 w-10">
                  {fmt(s?.start)}
                </span>
                <span className="text-[13px] text-gray-200 break-words">{s?.text || ""}</span>
              </div>
            ))
          )}
        </div>
      </div>

      <div className="flex items-center justify-end gap-2 mt-4">
        <button
          type="button"
          onClick={onBack}
          disabled={submitting}
          className="px-4 py-2 rounded-md text-sm text-ink-secondary hover:text-white hover:bg-surface-3/40 disabled:opacity-50 transition-colors"
        >
          {tr("variant.cancel", "Cancelar")}
        </button>
        <button
          type="button"
          onClick={onCreate}
          disabled={submitting}
          className="btn-primary text-sm h-10 px-5 disabled:opacity-60"
        >
          {submitting
            ? tr("variant.creating", "Creando…")
            : tr("variant.create", "Crear variante")}
        </button>
      </div>
    </div>
  );
}
