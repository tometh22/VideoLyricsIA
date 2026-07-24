// Panel "¿Qué falló?" — portado de la vieja ActividadView (absorbida por
// Insights 2026-06-10): cada error con su categoría, usuario, canción,
// mensaje y cuándo, sin tener que abrir drill-downs.
import { useState } from "react";

import { fmtAgo, ERROR_CATEGORY_LABELS } from "../../adminApi";

function CategoryChip({ category }) {
  if (!category) return null;
  return (
    <span className="shrink-0 px-1.5 py-0.5 rounded bg-red-500/10 ring-1 ring-red-500/20 text-label text-red-300 whitespace-nowrap">
      {ERROR_CATEGORY_LABELS[category] || category}
    </span>
  );
}

export default function ProblemsPanel({ recentErrors = [], errorsByCategory = {} }) {
  const [expanded, setExpanded] = useState(true);
  // Profundidad 2026-06-11: click en un chip de categoría → filtra la lista.
  const [categoryFilter, setCategoryFilter] = useState(null);
  if (recentErrors.length === 0) return null;
  const visible = categoryFilter
    ? recentErrors.filter((e) => e.category === categoryFilter)
    : recentErrors;

  return (
    <div className="glass-elevated rounded-card overflow-hidden ring-1 ring-red-500/20">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="w-full flex items-center gap-3 px-5 py-3.5 text-left hover:bg-white/[0.02] transition-colors duration-brand"
      >
        <span className="w-2 h-2 rounded-full bg-red-400 shrink-0" />
        <span className="text-ui font-semibold text-white flex-1">
          ¿Qué falló? <span className="text-red-300">({recentErrors.length} {recentErrors.length === 1 ? "error" : "errores"})</span>
        </span>
        <span className="hidden sm:flex items-center gap-1.5 flex-wrap justify-end">
          {Object.entries(errorsByCategory || {})
            .sort((a, b) => b[1] - a[1])
            .slice(0, 4)
            .map(([cat, n]) => (
              <span
                key={cat}
                role="button"
                tabIndex={0}
                onClick={(ev) => {
                  ev.stopPropagation();
                  setCategoryFilter((cur) => (cur === cat ? null : cat));
                  setExpanded(true);
                }}
                className={`px-2 py-0.5 rounded-full text-label cursor-pointer transition-colors duration-brand ${
                  categoryFilter === cat
                    ? "bg-red-500/30 ring-1 ring-red-400 text-white"
                    : "bg-red-500/10 text-red-300 hover:bg-red-500/20"
                }`}
                title="Click: filtrar por esta categoría"
              >
                {ERROR_CATEGORY_LABELS[cat] || cat}: {n}
              </span>
            ))}
        </span>
        <svg
          className={`w-4 h-4 text-gray-500 shrink-0 transition-transform duration-brand ${expanded ? "rotate-180" : ""}`}
          fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24"
        >
          <path d="M6 9l6 6 6-6" />
        </svg>
      </button>

      {expanded && (
        <div className="px-5 pb-4 divide-y divide-white/[0.04]">
          {visible.slice(0, 10).map((e, i) => (
            <div key={`${e.job_id}-${i}`} className="py-2.5 flex items-start gap-3">
              <CategoryChip category={e.category} />
              <div className="flex-1 min-w-0">
                <p className="text-caption text-gray-300">
                  <span className="font-medium text-white">{e.artist} — {e.song_title || e.job_id}</span>
                  {e.username && <span className="text-gray-500"> · {e.username}</span>}
                </p>
                <p className="text-label font-mono text-red-300/90 break-words mt-0.5">
                  {e.error || "(sin mensaje de error)"}
                </p>
              </div>
              <span className="shrink-0 text-label text-gray-500 whitespace-nowrap">{fmtAgo(e.created_at)}</span>
            </div>
          ))}
          {visible.length > 10 && (
            <p className="pt-2.5 text-label text-gray-500">
              … y {visible.length - 10} más en la ventana
            </p>
          )}
        </div>
      )}
    </div>
  );
}
