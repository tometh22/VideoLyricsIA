// Resumen antes de publicar al portal (backend/publish_review.py,
// PUBLISH_REVIEW_SUMMARY=prompt): líneas que nadie escuchó en el editor y
// avisos precisos de la Revisión rápida que quedaron sin mirar. El backend
// responde 409 `publish_review_required` con los resúmenes; "Publicar igual"
// reenvía el token de cada resumen visto.

export const PUBLISH_REVIEW_CODE = "publish_review_required";

export const CATEGORY_LABELS = {
  official: "Letra oficial",
  two_ears: "Dos oídos de acuerdo",
  style: "Estilo del portal",
};

// `detail` es el cuerpo de un 409 (`{detail: {...}}` ya desenvuelto o no).
export function publishReviewFrom(detail) {
  const value = detail?.detail && typeof detail.detail === "object" ? detail.detail : detail;
  if (!value || value.code !== PUBLISH_REVIEW_CODE || !Array.isArray(value.summaries)) return null;
  return { summaries: value.summaries, totals: value.totals || totalsOf(value.summaries) };
}

export function totalsOf(summaries = []) {
  return summaries.reduce((acc, summary) => ({
    songs: acc.songs + 1,
    songs_needing_attention: acc.songs_needing_attention + (summary.needs_attention ? 1 : 0),
    lines_unheard: acc.lines_unheard + (Number(summary.lines_unheard) || 0),
    lines_total: acc.lines_total + (Number(summary.lines_total) || 0),
    warnings: acc.warnings + (Number(summary.warnings_count) || 0),
  }), { songs: 0, songs_needing_attention: 0, lines_unheard: 0, lines_total: 0, warnings: 0 });
}

export function tokensOf(summaries = []) {
  return Object.fromEntries(summaries
    .filter((summary) => summary?.job_id && typeof summary.token === "string")
    .map((summary) => [summary.job_id, summary.token]));
}

export function clock(seconds) {
  const value = Math.max(0, Math.floor(Number(seconds) || 0));
  return `${Math.floor(value / 60)}:${String(value % 60).padStart(2, "0")}`;
}

const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;

// "12 de 40 líneas sin escuchar · 3 avisos sin mirar"
export function headline(summary) {
  const parts = [];
  if (summary.lines_unheard > 0) {
    parts.push(`${summary.lines_unheard} de ${plural(summary.lines_total, "línea", "líneas")} sin escuchar`);
  }
  if (summary.warnings_count > 0) parts.push(`${plural(summary.warnings_count, "aviso", "avisos")} sin mirar`);
  return parts.join(" · ");
}

export function totalsText(totals) {
  const parts = [];
  if (totals.lines_unheard > 0) parts.push(`${plural(totals.lines_unheard, "línea", "líneas")} sin escuchar`);
  if (totals.warnings > 0) parts.push(`${plural(totals.warnings, "aviso", "avisos")} sin mirar`);
  const songs = totals.songs_needing_attention || totals.songs || 0;
  return `${parts.join(" y ")}${songs > 1 ? ` en ${songs} canciones` : ""}`;
}
