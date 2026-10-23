import React from "react";

// Advisory only. Approval remains one whole-song listening confirmation.
// No apply button here: historical/editor data never enters automatic division.
export default function DisplaySegmentationNotice({ report, stale, onSeek, t }) {
  if (!report || !report.policy_version || report.mode === "off") return null;
  const applied = Number(report.applied_count) || 0;
  const proposed = Number(report.proposed_count) || 0;
  const review = Array.isArray(report.review) ? report.review : [];
  const candidates = !applied && Array.isArray(report.candidates) ? report.candidates : [];
  const points = [...review, ...candidates].filter((item) => {
    const start = Number(item?.start);
    const end = Number(item?.end);
    return Number.isFinite(start) && Number.isFinite(end) && start >= 0 && end > start;
  });
  const label = (key, fallback) => t?.(`editor.display_seg_${key}`) || fallback;
  return (
    <section data-testid="display-segmentation-notice" aria-label={label("title", "División de frases")}
      className="mb-4 rounded-xl border border-sky-400/20 bg-sky-400/5 px-3 py-3 text-xs text-gray-200">
      <p className="font-semibold">{label("title", "División de frases")}</p>
      <p className="mt-1">
        {stale ? label("stale", "El análisis corresponde a una versión anterior. Revisá tus cambios.")
          : ["unavailable", "contract_rejected"].includes(report.status)
            ? label("unavailable", "La división no pudo comprobarse. Revisá las frases largas antes de generar.")
          : applied > 0 ? label("applied", "Se dividieron {count} bloques conservando la letra y sus bordes originales.").replace("{count}", applied)
            : proposed > 0 ? label("shadow", "Se propusieron {count} divisiones, todavía sin aplicar.").replace("{count}", proposed)
              : label("unchanged", "No se aplicaron divisiones automáticas.")}
      </p>
      <p className="mt-1 text-gray-400">{label("listen", "La escucha de letra y timing de la canción completa sigue siendo necesaria.")}</p>
      {points.length > 0 && <div className="mt-2 max-h-48 overflow-auto">
        <p className="mb-1">{label("review", "Tramos para revisar: {count}").replace("{count}", points.length)}</p>
        <div className="flex flex-wrap gap-2">
          {points.map((point, index) => <button key={`${point.index}-${index}`} type="button"
            disabled={stale} onClick={() => onSeek?.(Math.max(0, Number(point.start) - 0.5))}
            className="rounded-lg bg-white/5 px-2 py-1 hover:bg-white/10 disabled:opacity-40">
            {Math.floor(Number(point.start) / 60)}:{String(Math.floor(Number(point.start) % 60)).padStart(2, "0")}
            {point.reason && ` · ${label("manual", "Escuchar antes de cortar")}`}
          </button>)}
        </div>
      </div>}
    </section>
  );
}
