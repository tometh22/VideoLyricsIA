export default function ConfigPanel({ artist, onArtist }) {
  return (
    <div>
      <label className="block text-sm font-medium text-gray-400 mb-2 ml-1">
        Artista
      </label>
      <input
        type="text"
        value={artist}
        onChange={(e) => onArtist(e.target.value)}
        placeholder="Nombre del artista"
        className="input-field"
      />
    </div>
  );
}
