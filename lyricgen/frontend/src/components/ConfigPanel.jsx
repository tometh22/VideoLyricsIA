const STYLES = [
  { id: "oscuro", label: "Oscuro", color: "#1a1a2e" },
  { id: "neon", label: "Neon", color: "#0ff0fc" },
  { id: "minimal", label: "Minimal", color: "#e0e0e0" },
  { id: "calido", label: "Cálido", color: "#f5a623" },
];

export default function ConfigPanel({ artist, onArtist, style, onStyle }) {
  return (
    <div className="space-y-4">
      <div>
        <label className="block text-sm text-gray-400 mb-1">Nombre del artista</label>
        <input
          type="text"
          value={artist}
          onChange={(e) => onArtist(e.target.value)}
          placeholder="Ej: Bad Bunny"
          className="w-full px-4 py-3 rounded-xl bg-surface-light border border-gray-700
            focus:border-brand focus:outline-none text-white placeholder-gray-500"
        />
      </div>

      <div>
        <label className="block text-sm text-gray-400 mb-2">Estilo visual</label>
        <div className="grid grid-cols-4 gap-3">
          {STYLES.map((s) => (
            <button
              key={s.id}
              onClick={() => onStyle(s.id)}
              className={`flex flex-col items-center gap-2 p-3 rounded-xl border transition
                ${style === s.id
                  ? "border-brand bg-brand/10"
                  : "border-gray-700 bg-surface-light hover:border-gray-500"
                }`}
            >
              <div
                className="w-8 h-8 rounded-full border border-gray-600"
                style={{ backgroundColor: s.color }}
              />
              <span className="text-xs text-gray-300">{s.label}</span>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
