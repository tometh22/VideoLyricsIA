import { REVIEW_SCOPES } from "../lib/reviewerNavigation";

export default function ReviewScopes({ value, counts, onChange, cards = false, showSavedChanges = true }) {
  return <div><nav aria-label={cards ? "Resumen de la campaña" : "Filtrar canciones"}
    role={cards ? undefined : "tablist"}
    className={cards ? "grid grid-cols-2 gap-3 sm:grid-cols-4" : "flex flex-wrap gap-2 border-b border-white/10"}>
    {REVIEW_SCOPES.map(([key, label], index) => <button key={key} type="button"
      role={cards ? undefined : "tab"} aria-selected={cards ? undefined : (value === key || (key === "pending" && value === "drafts"))}
      aria-pressed={cards ? (value === key || (key === "pending" && value === "drafts")) : undefined} tabIndex={cards || (value === key || (key === "pending" && value === "drafts")) ? 0 : -1}
      onClick={() => onChange(key)} onKeyDown={(event) => {
        if (cards || !["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        const size = REVIEW_SCOPES.length;
        const next = event.key === "Home" ? 0 : event.key === "End" ? size - 1
          : (index + (event.key === "ArrowRight" ? 1 : size - 1)) % size;
        event.currentTarget.parentElement.querySelectorAll("button")[next].focus();
        onChange(REVIEW_SCOPES[next][0]);
      }} className={`${cards ? "rounded-2xl p-4 text-left ring-1" : "border-b-2 px-4 py-3"} font-semibold transition focus-visible:outline focus-visible:outline-2 focus-visible:outline-brand ${(value === key || (key === "pending" && value === "drafts")) ? "border-brand bg-brand/10 text-white ring-brand/40" : "border-transparent bg-surface-2/40 text-ink-secondary ring-white/10 hover:bg-white/10"}`}>
      {cards ? <><span className="block text-2xl tabular-nums">{counts[key] ?? "—"}</span><span className="text-sm">{label}</span></>
        : <>{label} <span className="ml-1 text-xs tabular-nums">{counts[key] ?? "—"}</span></>}
    </button>)}
  </nav>
    {!cards && <p className="mt-2 text-sm text-ink-secondary">
      {counts.all ?? "—"} canciones en total = {counts.pending ?? "—"} por revisar + {counts.approved ?? "—"} aprobadas + {counts.discarded ?? "—"} descartadas.
    </p>}
    {!cards && showSavedChanges && <div className="mt-3 flex flex-wrap items-center gap-3 text-sm">
      <span className="text-ink-secondary">Dentro de las pendientes:</span>
      <button type="button" aria-pressed={value === "drafts"}
        onClick={() => onChange(value === "drafts" ? "pending" : "drafts")}
        className={`rounded-lg px-3 py-2 ring-1 ${value === "drafts" ? "bg-brand/15 text-brand-light ring-brand/30" : "text-ink-secondary ring-white/10"}`}>
        Con cambios humanos guardados {counts.drafts ?? "—"}
      </button>
      <span className="text-xs text-ink-tertiary">Incluidas en “Por revisar”; todavía sin aprobar. Los guardados automáticos no cuentan como revisión.</span>
    </div>}
  </div>;
}
