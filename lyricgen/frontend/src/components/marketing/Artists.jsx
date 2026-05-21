import { useNavigate } from "react-router-dom";
import { useI18n } from "../../i18n";

// For Artists page: features + the 3 outputs + real examples + CTA.
export default function Artists() {
  const { t } = useI18n();
  const navigate = useNavigate();

  const feats = [
    { t: t("feat.lyrics"), d: t("feat.lyrics_desc") },
    { t: t("feat.backgrounds"), d: t("feat.backgrounds_desc") },
    { t: t("feat.outputs"), d: t("feat.outputs_desc") },
    { t: t("feat.youtube"), d: t("feat.youtube_desc") },
  ];
  const outputs = [
    { title: "Lyric Video", res: "1920 × 1080", desc: "Full HD", gradient: "from-brand/30 to-brand-dark/30", aspect: "aspect-video", img: null },
    { title: "YouTube Short", res: "1080 × 1920", desc: "Vertical 30s", gradient: "from-pink-500/20 to-rose-600/30", aspect: "aspect-[9/16] max-h-52", img: null },
    { title: "Thumbnail", res: "1280 × 720", desc: "Portada", gradient: "from-amber-500/20 to-orange-600/30", aspect: "aspect-video", img: null },
  ];

  return (
    <div className="px-6 pt-24 pb-28 max-w-5xl mx-auto">
      {/* Header */}
      <div className="text-center max-w-2xl mx-auto mb-14">
        <span className="text-[11px] uppercase tracking-[0.25em] text-brand-light">{t("nav.for_artists")}</span>
        <h1 className="text-4xl sm:text-5xl font-extrabold tracking-tight mt-3 mb-4">{t("home.aud_artists_t")}</h1>
        <p className="text-gray-400 text-lg leading-relaxed mb-8">{t("home.aud_artists_d")}</p>
        <button onClick={() => navigate("/login")} className="btn-primary text-lg py-4 px-10">{t("landing.cta")}</button>
      </div>

      {/* Features */}
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-5">
        {feats.map((f) => (
          <div key={f.t} className="glass rounded-card p-6">
            <h3 className="font-semibold mb-2">{f.t}</h3>
            <p className="text-sm text-gray-400 leading-relaxed">{f.d}</p>
          </div>
        ))}
      </div>

      {/* Outputs */}
      <div className="mt-24">
        <h2 className="text-3xl font-bold text-center mb-4">{t("landing.outputs_title")}</h2>
        <p className="text-gray-500 text-center mb-12 max-w-md mx-auto">{t("landing.outputs_sub")}</p>
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-6">
          {outputs.map((item) => (
            <div key={item.title} className="glass rounded-3xl p-5 text-center glass-hover">
              <div className={`rounded-2xl overflow-hidden bg-gradient-to-br ${item.gradient} ${item.aspect} mx-auto mb-4 flex items-center justify-center`}>
                {item.img ? <img src={item.img} alt={item.title} className="w-full h-full object-cover" /> : <p className="text-xs font-bold text-white/60">{item.res}</p>}
              </div>
              <h3 className="font-semibold mb-1">{item.title}</h3>
              <p className="text-xs text-gray-500">{item.desc}</p>
            </div>
          ))}
        </div>
      </div>

      {/* Examples */}
      <div className="mt-24">
        <h2 className="text-3xl font-bold text-center mb-4">{t("landing.examples_title")}</h2>
        <p className="text-gray-500 text-center mb-12 max-w-md mx-auto">{t("landing.examples_sub")}</p>
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-5">
          {["/samples/ex1.mp4", "/samples/ex2.mp4", "/samples/ex3.mp4"].map((src) => (
            <div key={src} className="glass rounded-2xl p-2 glass-hover">
              <div className="rounded-xl overflow-hidden aspect-video bg-black">
                <video autoPlay muted loop playsInline className="w-full h-full object-cover" src={src} />
              </div>
            </div>
          ))}
        </div>
        <div className="text-center mt-12">
          <button onClick={() => navigate("/login")} className="btn-primary text-lg py-4 px-10">{t("landing.cta")}</button>
        </div>
      </div>
    </div>
  );
}
