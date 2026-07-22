import { sanitizeSegmentsForSave, findSanitizedDiffs } from "./sanitizeSegments";

// Persist de segmentos al backend (POST /jobs/{id}/save-segments), extraído
// de App.jsx (PR F). `authFetch` y `API` se INYECTAN en vez de importarlos
// para que la función sea unit-testeable sin el wrapper de fetch de App ni
// import.meta.env — el test pasa un authFetch mock y asserta el contrato real
// (antes PersistSegmentsRoundTrip testeaba un mirror inline con el eco ya
// removido: no protegía nada de producción).
//
// Contrato de retorno (lo consume el saveQueue / saveStatus del editor):
//   { ok: true }                                   éxito
//   { ok: false, reason: "no-data" }               input inválido (no se envió)
//   { ok: false, reason: "job-gone", status: 404 } job reapeado
//   { ok: false, reason: "http-<code>", status }   rechazo del backend
//   { ok: false, reason: "network", error }        el fetch tiró
//
// El motivo alimenta el copy honesto del banner (network / session(401) /
// job-gone(404) / server). Ver LyricsEditor `_SAVE_ERROR_COPY`.
export async function persistSegments(authFetch, API, jobId, segments, opts = {}) {
  if (!jobId || !Array.isArray(segments) || segments.length === 0) {
    return { ok: false, reason: "no-data" };
  }
  // Sanitizar al contrato del backend ANTES del POST. Un solo segmento fuera
  // de contrato (NaN/invertido/fuera de rango de un drag o edición manual)
  // hacía 400 todo el save → saveStatus trababa en "error" → "Aprobar"
  // bloqueado para el job (incidente 2026-06-26, Universal AR). Clampeamos
  // para que un valor suelto degrade a "guardado, corregible" en vez de
  // bloquear todo.
  const safeSegments = sanitizeSegmentsForSave(segments);
  const _fixed = findSanitizedDiffs(segments, safeSegments);
  if (_fixed.length) {
    // Igual reportamos QUÉ estaba fuera de contrato (tag [autosave] en Sentry)
    // aunque ya no fallemos — para rastrear el path de edición de origen.
    console.warn("[autosave] sanitized out-of-contract segment(s) before save", {
      jobId, count: _fixed.length, sample: _fixed.slice(0, 3),
    });
  }
  // Canario [drag-persist] del reseed-storm (se remueve en PR G junto a la
  // causa, NO antes — es la señal que confirma que el storm quedó silenciado).
  const _sample = safeSegments.slice(0, 2).map((s) => ({
    start: Math.round((s.start || 0) * 1000) / 1000,
    end: Math.round((s.end || 0) * 1000) / 1000,
  }));
  console.warn("[drag-persist] POST", { jobId, count: safeSegments.length, sample: _sample });
  try {
    const res = await authFetch(`${API}/jobs/${jobId}/save-segments`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ segments: safeSegments }),
      // keepalive deja que el POST sobreviva un unload (refresh / cierre de
      // tab). El flush de unload lo setea para que las últimas ediciones aún
      // no debounced no se cancelen mid-flight (reporte Gaby 2026-06-24:
      // refrescó para salir del titileo y perdió TODO). Body cap 64KB.
      keepalive: opts.keepalive === true,
    });
    if (!res.ok) {
      if (res.status === 400) {
        // Post-sanitize esto debería ser inalcanzable por forma de segmento,
        // pero si el backend rechaza por un motivo que no espejamos, capturamos
        // el `detail` exacto en vez de un status pelado — lo que faltó el 06-26.
        let detail = "";
        try { detail = (await res.clone().json())?.detail || ""; } catch { /* non-JSON */ }
        console.warn("[autosave] /save-segments rejected 400", detail);
      } else if (res.status !== 404) {
        // 404 = el job ya fue reapeado — nada contra qué guardar. Lo logueamos
        // como warning suave; el usuario verá el error real al "Crear videos".
        console.warn("[autosave] /save-segments failed", res.status);
      }
      return {
        ok: false,
        reason: res.status === 404 ? "job-gone" : `http-${res.status}`,
        status: res.status,
      };
    }
    // El eco post-200 (setCurrentReview con el snapshot enviado, que pisaba
    // ediciones in-flight) se ELIMINÓ en la auditoría 2026-06-10; desde PR E
    // la fuente de frescura es el segmentsStore, así que no escribimos nada
    // de vuelta acá.
    return { ok: true };
  } catch (err) {
    console.warn("[autosave] /save-segments network error", err);
    return { ok: false, reason: "network", error: String(err) };
  }
}
