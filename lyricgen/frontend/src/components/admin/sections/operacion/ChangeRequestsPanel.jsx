// Pedidos de cambio de UMG (delivery_change_requests).
//
// El operador filtra pending/resolved/all (chips con badge), ve el contexto
// del delivery (artista, canción, label, frame_size, tenant, owner), un
// preview del video clickeable, el comentario, y resuelve / reabre.
import { useState } from "react";

import { fmtDate } from "../../adminApi";
import FilterBar from "../../primitives/FilterBar";
import EmptyState from "../../primitives/EmptyState";
import TableSkeleton from "../../primitives/TableSkeleton";

export default function ChangeRequestsPanel({
  changeRequests,
  crStatusFilter,
  setCrStatusFilter,
  crPendingCount,
  crResolvedCount,
  crLoading,
  crResolvingId,
  resolveChangeRequest,
  reopenChangeRequest,
}) {
  // Draft local del input de "respuesta" por CR. Clave = id del CR.
  const [drafts, setDrafts] = useState({});
  const setDraft = (id, val) => setDrafts((d) => ({ ...d, [id]: val }));

  const filterOptions = [
    { id: "pending", label: "Pendientes", badge: crPendingCount },
    { id: "resolved", label: "Resueltos", badge: crResolvedCount },
    { id: "all", label: "Todos" },
  ];

  return (
    <div className="space-y-4">
      <FilterBar>
        <FilterBar.Chips
          value={crStatusFilter}
          onChange={setCrStatusFilter}
          options={filterOptions}
          label="Estado"
        />
      </FilterBar>

      {crLoading && changeRequests.length === 0 ? (
        <div className="glass rounded-card p-2">
          <TableSkeleton rows={3} cols={3} />
        </div>
      ) : changeRequests.length === 0 ? (
        <EmptyState
          title={
            crStatusFilter === "pending"
              ? "Sin pedidos pendientes"
              : crStatusFilter === "resolved"
                ? "Sin pedidos resueltos"
                : "Sin pedidos de cambio"
          }
          message={
            crStatusFilter === "pending"
              ? "No hay pedidos de cambio pendientes."
              : "Todavía no hay pedidos en esta categoría."
          }
        />
      ) : (
        <div className="space-y-3">
          {changeRequests.map((item) => (
            <ChangeRequestCard
              key={item.id}
              item={item}
              draft={drafts[item.id] || ""}
              onDraftChange={(v) => setDraft(item.id, v)}
              resolving={crResolvingId === item.id}
              onResolve={() => resolveChangeRequest(item.id, drafts[item.id])}
              onReopen={() => reopenChangeRequest(item.id)}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function ChangeRequestCard({ item, draft, onDraftChange, resolving, onResolve, onReopen }) {
  const d = item.delivery || {};
  const isResolved = !!item.resolved_at;

  return (
    <div
      className={`glass rounded-card p-5 border-l-4 ${
        isResolved ? "border-emerald-500/60 opacity-75" : "border-amber-400"
      }`}
    >
      {/* Contexto del delivery */}
      <div className="flex items-start justify-between gap-3 mb-3 flex-wrap">
        <div className="min-w-0">
          <p className="text-section uppercase tracking-wider text-brand-light font-bold mb-0.5">
            {d.artist || "(sin artista)"}
          </p>
          <h3 className="text-ui font-bold leading-snug text-white">
            {d.song || "(canción eliminada)"}
          </h3>
          <div className="flex items-center gap-2 mt-1 flex-wrap text-label text-gray-500">
            {d.label && <span>{d.label}</span>}
            {d.frame_size && (<><span>·</span><span className="text-brand-light">{d.frame_size}</span></>)}
            {d.job_id && (<><span>·</span><span className="font-mono">job {d.job_id}</span></>)}
            {d.tenant && (<><span>·</span><span>{d.tenant}</span></>)}
            {(d.owner_email || d.owner_username) && (
              <>
                <span>·</span>
                <span title="Usuario que generó el video">
                  por <span className="text-gray-300">{d.owner_email || d.owner_username}</span>
                </span>
              </>
            )}
            {d.removed_at && <span className="text-red-300">· entrega eliminada</span>}
          </div>
        </div>
        <span
          className={`text-section font-bold uppercase px-2 py-1 rounded-button shrink-0 ${
            isResolved ? "bg-emerald-500/15 text-emerald-300" : "bg-amber-500/15 text-amber-300"
          }`}
        >
          {isResolved ? "Resuelto" : "Pendiente"}
        </span>
      </div>

      {/* Preview: thumbnail clickeable que abre el video en pestaña nueva. */}
      {d.thumbnail_url && (
        <a
          href={d.video_url || d.thumbnail_url}
          target="_blank"
          rel="noopener noreferrer"
          className="block relative mb-3 rounded-button overflow-hidden ring-1 ring-white/[0.06] group"
          title={d.video_url ? "Abrir video en pestaña nueva" : "Abrir imagen"}
        >
          <img
            src={d.thumbnail_url}
            alt="Preview del video"
            loading="lazy"
            className="w-full max-h-[220px] object-contain bg-black/40"
          />
          {d.video_url && (
            <span className="absolute inset-0 flex items-center justify-center">
              <span className="w-12 h-12 rounded-full bg-black/50 ring-1 ring-white/30 flex items-center justify-center group-hover:bg-black/70 transition-colors duration-brand">
                <svg className="w-5 h-5 text-white ml-0.5" viewBox="0 0 24 24" fill="currentColor">
                  <path d="M8 5v14l11-7z" />
                </svg>
              </span>
            </span>
          )}
        </a>
      )}

      {/* Comentario del pedido */}
      <div className="rounded-button bg-surface-2/40 ring-1 ring-white/[0.04] p-3 text-caption leading-relaxed whitespace-pre-wrap font-mono text-gray-200">
        {item.comment}
      </div>

      <p className="text-label text-gray-500 mt-2">
        UMG envió este pedido el {fmtDate(item.submitted_at)}
      </p>

      {/* Resolución */}
      {isResolved ? (
        <div className="mt-3 pt-3 border-t border-white/[0.06] flex items-start justify-between gap-3 flex-wrap">
          <div className="text-label text-gray-400 min-w-0">
            <span className="text-emerald-300 font-medium">Resuelto</span>
            {item.resolved_by && <> por <b>{item.resolved_by}</b></>}
            {" "}el {fmtDate(item.resolved_at)}
            {item.resolution_note && (
              <p className="mt-1 text-gray-300 whitespace-pre-wrap">
                <span className="text-gray-500">Respuesta: </span>
                {item.resolution_note}
              </p>
            )}
          </div>
          <button
            onClick={onReopen}
            disabled={resolving}
            className="text-label text-amber-300 hover:text-amber-200 disabled:opacity-50"
          >
            Reabrir
          </button>
        </div>
      ) : (
        <div className="mt-3 pt-3 border-t border-white/[0.06] space-y-2">
          <input
            type="text"
            placeholder="Respuesta opcional (ej: re-renderizado con la línea corregida)"
            value={draft}
            onChange={(e) => onDraftChange(e.target.value)}
            maxLength={2000}
            className="bg-surface-3/40 ring-1 ring-white/[0.06] focus:ring-brand/40 focus:outline-none rounded-button px-3 py-2 text-caption text-white placeholder:text-gray-600 w-full"
          />
          <div className="flex justify-end">
            <button
              onClick={onResolve}
              disabled={resolving}
              className="bg-brand hover:bg-brand-light text-white text-caption font-medium px-3 py-1.5 rounded-button disabled:opacity-50 transition-colors duration-brand"
            >
              {resolving ? "Guardando…" : "Marcar resuelto"}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
