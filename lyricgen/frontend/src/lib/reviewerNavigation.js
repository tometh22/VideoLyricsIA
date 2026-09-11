export const REVIEW_SCOPES = [["pending", "Por revisar"], ["drafts", "Borradores"], ["approved", "Aprobadas"], ["all", "Todas"], ["discarded", "Descartadas"]];
export const validReviewScope = (value) => REVIEW_SCOPES.some(([key]) => key === value) ? value : "pending";

export function reviewStateFilter(value, scope) {
  const approved = ["approved", "exported"];
  const pending = ["pending", "processing", "ready", "reviewing", "failed"];
  const allowed = scope === "all" ? [...approved, ...pending, "discarded"] : scope === "discarded" ? ["discarded"] : scope === "approved" ? approved : pending;
  return allowed.includes(value) ? value : "";
}

export function reviewCounts(queue, campaign) {
  const all = queue?.campaign_totals?.songs ?? campaign?.registered_count;
  const approved = queue?.campaign_totals?.approved;
  const discarded = queue?.campaign_totals?.discarded ?? 0;
  const drafts = queue?.campaign_totals?.drafts ?? 0;
  return { all, approved, discarded, drafts, pending: all == null || approved == null ? undefined : Math.max(0, all - approved - discarded) };
}

export function reviewStateLabel(row) {
  if (row.state === "discarded") return "Descartada";
  if (row.state === "approved") return "Aprobada";
  if (row.state === "exported") return "Exportada";
  if (row.reviewer_lock_active) return row.reviewer_is_current_user
    ? "En revisión por vos" : `En revisión por ${row.reviewer_name || "otra persona"}`;
  if (row.is_draft) return "Borrador guardado";
  if (row.resume_available && ["ready", "reviewing"].includes(row.state)) return "Revisión guardada";
  return { pending: "Pendiente de procesamiento", processing: "Procesando", ready: "Sin revisar",
    reviewing: "En revisión", failed: "Fallida" }[row.state] || "Estado no disponible";
}

export function reviewActionLabel(row, stage = "lyrics") {
  if (!row.job_id) return null;
  if (["approved", "exported"].includes(row.state)) {
    if (stage === "final") return "Ver video";
    if (row.reviewer_lock_active && !row.reviewer_is_current_user) return null;
    return "Editar transcripción";
  }
  if (!["ready", "reviewing"].includes(row.state)) return null;
  if (row.reviewer_lock_active && !row.reviewer_is_current_user) return null;
  if (row.is_draft) return "Continuar borrador";
  return row.resume_available || row.reviewer_is_current_user ? "Continuar" : "Revisar";
}

export function reviewDestination(row, returnTo, stage = "lyrics") {
  // A lyric approval is not a rendered video. Only final-stage approvals
  // open video details; lyrics always resume the durable transcript editor.
  const route = stage === "final" && ["approved", "exported"].includes(row.state) ? "videos" : "review";
  return `/${route}/${encodeURIComponent(row.job_id)}?return_to=${encodeURIComponent(returnTo)}`;
}

export function safeReviewReturnPath(value) {
  if (!value?.startsWith("/") || value.startsWith("//")) return null;
  try {
    const url = new URL(value, "https://genly.invalid");
    return url.origin === "https://genly.invalid" &&
      (url.pathname === "/admin/cola" || /^\/campaigns\/[^/]+$/.test(url.pathname)) ? value : null;
  } catch { return null; }
}
