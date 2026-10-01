// Approval validates; it never repairs the editor's saved snapshot.
// Compare at the backend's persisted resolution (four decimal seconds).
export function approvalConflict(segments) {
  if (!segments.length) return "No hay líneas para aprobar.";
  const rows = segments.map((s, index) => ({ ...s, index }));
  for (const row of rows) {
    if (!Number.isFinite(row.start) || !Number.isFinite(row.end)
        || row.start < 0 || row.end <= row.start) {
      return `La línea ${row.index + 1} tiene tiempos inválidos. Corregila antes de aprobar.`;
    }
    if (!(row.text || "").trim()) return `La línea ${row.index + 1} está vacía. Editala o eliminála explícitamente.`;
  }
  rows.sort((a, b) => a.start - b.start);
  for (let i = 1; i < rows.length; i += 1) {
    if (Math.round(rows[i - 1].end * 10000) > Math.round(rows[i].start * 10000)) {
      return `Las líneas ${rows[i - 1].index + 1} y ${rows[i].index + 1} se solapan. Ajustá el conflicto antes de aprobar; tus tiempos no se modificaron.`;
    }
  }
  return null;
}
