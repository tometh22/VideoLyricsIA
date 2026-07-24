/**
 * Campos de fondo del POST /generate, gateados por el tab activo del
 * wizard (bgSelectMode: "auto" | "library" | "custom").
 *
 * Bug Ana M. 2026-06-09: backgroundFile/backgroundId viven en App y
 * sobreviven entre batches; el tab visual era state local de UploadZone
 * y volvía a "Generar con IA" en cada remount. App mandaba el archivo
 * stale igual ("if (backgroundFile) append(...)"), así que 3 audios
 * nuevos salieron con la imagen custom de un video anterior mientras la
 * UI prometía fondo IA. Regla única: sólo se envía lo que el tab activo
 * dice — "auto" no manda nada y el backend genera con IA.
 */
export function appendBackgroundFields(body, {
  bgSelectMode,
  backgroundId,
  backgroundMode,
  backgroundFile,
  animateImage,
}) {
  if (bgSelectMode === "library" && backgroundId) {
    body.append("background_id", backgroundId);
    body.append("background_mode", backgroundMode);
  } else if (bgSelectMode === "custom" && backgroundFile) {
    body.append("background_file", backgroundFile);
    if (animateImage) body.append("animate_image", "true");
  }
}
