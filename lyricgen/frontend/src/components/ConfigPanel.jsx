export default function ConfigPanel({ artist, onArtist }) {
  return (
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
  );
}
