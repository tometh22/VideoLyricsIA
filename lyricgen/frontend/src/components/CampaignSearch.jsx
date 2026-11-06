export default function CampaignSearch({ value, onChange, label = "Buscar canción o artista", placeholder = "Canción, artista, código o archivo", className = "" }) {
  return <div role="search" className={`relative ${className}`}>
    <input type="search" aria-label={label} placeholder={placeholder} value={value}
      onChange={event => onChange(event.target.value)}
      onKeyDown={event => { if (event.key === "Escape" && value) { event.preventDefault(); onChange(""); } }}
      className="w-full rounded-xl bg-black/25 py-2.5 pl-3 pr-20 text-sm text-white ring-1 ring-white/15 outline-none focus:ring-brand/60" />
    {value && <button type="button" aria-label={`Limpiar ${label.toLowerCase()}`} onClick={() => onChange("")}
      className="absolute inset-y-1 right-1 rounded-lg px-3 text-xs text-ink-secondary hover:bg-white/10">Limpiar</button>}
  </div>;
}
