/**
 * "Qué va a pasar cuando apruebe", en el punto de commit.
 *
 * Por qué NO es un modal de confirmación: los productores que hacen lotes
 * aprenden a clickear modales, y un aviso que se despacha con un Enter deja de
 * ser un aviso. Esto vive fijo bajo el preview, no se puede saltear ni cerrar.
 *
 * Por qué se alimenta de `resolveEditSubmission` y NO del diff: el diff no
 * decide el output — la degradación por status y el gate de multi-escena sí. Un
 * resumen construido sobre `computeFieldDiff` diría "Movimiento: Animado →
 * Estático" en un video que está por descartar ese cambio, que es el bug
 * original reimplementado una capa más arriba.
 *
 * Muestra los BUCKETS, no los campos: es lo que el backend realmente aplica o
 * descarta como unidad. Bajar a campo por campo sería más lindo y menos cierto.
 */

const BUCKET_LABELS = (t) => ({
  background: t("plan.bucket_background") || "Fondo",
  background_library: t("plan.bucket_background_library") || "Fondo de Biblioteca",
  lyrics: t("plan.bucket_lyrics") || "Letra",
  metadata: t("plan.bucket_metadata") || "Título y artista",
  typography: t("plan.bucket_typography") || "Tipografía y portada",
});

export default function EditPlanSummary({ plan, t }) {
  if (!plan) return null;

  const labels = BUCKET_LABELS(t);
  const applied = Object.keys(plan.willApply || {});
  const dropped = plan.willDrop || [];

  // Bloqueado: el aviso completo vive arriba, en el bloque de fondo (donde el
  // operador clickea). Acá no repetimos.
  if (plan.blocked) return null;

  if (applied.length === 0 && dropped.length === 0) {
    return (
      <p className="text-[10px] text-gray-600 px-1 mt-1" data-testid="edit-plan-summary">
        {t("plan.nothing_yet") || "Todavía no cambiaste nada."}
      </p>
    );
  }

  return (
    <div
      className="mt-1.5 px-1 space-y-1"
      data-testid="edit-plan-summary"
      // aria-live: el resumen cambia mientras el operador toca controles en
      // otros pasos; que un lector de pantalla lo anuncie es el punto.
      aria-live="polite"
    >
      {applied.length > 0 && (
        <p className="text-[10px] text-gray-400 leading-snug">
          <span className="text-gray-500">
            {t("plan.will_apply") || "Al aprobar se aplica"}:{" "}
          </span>
          <span className="text-gray-200 font-medium" data-testid="plan-applied">
            {applied.map((b) => labels[b] || b).join(" · ")}
          </span>
        </p>
      )}
      {dropped.length > 0 && (
        <p className="text-[10px] leading-snug" data-testid="plan-dropped">
          <span className="text-amber-300/80">
            {t("plan.will_drop") || "NO se aplica"}:{" "}
          </span>
          <span className="text-amber-200 font-medium">
            {dropped.map((b) => labels[b] || b).join(" · ")}
          </span>
          <span className="block text-[9.5px] text-amber-200/50">
            {t("plan.will_drop_why") || "Este video ya está aprobado — para cambiar el fondo, creá una variante."}
          </span>
        </p>
      )}
    </div>
  );
}
