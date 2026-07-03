// Ficha completa de UN video (modal) — el fondo del drill-down de
// Insights: parámetros elegidos, cada llamada IA con su costo, eventos
// de auditoría y error. Se abre desde cualquier lista de videos
// (perfil de usuario, drill de features).
import { useEffect, useState } from "react";

import { API, fetchJson, fmtAgo, fmtMoney } from "../../adminApi";
import StatusBadge from "../../primitives/StatusBadge";

function Row({ label, children }) {
  return (
    <div className="flex items-start gap-2 text-caption">
      <span className="text-gray-500 w-32 shrink-0">{label}</span>
      <span className="flex-1 text-gray-200 min-w-0 break-words">{children}</span>
    </div>
  );
}

export default function JobDetailPanel({ jobId, onClose }) {
  const [detail, setDetail] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let alive = true;
    setDetail(null);
    setError(null);
    fetchJson(`${API}/admin/insights/job/${jobId}`)
      .then((d) => { if (alive) setDetail(d); })
      .catch((e) => { if (alive) setError(e.message); });
    return () => { alive = false; };
  }, [jobId]);

  return (
    <div
      className="fixed inset-0 z-50 bg-black/50 backdrop-blur-[2px] flex justify-end"
      onClick={onClose}
    >
      {/* Drawer lateral (rediseño world-class 2026-06-12): la ficha entra
          desde la derecha como en Stripe/Linear, con el contexto de fondo
          visible — chau modal flotante crudo. */}
      <div
        className="h-full w-full max-w-xl bg-[#101016]/97 ring-1 ring-white/10 shadow-2xl flex flex-col animate-slide-in-right"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header FIJO: antes todo el drawer era overflow-y-auto y el
            header (con "Ver video ↗") scrolleaba fuera de pantalla en
            cuanto la lista de llamadas IA crecía — reporte 2026-07-03:
            "no se ve el botón violeta de ir al video, queda cortado".
            Header fuera del scroll = botón siempre visible. */}
        <div className="shrink-0 p-6 pb-3 border-b border-white/[0.06] flex items-start justify-between gap-3">
          <div className="min-w-0">
            <h3 className="text-lg font-bold text-white truncate">
              {detail ? `${detail.artist} — ${detail.song_title || "(sin título)"}` : `Video ${jobId}`}
            </h3>
            <p className="text-label text-gray-500 font-mono">
              {jobId}
              {detail?.username && ` · ${detail.username}`}
              {detail?.tenant_id && ` · ${detail.tenant_id}`}
            </p>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <a
              href={`/videos/${jobId}`}
              target="_blank"
              rel="noopener noreferrer"
              className="px-3 py-1 rounded-md bg-brand/15 ring-1 ring-brand/40 text-caption text-brand-light hover:bg-brand/25 transition-colors duration-brand whitespace-nowrap"
              title="Abrir el video en la plataforma (pestaña nueva)"
            >
              Ver video ↗
            </a>
            <button
              type="button"
              onClick={onClose}
              className="text-gray-400 hover:text-white px-2 py-1 text-caption"
            >
              ✕ cerrar
            </button>
          </div>
        </div>

        <div className="flex-1 overflow-y-auto p-6 pt-4 space-y-4">
        {error && <p className="text-caption text-red-300">{error}</p>}
        {!detail && !error && <p className="text-caption text-gray-500">Cargando…</p>}

        {detail && (
          <>
            <div className="flex items-center gap-3 flex-wrap">
              <StatusBadge status={detail.status} />
              {detail.edit_count > 0 && (
                <span className="text-label text-amber-300">{detail.edit_count} re-renders</span>
              )}
              {detail.parent_job_id && (
                <span className="text-label text-gray-400">variante de {detail.parent_job_id}</span>
              )}
              <span className="text-label text-gray-500">creado {fmtAgo(detail.created_at)}</span>
              {detail.approved_at && (
                <span className="text-label text-accent">aprobado {fmtAgo(detail.approved_at)}</span>
              )}
            </div>

            {detail.error && (
              <div className="rounded-card bg-red-500/[0.08] ring-1 ring-red-500/25 px-3 py-2">
                <p className="text-label text-red-300 font-mono break-words">
                  [{detail.error_category || "?"}] {detail.error}
                </p>
              </div>
            )}

            {/* Parámetros elegidos (render_params no vacíos) */}
            <div>
              <p className="text-section uppercase text-gray-500 mb-2">Parámetros elegidos</p>
              <div className="flex flex-wrap gap-1.5">
                <span className="px-2 py-0.5 rounded-full bg-surface-3/50 ring-1 ring-white/[0.06] text-label text-gray-300">
                  estilo: {detail.choices?.style || "—"}
                </span>
                <span className="px-2 py-0.5 rounded-full bg-surface-3/50 ring-1 ring-white/[0.06] text-label text-gray-300">
                  fondo: {detail.choices?.background_source || "?"}
                </span>
                {Object.entries(detail.render_params || {})
                  .filter(([, v]) => typeof v !== "object" || v === null)
                  .map(([k, v]) => (
                  <span key={k} className="px-2 py-0.5 rounded-full bg-brand/10 ring-1 ring-brand/25 text-label text-brand-light">
                    {k}: {String(v).slice(0, 40)}
                  </span>
                ))}
              </div>
            </div>

            {/* Llamadas IA con costo */}
            <div>
              <p className="text-section uppercase text-gray-500 mb-2">
                Llamadas IA ({detail.ai_calls.length}) · {fmtMoney(detail.ai_cost_usd)}
              </p>
              <div className="space-y-1 max-h-44 overflow-y-auto pr-1">
                {detail.ai_calls.length === 0 && (
                  <p className="text-caption text-gray-500">Sin llamadas IA registradas.</p>
                )}
                {detail.ai_calls.map((c, i) => (
                  <div key={i} className="flex items-center gap-2 text-caption">
                    <span className="font-mono text-gray-400 w-32 shrink-0 truncate" title={c.step}>{c.step}</span>
                    <span className="flex-1 text-gray-300 truncate">{c.tool_name}</span>
                    <span className="text-gray-500 tabular-nums">
                      {c.duration_ms != null ? `${(c.duration_ms / 1000).toFixed(1)}s` : "—"}
                    </span>
                    <span className="font-mono text-white tabular-nums w-16 text-right">
                      {fmtMoney(c.cost_usd)}
                    </span>
                  </div>
                ))}
              </div>
            </div>

            {/* Eventos */}
            <div>
              <p className="text-section uppercase text-gray-500 mb-2">
                Eventos ({detail.events.length})
              </p>
              <div className="space-y-1 max-h-40 overflow-y-auto pr-1">
                {detail.events.length === 0 && (
                  <p className="text-caption text-gray-500">Sin eventos de auditoría.</p>
                )}
                {detail.events.map((e, i) => (
                  <div key={i} className="flex items-center gap-2 text-caption">
                    <span className="font-mono text-gray-400">{e.action}</span>
                    <span className="flex-1 text-gray-500 truncate">
                      {e.detail?.edit_type || e.detail?.file_type || ""}
                    </span>
                    <span className="text-gray-500 shrink-0">{fmtAgo(e.created_at)}</span>
                  </div>
                ))}
              </div>
            </div>

            <Row label="Perfil de entrega">{detail.delivery_profile}</Row>
          </>
        )}
        </div>
      </div>
    </div>
  );
}
