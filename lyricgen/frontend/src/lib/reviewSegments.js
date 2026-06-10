import { segmentsValuesEqual } from "./segmentsValuesEqual";

/**
 * Reducer puro del mirror editor→currentReview (auditoría 2026-06-10).
 *
 * Contrato crítico: cuando los valores NO cambiaron, devuelve el MISMO
 * objeto (identidad estable) — eso corta el loop de re-renders
 * App↔LyricsEditor que saturaba el main thread durante la sincronización
 * ("se mueve y no me deja hacer cambios"). Solo crea un objeto nuevo
 * cuando hay una edición real que propagar al WizardLivePreview.
 */
export function mergeEditedSegments(review, segs) {
  if (!review) return review;
  if (segmentsValuesEqual(review.segments, segs)) return review;
  return { ...review, segments: segs };
}
