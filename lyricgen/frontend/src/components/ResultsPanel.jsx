const API = "";

const CARDS = [
  { type: "video", label: "Lyric Video", desc: "1920x1080 MP4", icon: "🎬" },
  { type: "short", label: "YouTube Short", desc: "1080x1920 30s", icon: "📱" },
  { type: "thumbnail", label: "Thumbnail", desc: "1280x720 JPG", icon: "🖼️" },
];

export default function ResultsPanel({ jobId, files, onReset }) {
  const downloadAll = () => {
    CARDS.forEach(({ type }) => {
      const a = document.createElement("a");
      a.href = `${API}/download/${jobId}/${type}`;
      a.download = "";
      a.click();
    });
  };

  return (
    <div className="w-full max-w-2xl mt-10 space-y-6">
      <h2 className="text-2xl font-bold text-center text-green-400">
        &#10003; Listo
      </h2>

      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        {CARDS.map((card) => (
          <div
            key={card.type}
            className="bg-surface-light rounded-2xl p-6 flex flex-col items-center gap-3"
          >
            <span className="text-3xl">{card.icon}</span>
            <p className="font-semibold">{card.label}</p>
            <p className="text-xs text-gray-500">{card.desc}</p>
            <a
              href={`${API}/download/${jobId}/${card.type}`}
              download
              className="mt-auto px-4 py-2 rounded-lg bg-brand hover:bg-brand/80 text-sm font-medium transition"
            >
              Descargar
            </a>
          </div>
        ))}
      </div>

      <div className="flex gap-4 justify-center">
        <button
          onClick={downloadAll}
          className="px-6 py-3 rounded-xl bg-brand hover:bg-brand/80 font-semibold transition"
        >
          Descargar todo
        </button>
        <button
          onClick={onReset}
          className="px-6 py-3 rounded-xl bg-surface-light border border-gray-600 hover:border-brand transition"
        >
          Nuevo video
        </button>
      </div>
    </div>
  );
}
