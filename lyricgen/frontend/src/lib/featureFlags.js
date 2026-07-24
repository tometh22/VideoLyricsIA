// Build-time feature flags. Vite inlines `import.meta.env.VITE_*` at build
// time, so these are compile-time constants — NOT runtime-togglable. A
// rollback means a rebuild + redeploy in the correct Vercel branch scope,
// not a live switch. Mirrors the existing idiom (UploadZone's
// ART_TRACK_ENABLED = import.meta.env.VITE_ART_TRACK_ENABLED === "true").
//
// UNIFIED_EDIT_FLOW: colapsa el doble camino de correcciones (wizard +
// tarjeta "Regenerar fondo") en UN solo camino (el wizard). Cuando está ON,
// JobDetail/EditRequestPanel ocultan la tarjeta de fondo y el wizard toma su
// función (regenerar fondo + validación de contenido). OFF = comportamiento
// histórico intacto. Ver plan Paso 3.
export const UNIFIED_EDIT_FLOW =
  import.meta.env.VITE_UNIFIED_EDIT_FLOW === "true";
