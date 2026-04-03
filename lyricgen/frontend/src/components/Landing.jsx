const FEATURES = [
  {
    title: "Lyrics automaticas",
    desc: "IA que transcribe y sincroniza las letras de cualquier cancion, en cualquier idioma.",
    icon: (
      <svg className="w-6 h-6" fill="none" stroke="currentColor" strokeWidth="1.5" viewBox="0 0 24 24">
        <path d="M12 18.5a6.5 6.5 0 006.5-6.5V6a6.5 6.5 0 10-13 0v6a6.5 6.5 0 006.5 6.5z" strokeLinecap="round" strokeLinejoin="round"/>
        <path d="M19 10v2a7 7 0 01-14 0v-2M12 18.5V22M8 22h8" strokeLinecap="round" strokeLinejoin="round"/>
      </svg>
    ),
  },
  {
    title: "Fondos unicos con IA",
    desc: "Cada video tiene un fondo cinematografico generado por IA que refleja el mood de la cancion.",
    icon: (
      <svg className="w-6 h-6" fill="none" stroke="currentColor" strokeWidth="1.5" viewBox="0 0 24 24">
        <rect x="2" y="2" width="20" height="20" rx="2.18" strokeLinecap="round" strokeLinejoin="round"/>
        <path d="M7 2v20M17 2v20M2 12h20M2 7h5M2 17h5M17 17h5M17 7h5" strokeLinecap="round" strokeLinejoin="round"/>
      </svg>
    ),
  },
  {
    title: "3 outputs por cancion",
    desc: "Lyric video Full HD, YouTube Short vertical y thumbnail listos para publicar.",
    icon: (
      <svg className="w-6 h-6" fill="none" stroke="currentColor" strokeWidth="1.5" viewBox="0 0 24 24">
        <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4M7 10l5 5 5-5M12 15V3" strokeLinecap="round" strokeLinejoin="round"/>
      </svg>
    ),
  },
  {
    title: "Batch processing",
    desc: "Subi 1 o 100 canciones. La plataforma las procesa todas automaticamente.",
    icon: (
      <svg className="w-6 h-6" fill="none" stroke="currentColor" strokeWidth="1.5" viewBox="0 0 24 24">
        <rect x="3" y="3" width="7" height="7" rx="1" /><rect x="14" y="3" width="7" height="7" rx="1" />
        <rect x="3" y="14" width="7" height="7" rx="1" /><rect x="14" y="14" width="7" height="7" rx="1" />
      </svg>
    ),
  },
  {
    title: "Control de calidad",
    desc: "Revision y edicion de lyrics con sugerencias inteligentes antes de generar.",
    icon: (
      <svg className="w-6 h-6" fill="none" stroke="currentColor" strokeWidth="1.5" viewBox="0 0 24 24">
        <path d="M9 11l3 3L22 4" strokeLinecap="round" strokeLinejoin="round"/>
        <path d="M21 12v7a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2h11" strokeLinecap="round" strokeLinejoin="round"/>
      </svg>
    ),
  },
  {
    title: "100% comercial",
    desc: "Todos los outputs son propiedad del cliente. Sin regalias, sin atribuciones.",
    icon: (
      <svg className="w-6 h-6" fill="none" stroke="currentColor" strokeWidth="1.5" viewBox="0 0 24 24">
        <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" strokeLinecap="round" strokeLinejoin="round"/>
      </svg>
    ),
  },
];

const STEPS = [
  { num: "01", title: "Subi los MP3", desc: "Arrastra uno o varios archivos con el nombre del artista e idioma." },
  { num: "02", title: "Revisa las lyrics", desc: "La IA transcribe las letras con sugerencias de correccion inteligentes." },
  { num: "03", title: "Descarga los videos", desc: "Lyric video, short y thumbnail listos para publicar en minutos." },
];

export default function Landing({ onStart }) {
  return (
    <div className="min-h-screen bg-surface relative overflow-hidden">
      {/* Ambient blobs */}
      <div className="fixed inset-0 pointer-events-none">
        <div className="absolute top-[-20%] left-[10%] w-[700px] h-[700px] bg-brand/[0.05] rounded-full blur-[150px]" />
        <div className="absolute bottom-[-10%] right-[5%] w-[500px] h-[500px] bg-brand-light/[0.04] rounded-full blur-[120px]" />
        <div className="absolute top-[40%] right-[20%] w-[300px] h-[300px] bg-accent/[0.03] rounded-full blur-[100px]" />
      </div>

      {/* Nav */}
      <nav className="relative z-10 flex items-center justify-between px-8 py-6 max-w-6xl mx-auto">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-brand to-brand-light flex items-center justify-center shadow-glow">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
              <path d="M9 18V5l12-2v13" /><circle cx="6" cy="18" r="3" /><circle cx="18" cy="16" r="3" />
            </svg>
          </div>
          <span className="text-xl font-bold tracking-tight">LyricGen</span>
          <span className="text-[10px] font-medium text-brand bg-brand/10 px-2 py-0.5 rounded-full ml-1 uppercase tracking-widest">Pro</span>
        </div>
        <button onClick={onStart} className="btn-primary text-sm py-2.5 px-6">
          Comenzar
        </button>
      </nav>

      {/* Hero */}
      <section className="relative z-10 pt-16 pb-8 px-6 max-w-6xl mx-auto">
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-12 items-center">
          {/* Left: text */}
          <div>
            <div className="inline-block px-4 py-1.5 rounded-full glass text-xs text-gray-400 mb-6 animate-fade-in">
              Plataforma de generacion de lyric videos con IA
            </div>
            <h1 className="text-5xl sm:text-6xl font-extrabold tracking-tight mb-6 leading-[1.1] animate-fade-in">
              <span className="bg-gradient-to-r from-white via-white to-gray-400 bg-clip-text text-transparent">
                De MP3 a
              </span>
              <br />
              <span className="bg-gradient-to-r from-brand to-brand-light bg-clip-text text-transparent">
                lyric video
              </span>
              <br />
              <span className="bg-gradient-to-r from-white via-white to-gray-400 bg-clip-text text-transparent">
                en minutos
              </span>
            </h1>
            <p className="text-gray-400 text-lg max-w-lg leading-relaxed mb-8 animate-fade-in">
              Subi un MP3 y genera automaticamente un lyric video Full HD, un YouTube Short y un thumbnail. Con fondos cinematograficos unicos generados por IA.
            </p>
            <div className="flex gap-4 animate-fade-in">
              <button onClick={onStart} className="btn-primary text-lg py-4 px-10">
                Empezar ahora
                <svg className="inline-block ml-2 w-5 h-5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M5 12h14M12 5l7 7-7 7" />
                </svg>
              </button>
            </div>
          </div>

          {/* Right: visual mockup */}
          <div className="relative animate-slide-up hidden lg:block">
            {/* Main video mockup */}
            <div className="glass rounded-3xl p-3 shadow-glow-lg">
              <div className="rounded-2xl overflow-hidden bg-gradient-to-br from-brand-dark/30 via-surface-2 to-brand/20 aspect-video relative">
                {/* Fake video player UI */}
                <div className="absolute inset-0 flex items-center justify-center">
                  <div className="text-center">
                    <p className="text-3xl font-extrabold text-white/90 uppercase tracking-wide mb-1" style={{textShadow: '0 2px 20px rgba(0,0,0,0.5)'}}>
                      I WANT IT, I GOT IT
                    </p>
                    <p className="text-xs text-white/40 mt-4">Lyric Video — 1920x1080</p>
                  </div>
                </div>
                {/* Play button */}
                <div className="absolute bottom-4 left-4 flex items-center gap-2">
                  <div className="w-8 h-8 rounded-full bg-white/10 backdrop-blur-sm flex items-center justify-center">
                    <svg className="w-3.5 h-3.5 text-white ml-0.5" fill="currentColor" viewBox="0 0 24 24">
                      <path d="M8 5v14l11-7z"/>
                    </svg>
                  </div>
                  <div className="h-1 w-32 bg-white/10 rounded-full overflow-hidden">
                    <div className="h-full w-2/3 bg-brand rounded-full"/>
                  </div>
                  <span className="text-[10px] text-white/40">2:34</span>
                </div>
              </div>
            </div>

            {/* Floating Short mockup */}
            <div className="absolute -bottom-6 -left-8 glass rounded-2xl p-2 shadow-glow w-28 animate-pulse-slow">
              <div className="rounded-xl bg-gradient-to-b from-pink-500/20 to-brand/20 aspect-[9/16] flex items-center justify-center">
                <div className="text-center px-2">
                  <p className="text-[8px] font-bold text-white/80 uppercase">SHORT</p>
                  <p className="text-[6px] text-white/40 mt-1">1080x1920</p>
                </div>
              </div>
            </div>

            {/* Floating Thumbnail mockup */}
            <div className="absolute -top-4 -right-6 glass rounded-2xl p-2 shadow-glow w-36">
              <div className="rounded-xl bg-gradient-to-br from-amber-500/20 to-orange-600/20 aspect-video flex items-center justify-center">
                <div className="text-center">
                  <p className="text-[9px] font-bold text-white/80">ARIANA GRANDE</p>
                  <p className="text-[7px] text-white/40 mt-0.5">7 rings</p>
                </div>
              </div>
            </div>
          </div>
        </div>

        {/* Stats */}
        <div className="flex justify-center gap-16 mt-20 pt-12 border-t border-white/[0.04]">
          {[
            { value: "< 5 min", label: "por video" },
            { value: "3", label: "outputs por cancion" },
            { value: "100%", label: "comercial" },
            { value: "6+", label: "idiomas" },
          ].map((s) => (
            <div key={s.label} className="text-center">
              <p className="text-3xl font-bold bg-gradient-to-r from-white to-gray-400 bg-clip-text text-transparent">{s.value}</p>
              <p className="text-xs text-gray-500 mt-1">{s.label}</p>
            </div>
          ))}
        </div>
      </section>

      {/* How it works */}
      <section className="relative z-10 py-24 px-6 max-w-5xl mx-auto">
        <h2 className="text-3xl font-bold text-center mb-4">Como funciona</h2>
        <p className="text-gray-500 text-center mb-16 max-w-md mx-auto">Tres pasos. Sin instalaciones. Sin complicaciones.</p>
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-10">
          {STEPS.map((step, i) => (
            <div key={step.num} className="relative text-center group">
              <div className="text-6xl font-extrabold text-brand/10 group-hover:text-brand/20 transition-colors mb-4">{step.num}</div>
              <h3 className="text-lg font-bold mb-3">{step.title}</h3>
              <p className="text-sm text-gray-400 leading-relaxed">{step.desc}</p>
              {i < 2 && (
                <div className="hidden sm:block absolute top-8 -right-6 text-gray-700">
                  <svg className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                    <path d="M5 12h14M12 5l7 7-7 7" strokeLinecap="round" strokeLinejoin="round"/>
                  </svg>
                </div>
              )}
            </div>
          ))}
        </div>
      </section>

      {/* Outputs showcase */}
      <section className="relative z-10 py-20 px-6 max-w-5xl mx-auto">
        <h2 className="text-3xl font-bold text-center mb-4">Un MP3, tres outputs</h2>
        <p className="text-gray-500 text-center mb-16 max-w-md mx-auto">Todo lo que necesitas para publicar en YouTube, listos para descargar.</p>
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-6">
          {[
            { title: "Lyric Video", res: "1920 x 1080", desc: "Full HD horizontal con lyrics sincronizadas", gradient: "from-brand/30 to-brand-dark/30", aspect: "aspect-video" },
            { title: "YouTube Short", res: "1080 x 1920", desc: "Vertical 30s del chorus para Shorts, Reels y TikTok", gradient: "from-pink-500/20 to-rose-600/30", aspect: "aspect-[9/16] max-h-52" },
            { title: "Thumbnail", res: "1280 x 720", desc: "Portada lista para YouTube con artista y cancion", gradient: "from-amber-500/20 to-orange-600/30", aspect: "aspect-video" },
          ].map((item) => (
            <div key={item.title} className="glass rounded-3xl p-5 text-center glass-hover">
              <div className={`rounded-2xl bg-gradient-to-br ${item.gradient} ${item.aspect} mx-auto mb-4 flex items-center justify-center`}>
                <div>
                  <p className="text-xs font-bold text-white/60">{item.res}</p>
                </div>
              </div>
              <h3 className="font-semibold mb-1">{item.title}</h3>
              <p className="text-xs text-gray-500">{item.desc}</p>
            </div>
          ))}
        </div>
      </section>

      {/* Features */}
      <section className="relative z-10 py-20 px-6 max-w-5xl mx-auto">
        <h2 className="text-3xl font-bold text-center mb-4">Caracteristicas</h2>
        <p className="text-gray-500 text-center mb-16 max-w-lg mx-auto">
          Todo lo que necesitas para producir lyric videos a escala, con calidad profesional.
        </p>
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-5">
          {FEATURES.map((f) => (
            <div key={f.title} className="glass rounded-2xl p-6 glass-hover">
              <div className="w-11 h-11 rounded-xl bg-brand/10 flex items-center justify-center text-brand mb-4">
                {f.icon}
              </div>
              <h3 className="font-semibold mb-2">{f.title}</h3>
              <p className="text-sm text-gray-400 leading-relaxed">{f.desc}</p>
            </div>
          ))}
        </div>
      </section>

      {/* CTA */}
      <section className="relative z-10 py-24 px-6 text-center">
        <div className="max-w-2xl mx-auto glass rounded-3xl p-12 shadow-glow-lg">
          <h2 className="text-3xl font-bold mb-4">Listo para empezar?</h2>
          <p className="text-gray-400 mb-8">
            Genera tu primer lyric video en minutos.
          </p>
          <button onClick={onStart} className="btn-primary text-lg py-4 px-10">
            Crear lyric video
            <svg className="inline-block ml-2 w-5 h-5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24" strokeLinecap="round" strokeLinejoin="round">
              <path d="M5 12h14M12 5l7 7-7 7" />
            </svg>
          </button>
        </div>
      </section>

      {/* Footer */}
      <footer className="relative z-10 border-t border-white/[0.04] py-8 px-8 text-center">
        <p className="text-xs text-gray-600">LyricGen Pro — Plataforma de generacion de lyric videos con inteligencia artificial</p>
      </footer>
    </div>
  );
}
