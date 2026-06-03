// Barra de filtros consistente. Compound component: los hijos especializados
// (Chips, Search, Toggle) cubren el 90% de los casos; children libres para
// el resto (selects, botones).
//
//   <FilterBar>
//     <FilterBar.Chips value={days} onChange={setDays}
//       options={[{ id: 7, label: "7d" }, { id: 30, label: "30d" }]} label="Período" />
//     <FilterBar.Toggle checked={hide} onChange={setHide} label="Ocultar inactivos" />
//     <FilterBar.Search value={q} onChange={setQ} onSubmit={search} />
//   </FilterBar>

function FilterBar({ children, className = "" }) {
  return (
    <div className={`glass rounded-card p-4 flex items-center gap-4 flex-wrap ${className}`}>
      {children}
    </div>
  );
}

function Chips({ value, onChange, options, label }) {
  return (
    <div className="flex items-center gap-2">
      {label && <span className="text-section uppercase text-gray-500">{label}</span>}
      {options.map((opt) => (
        <button
          key={opt.id}
          type="button"
          onClick={() => onChange(opt.id)}
          className={`px-3 py-1 rounded-md text-caption ring-1 transition-colors duration-brand ${
            value === opt.id
              ? "bg-brand/20 ring-brand/40 text-white"
              : "ring-white/[0.06] text-gray-400 hover:text-white"
          }`}
        >
          {opt.label}
          {opt.badge > 0 && (
            <span className="ml-1.5 text-section font-bold text-amber-300">{opt.badge}</span>
          )}
        </button>
      ))}
    </div>
  );
}

function Search({ value, onChange, onSubmit, placeholder = "Buscar…" }) {
  return (
    <input
      type="text"
      value={value}
      onChange={(e) => onChange(e.target.value)}
      onKeyDown={(e) => { if (e.key === "Enter" && onSubmit) onSubmit(); }}
      placeholder={placeholder}
      className="bg-surface-3/40 ring-1 ring-white/[0.06] focus:ring-brand/40 focus:outline-none rounded-md px-3 py-1.5 text-caption text-white placeholder:text-gray-600 w-56"
    />
  );
}

function Toggle({ checked, onChange, label }) {
  return (
    <label className="flex items-center gap-1.5 text-caption text-gray-400 cursor-pointer select-none">
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        className="accent-brand"
      />
      {label}
    </label>
  );
}

function Select({ value, onChange, options, label }) {
  return (
    <div className="flex items-center gap-2">
      {label && <span className="text-section uppercase text-gray-500">{label}</span>}
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="bg-surface-3/40 ring-1 ring-white/[0.06] focus:ring-brand/40 focus:outline-none rounded-md px-2 py-1.5 text-caption text-white"
      >
        {options.map((opt) => (
          <option key={opt.id} value={opt.id}>{opt.label}</option>
        ))}
      </select>
    </div>
  );
}

FilterBar.Chips = Chips;
FilterBar.Search = Search;
FilterBar.Toggle = Toggle;
FilterBar.Select = Select;

export default FilterBar;
