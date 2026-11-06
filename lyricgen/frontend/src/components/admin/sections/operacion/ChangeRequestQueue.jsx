import { fmtAgo } from "../../adminApi";
import {
  PORTAL_LABELS,
  requestKind,
  requestWorkflow,
  workflowCounts,
} from "./changeRequestWorkflow";

const FILTERS = [
  { id: "all", label: "Todos" },
  { id: "action", label: "Acción" },
  { id: "rendering", label: "Render" },
  { id: "review", label: "Revisar" },
];

const DOTS = {
  action: "bg-brand",
  attention: "bg-amber-400",
  busy: "bg-sky-400 animate-pulse",
  done: "bg-emerald-400",
  idle: "bg-gray-500",
};

export default function ChangeRequestQueue({
  items,
  allItems,
  selectedId,
  onSelect,
  proposals,
  proposalEnabled,
  search,
  onSearchChange,
  stageFilter,
  onStageFilterChange,
  searchInputRef,
}) {
  const counts = workflowCounts(allItems, proposals, proposalEnabled);
  return (
    <aside className="min-w-0 overflow-hidden rounded-2xl bg-surface-2/35 ring-1 ring-white/[0.07] xl:sticky xl:top-4 xl:max-h-[calc(100vh-7rem)]">
      <div className="border-b border-white/[0.06] p-3 space-y-3">
        <label className="relative block">
          <span className="sr-only">Buscar pedidos</span>
          <svg className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-gray-500" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <circle cx="11" cy="11" r="7" /><path d="m20 20-3.5-3.5" />
          </svg>
          <input
            ref={searchInputRef}
            type="search"
            value={search}
            onChange={(event) => onSearchChange(event.target.value)}
            placeholder="Canción, artista, job o pedido…"
            className="w-full rounded-xl bg-black/20 py-2.5 pl-9 pr-3 text-caption text-white ring-1 ring-white/[0.08] placeholder:text-gray-600 focus:outline-none focus:ring-brand/50"
          />
        </label>
        <div className="grid grid-cols-4 gap-1 rounded-xl bg-black/15 p-1" aria-label="Filtrar por etapa">
          {FILTERS.map((filter) => {
            const count = counts[filter.id] || 0;
            const selected = stageFilter === filter.id;
            return (
              <button
                key={filter.id}
                type="button"
                onClick={() => onStageFilterChange(filter.id)}
                className={`rounded-lg px-1.5 py-2 text-[10px] font-semibold transition-colors ${
                  selected ? "bg-white/[0.1] text-white" : "text-gray-500 hover:text-gray-300"
                }`}
              >
                <span className="block">{filter.label}</span>
                <span className={selected ? "text-brand-light" : "text-gray-600"}>{count}</span>
              </button>
            );
          })}
        </div>
      </div>

      <div className="max-h-[34rem] overflow-y-auto xl:max-h-[calc(100vh-15.5rem)]" role="listbox" aria-label="Cola de pedidos">
        {items.length === 0 ? (
          <div className="px-5 py-10 text-center">
            <p className="text-caption font-medium text-gray-300">No hay coincidencias</p>
            <p className="mt-1 text-label text-gray-600">Probá otro filtro o búsqueda.</p>
          </div>
        ) : items.map((item) => {
          const delivery = item.delivery || {};
          const workflow = requestWorkflow(item, proposals?.[item.id], proposalEnabled);
          const selected = String(item.id) === String(selectedId);
          return (
            <button
              key={item.id}
              type="button"
              role="option"
              aria-selected={selected}
              onClick={() => onSelect(item.id)}
              className={`w-full border-b border-white/[0.05] px-4 py-3 text-left transition-colors last:border-b-0 ${
                selected ? "bg-brand/[0.10] shadow-[inset_3px_0_0_#7c3aed]" : "hover:bg-white/[0.035]"
              }`}
            >
              <div className="flex items-start gap-2.5">
                <span className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${DOTS[workflow.tone] || DOTS.idle}`} />
                <div className="min-w-0 flex-1">
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0">
                      <p className="truncate text-caption font-semibold text-white">
                        {delivery.song || "Canción eliminada"}
                      </p>
                      <p className="truncate text-label text-gray-400">
                        {delivery.artist || "Sin artista"}
                      </p>
                    </div>
                    <span className="shrink-0 rounded-full bg-white/[0.06] px-2 py-0.5 text-[10px] font-medium text-gray-300">
                      {requestKind(item, proposals?.[item.id])}
                    </span>
                  </div>
                  <p className={`mt-2 truncate text-label font-medium ${
                    workflow.tone === "attention" ? "text-amber-300"
                      : workflow.tone === "busy" ? "text-sky-300"
                        : workflow.tone === "done" ? "text-emerald-300" : "text-brand-light"
                  }`}>
                    {workflow.label}
                  </p>
                  <div className="mt-1.5 flex items-center justify-between gap-2 text-[10px] text-gray-600">
                    <span className="truncate">{PORTAL_LABELS[delivery.portal_id] || delivery.portal_id || "Portal"}</span>
                    <span className="shrink-0">{fmtAgo(item.submitted_at)}</span>
                  </div>
                </div>
              </div>
            </button>
          );
        })}
      </div>
      <div className="hidden border-t border-white/[0.06] px-4 py-2 text-[10px] text-gray-600 xl:flex xl:justify-between">
        <span>J/K para navegar</span>
        <span>/ para buscar</span>
      </div>
    </aside>
  );
}
