import { useI18n } from "../i18n";

// Panel "¿Necesitás ajustes?" del detalle de un job.
//
// Unificación 2026-07-24: una sola vía de edición. Antes el panel ofrecía
// DOS tarjetas —"Editar y re-renderizar" (→ wizard) y "Regenerar fondo"
// (formulario Veo/Imagen inline)— lo que confundía y dejaba el fondo como un
// flujo aparte. El operador reportaba que "no podía cambiar el fondo" desde el
// wizard porque el fondo se editaba en OTRO lado. Ahora el panel es un único
// botón que abre el Studio Console (/videos/:id/edit-lyrics), donde se edita
// TODO —título, artista, letra, tipografía, timing y fondo (regen IA vía
// "Regenerar fondo (nueva versión)" o swap de biblioteca)—. El regen de fondo
// dejó de vivir en este panel; su lógica ahora es parte del wizard.
export default function EditRequestPanel({ job, onLyricsClick }) {
  const { t } = useI18n();
  const editCount = job.edit_count ?? 0;
  const editsRemaining = job.edits_remaining ?? Math.max(0, 3 - editCount);
  // Admins have no edit cap (backend bypasses it); the panel shows
  // "sin límite" and never gates on editsRemaining.
  const editLimitExempt = job.edit_limit_exempt ?? false;
  const limitReached = !editLimitExempt && editsRemaining <= 0;

  if (limitReached) {
    return (
      <div className="rounded-card p-4 mb-4 bg-surface-2/40 ring-1 ring-white/[0.04] animate-fade-in">
        <div className="flex items-start gap-3">
          <div className="w-8 h-8 rounded-lg bg-amber-500/15 ring-1 ring-amber-500/30 flex items-center justify-center shrink-0">
            <svg className="w-4 h-4 text-amber-400" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
              <circle cx="12" cy="12" r="10" />
              <path d="M12 8v4M12 16h.01" strokeLinecap="round" />
            </svg>
          </div>
          <div className="flex-1 min-w-0">
            <p className="text-sm font-semibold text-white">
              {t("edit.limit_reached_title") || "Ya pediste 3 ediciones"}
            </p>
            <p className="text-xs text-ink-secondary mt-0.5">
              {t("edit.limit_reached_desc") || "Aprobá o rechazá el video. Si todavía no estás conforme, rechazá y empezá un nuevo job."}
            </p>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="rounded-card p-5 mb-4 bg-surface-2/40 ring-1 ring-white/[0.05] animate-fade-in" data-tour="jobdetail-edit-panel">
      <div className="flex items-start justify-between gap-3 mb-3">
        <div>
          <h3 className="text-sm font-semibold tracking-tight">
            {t("edit.panel_title") || "¿Necesitás ajustes?"}
          </h3>
          <p className="text-xs text-ink-secondary mt-0.5">
            {t("edit.panel_desc") || "Editá letra, tipografía, fondo y timing desde el wizard — sin volver a transcribir."}
          </p>
        </div>
        <span className="text-[11px] font-mono text-ink-secondary px-2 py-1 rounded-md bg-surface-3/60 ring-1 ring-white/[0.04] shrink-0">
          {editLimitExempt
            ? (t("edit.no_limit") || "sin límite")
            : editsRemaining === 1
            ? (t("edit.remaining_one") || "1 ed. restante")
            : `${editsRemaining} ${t("edit.remaining_many") || "ed. restantes"}`}
        </span>
      </div>

      <button
        type="button"
        onClick={() => { if (onLyricsClick) onLyricsClick(); }}
        className="w-full text-left p-4 rounded-xl bg-surface-3/40 hover:bg-surface-3/60 ring-1 ring-white/[0.04] hover:ring-brand-light/30 transition-all"
      >
        <div className="flex items-center gap-2 mb-1">
          <svg className="w-4 h-4 text-brand-light" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
            <path d="M9 19V6l12-2v13M9 19a2 2 0 11-4 0 2 2 0 014 0zM21 17a2 2 0 11-4 0 2 2 0 014 0z" strokeLinecap="round" />
          </svg>
          <span className="text-sm font-medium text-white">
            {t("edit.wizard_title") || "Editar y re-renderizar"}
          </span>
        </div>
        <p className="text-[11px] text-ink-secondary">
          {t("edit.wizard_cost") ||
            "~5-10 min · título, artista, letra, tipografía, timing y fondo — todo desde el wizard. Regenerar el fondo cuesta ~US$0.90."}
        </p>
      </button>
    </div>
  );
}
