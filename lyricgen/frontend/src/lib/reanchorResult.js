// Both HTTPException and JSONResponse errors are returned by the reanchor API.
// Task polling wraps that same response in {http_status, payload}.
export function reanchorHttpFailure(status, payload, { terminal = false } = {}) {
  const detail = payload?.detail;
  const error = detail && typeof detail === "object" && !Array.isArray(detail)
    ? detail : payload;
  return {
    ok: false,
    reason: `http-${status}`,
    status,
    terminal,
    code: error?.code || payload?.code || null,
    detail: typeof detail === "string" ? detail : error?.detail || "",
    structure: error?.structure || payload?.structure || null,
    structural: error?.structural || payload?.structural || null,
  };
}

export function shouldRecoverReanchor(result) {
  // A completed refusal cannot turn into a successful alignment. In
  // particular, an unrelated autosave advancing the revision is not success.
  if (result?.terminal || result?.reason === "declined" || result?.reason === "structural_mismatch") return false;
  // Legacy synchronous servers can return a duplicate request's conflict
  // while the original request is still applying the alignment.
  if (result?.status === 409 && result?.code === "stale_revision") return true;
  if (result?.status >= 400 && result.status < 500) {
    // A request timeout can have happened after the server started work.
    return result.status === 408;
  }
  return true;
}

export function reanchorFailureMessage(result, t) {
  if (result?.phase === "save") {
    return t("editor.reanchor_save_failed") || "No se inició la re-sincronización porque no se pudieron guardar tus cambios. Reintentá el guardado; la letra pegada sigue acá.";
  }
  if (result?.code === "stale_revision" || result?.code === "editor_state_conflict") {
    return t("editor.reanchor_conflict") || "La versión guardada cambió. No se aplicó la re-sincronización. Conservá la letra pegada y volvé a cargar la versión actual antes de reintentar.";
  }
  if (result?.reason === "declined") {
    return t("editor.reanchor_declined") || "No se pudo re-sincronizar: el motor no obtuvo una alineación utilizable. La letra pegada sigue acá y los tiempos no cambiaron. Podés reintentar o ajustar los tiempos manualmente.";
  }
  // These endpoints already supply actionable, human-readable errors (e.g.
  // original audio unavailable). Never render an object or a machine token.
  if (typeof result?.detail === "string" && /\s/.test(result.detail)) return result.detail;
  if (shouldRecoverReanchor(result)) {
    return t("editor.reanchor_unconfirmed") || "No se pudo confirmar si terminó la re-sincronización. La letra pegada sigue acá. Comprobá la versión guardada antes de reintentar.";
  }
  return t("editor.reanchor_failed") || "No se pudo re-sincronizar — el timing quedó como estaba.";
}
