// Stylized lyric-video tile: an atmospheric gradient "scene" + kinetic lyric
// typography + grain + vignette + equalizer. These are STYLE mockups (CSS, not
// real renders) used to convey the GenLy aesthetic on the landing — labeled
// honestly, never presented as specific real outputs.
const GRAIN =
  "url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='120' height='120'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='3' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E\")";

export default function LyricDemoTile({ line, img, scene, glow, delay = "0s", className = "" }) {
  return (
    <div className={`relative rounded-2xl overflow-hidden aspect-video group ${className}`}>
      {/* real AI-generated cinematic background (Imagen) — falls back to a CSS scene */}
      {img
        ? <img src={img} alt="" className="absolute inset-0 w-full h-full object-cover" />
        : <div className="absolute inset-0" style={{ background: scene }} />}
      {/* drifting light */}
      {glow && <div className="absolute -inset-1/3 opacity-50 animate-drift" style={{ background: glow, animationDelay: delay }} />}
      {/* film grain */}
      <div className="absolute inset-0 opacity-[0.13] mix-blend-overlay pointer-events-none" style={{ backgroundImage: GRAIN, backgroundSize: "180px 180px" }} />
      {/* vignette */}
      <div className="absolute inset-0" style={{ background: "radial-gradient(ellipse at 50% 45%, transparent 38%, rgba(0,0,0,.6))" }} />
      {/* kinetic lyric */}
      <div className="absolute inset-0 flex items-center justify-center px-5 text-center">
        <p className="font-extrabold tracking-tight text-white leading-[1.05] text-[clamp(1.05rem,2.6vw,1.8rem)] drop-shadow-[0_2px_24px_rgba(0,0,0,.8)] animate-fade-in" style={{ animationDelay: delay }}>
          {line}
        </p>
      </div>
      {/* equalizer */}
      <div className="absolute bottom-3 left-3 flex items-end gap-1 h-4">
        {[0, 0.2, 0.35, 0.12].map((d, i) => (
          <span key={i} className="w-[3px] rounded-full bg-white/80 animate-eq" style={{ animationDelay: `${d}s`, height: "5px" }} />
        ))}
      </div>
      {/* badge */}
      <span className="absolute bottom-3 right-3 text-[9px] font-semibold text-white/70 bg-black/40 backdrop-blur-sm px-2 py-0.5 rounded-full">GenLy</span>
    </div>
  );
}
