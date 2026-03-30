const API = "";

const CARDS = [
  {
    type: "video",
    label: "Lyric Video",
    desc: "Full HD 1920x1080",
    gradient: "from-brand/20 to-brand-dark/20",
    icon: (
      <svg className="w-7 h-7" fill="none" stroke="currentColor" strokeWidth="1.5" viewBox="0 0 24 24" strokeLinecap="round" strokeLinejoin="round">
        <rect x="2" y="2" width="20" height="20" rx="2.18" /><path d="M7 2v20M17 2v20M2 12h20M2 7h5M2 17h5M17 17h5M17 7h5" />
      </svg>
    ),
  },
  {
    type: "short",
    label: "YouTube Short",
    desc: "Vertical 1080x1920 / 30s",
    gradient: "from-pink-500/20 to-rose-600/20",
    icon: (
      <svg className="w-7 h-7" fill="none" stroke="currentColor" strokeWidth="1.5" viewBox="0 0 24 24" strokeLinecap="round" strokeLinejoin="round">
        <rect x="5" y="2" width="14" height="20" rx="2" /><line x1="12" y1="18" x2="12.01" y2="18" />
      </svg>
    ),
  },
  {
    type: "thumbnail",
    label: "Thumbnail",
    desc: "1280x720 JPG",
    gradient: "from-amber-500/20 to-orange-600/20",
    icon: (
      <svg className="w-7 h-7" fill="none" stroke="currentColor" strokeWidth="1.5" viewBox="0 0 24 24" strokeLinecap="round" strokeLinejoin="round">
        <rect x="3" y="3" width="18" height="18" rx="2" /><circle cx="8.5" cy="8.5" r="1.5" /><polyline points="21 15 16 10 5 21" />
      </svg>
    ),
  },
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
    <div className="w-full max-w-2xl mt-16 animate-fade-in">
      {/* Success header */}
      <div className="text-center mb-10">
        <div className="w-16 h-16 mx-auto mb-5 rounded-3xl bg-accent/10 flex items-center justify-center">
          <svg className="w-8 h-8 text-accent" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
            <polyline points="20 6 9 17 4 12" />
          </svg>
        </div>
        <h2 className="text-3xl font-bold mb-2">Contenido listo</h2>
        <p className="text-gray-500">Tus archivos fueron generados exitosamente</p>
      </div>

      {/* Cards */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4 mb-8">
        {CARDS.map((card) => (
          <a
            key={card.type}
            href={`${API}/download/${jobId}/${card.type}`}
            download
            className="group glass rounded-3xl p-6 flex flex-col items-center gap-4 text-center
              hover:border-white/[0.1] hover:shadow-glow transition-all duration-300"
          >
            <div className={`w-14 h-14 rounded-2xl bg-gradient-to-br ${card.gradient}
              flex items-center justify-center text-gray-300 group-hover:text-white transition-colors`}>
              {card.icon}
            </div>
            <div>
              <p className="font-semibold text-white mb-1">{card.label}</p>
              <p className="text-xs text-gray-500">{card.desc}</p>
            </div>
            <span className="mt-auto text-xs font-medium text-brand group-hover:text-brand-light transition-colors flex items-center gap-1.5">
              <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4" /><polyline points="7 10 12 15 17 10" /><line x1="12" y1="15" x2="12" y2="3" />
              </svg>
              Descargar
            </span>
          </a>
        ))}
      </div>

      {/* Actions */}
      <div className="flex gap-3 justify-center">
        <button onClick={downloadAll} className="btn-primary">
          <svg className="inline-block w-4 h-4 mr-2 -mt-0.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
            <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4" /><polyline points="7 10 12 15 17 10" /><line x1="12" y1="15" x2="12" y2="3" />
          </svg>
          Descargar todo
        </button>
        <button onClick={onReset} className="btn-secondary">
          Nuevo video
        </button>
      </div>
    </div>
  );
}
