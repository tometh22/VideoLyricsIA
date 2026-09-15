const steps = [
  ["review", "1. Letras", "Corregir y aprobar letra y tiempos"],
  ["creative", "2. Generar videos", "Elegir canciones y usar el estilo guardado"],
  ["history", "3. Revisar videos", "Reproducir, corregir y aprobar"],
  ["deliveries", "4. Entregables", "Enviar aprobados y seguir el envío"],
];

export default function CampaignWorkflow({ view, onChange, disabled = false }) {
  return <nav aria-label="Secciones de campaña" className="space-y-3">
    <div className="grid grid-cols-2 gap-2 lg:grid-cols-4">{steps.map(([key, label, description]) =>
      <button key={key} disabled={disabled} onClick={() => onChange(key)} aria-current={view === key ? "page" : undefined}
        className={`rounded-xl p-3 text-left ring-1 disabled:opacity-50 ${view === key ? "bg-brand/15 text-brand-light ring-brand/50" : "bg-white/[0.03] text-ink-secondary ring-white/10 hover:bg-white/[0.07]"}`}>
        <strong className="block text-sm">{label}</strong><span className="mt-1 block text-xs text-ink-secondary">{description}</span>
      </button>)}</div>
    <button disabled={disabled} aria-current={view === "contract" ? "page" : undefined} onClick={() => onChange("contract")} className="text-xs text-ink-secondary underline decoration-white/20 underline-offset-4">Contrato y cumplimiento</button>
  </nav>;
}
