import { EXTRA_STAGES, stagesFor, totalOf, toneOf } from "../../lib/campaignPipeline";

/**
 * The campaign's navigation *is* its pipeline. Every tab counts songs with
 * the same backend rule, so the numbers add up to the campaign total.
 */
export default function PipelineBar({ counts, kind, value, onChange, loading = false, badges = {} }) {
  const stages = stagesFor(kind);
  const active = totalOf(counts);
  const tabs = [
    { key: "all", label: "Todas", count: active, tone: null },
    ...stages.map((stage) => ({ key: stage.key, label: stage.label, title: stage.title, count: counts?.[stage.key] ?? 0, tone: toneOf(stage.key), human: stage.human })),
    ...EXTRA_STAGES.map((stage) => ({ key: stage.key, label: stage.label, title: stage.title, count: counts?.[stage.key] ?? 0, tone: toneOf(stage.key), extra: true })),
  ].filter((tab) => !tab.extra || tab.count > 0 || tab.key === value);

  const move = (event, index) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const size = tabs.length;
    const next = event.key === "Home" ? 0 : event.key === "End" ? size - 1
      : (index + (event.key === "ArrowRight" ? 1 : size - 1)) % size;
    event.currentTarget.parentElement.querySelectorAll("[role=tab]")[next]?.focus();
    onChange(tabs[next].key);
  };

  return <div className="space-y-3">
    <div className="flex h-2.5 overflow-hidden rounded-full bg-white/[0.06]" aria-hidden="true">
      {!loading && active > 0 && stages.map((stage) => {
        const count = counts?.[stage.key] ?? 0;
        return count > 0 ? <button key={stage.key} tabIndex={-1} title={`${stage.title}: ${count}`} onClick={() => onChange(stage.key)}
          className={`h-full border-r border-surface/60 transition-opacity duration-brand last:border-r-0 hover:opacity-80 ${toneOf(stage.key).bar} ${value !== "all" && value !== stage.key ? "opacity-35" : ""}`}
          style={{ width: `${(100 * count) / active}%` }} /> : null;
      })}
    </div>
    <div className="-mx-1">
      <div role="tablist" aria-label="Etapas de la campaña" className="flex flex-wrap gap-0.5 px-1">
        {tabs.map((tab, index) => {
          const selected = value === tab.key;
          return <button key={tab.key} type="button" role="tab" aria-selected={selected} tabIndex={selected ? 0 : -1}
            title={tab.title} onClick={() => onChange(tab.key)} onKeyDown={(event) => move(event, index)}
            className={`group flex items-center gap-1.5 rounded-xl px-2 py-1.5 text-sm font-medium transition duration-brand focus-visible:outline focus-visible:outline-2 focus-visible:outline-brand ${selected ? "bg-white/[0.09] text-white ring-1 ring-white/15" : tab.count ? "text-ink-secondary hover:bg-white/[0.05] hover:text-white" : "text-ink-secondary/60 hover:bg-white/[0.04]"}`}>
            {tab.tone && <span aria-hidden="true" className={`h-2 w-2 rounded-full ${tab.tone.dot} ${tab.count ? "" : "opacity-30"}`} />}
            <span>{tab.label}</span>
            <span className={`rounded-md px-1 py-0.5 text-xs tabular-nums ${selected ? "bg-white/10 text-white" : tab.human && tab.count ? "bg-white/[0.07] text-white" : "text-ink-secondary"}`}>
              {loading ? "—" : tab.count}
            </span>
            {!loading && badges[tab.key] > 0 && <span title={`${badges[tab.key]} ${badges[tab.key] === 1 ? "lista para reenviar" : "listas para reenviar"} al portal`}
              className="rounded-md bg-amber-400/15 px-1 py-0.5 text-xs tabular-nums text-amber-200">
              <span aria-hidden="true">↻ {badges[tab.key]}</span><span className="sr-only"> · {badges[tab.key]} por reenviar</span>
            </span>}
          </button>;
        })}
      </div>
    </div>
  </div>;
}
