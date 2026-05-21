import { useNavigate } from "react-router-dom";
import { useI18n } from "../../i18n";
import PageHero from "./PageHero";

// For Artists page: cinematic hero + light zone (features + outputs) + dark
// examples + CTA — same dark/light rhythm as Home.
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
    { title: "Lyric Video", desc: "1920 × 1080 · Full HD", shape: "aspect-video", tag: "16:9" },
    { title: "YouTube Short", desc: "1080 × 1920 · Vertical", shape: "aspect-[9/16] max-h-44", tag: "9:16" },
    { title: "Thumbnail", desc: "1280 × 720 · Portada", shape: "aspect-video", tag: "JPG" },
  ];

  return (
    <>
      <PageHero
        eyebrow={t("nav.for_artists")}
        title={t("home.aud_artists_t")}
        sub={t("home.aud_artists_d")}
        ctaLabel={t("landing.cta")}
        bg="/samples/looks/look-studio.png"
      />

      {/* LIGHT zone: features + outputs */}
      <div className="relative z-10 bg-white text-gray-900">
        <section className="max-w-5xl mx-auto px-6 py-24">
          <h2 className="text-3xl font-extrabold text-center mb-12">{t("landing.features")}</h2>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-5">
            {feats.map((f) => (
              <div key={f.t} className="rounded-card p-6 bg-gray-50 ring-1 ring-gray-200">
                <h3 className="font-semibold mb-2 text-gray-900">{f.t}</h3>
                <p className="text-sm text-gray-500 leading-relaxed">{f.d}</p>
              </div>
            ))}
          </div>
        </section>
        <section className="border-t border-gray-200 bg-gray-50">
          <div className="max-w-5xl mx-auto px-6 py-24">
            <h2 className="text-3xl font-extrabold text-center mb-4">{t("landing.outputs_title")}</h2>
            <p className="text-gray-500 text-center mb-12 max-w-md mx-auto">{t("landing.outputs_sub")}</p>
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-6">
              {outputs.map((o) => (
                <div key={o.title} className="rounded-2xl p-5 bg-white ring-1 ring-gray-200 flex flex-col items-center text-center">
                  <div className={`w-full ${o.shape} rounded-lg bg-gradient-to-br from-brand to-accent mb-4 flex items-center justify-center`}>
                    <span className="text-xs font-bold text-white/90 tracking-wider">{o.tag}</span>
                  </div>
                  <h3 className="font-semibold text-gray-900">{o.title}</h3>
                  <p className="text-xs text-gray-500 mt-1">{o.desc}</p>
                </div>
              ))}
            </div>
          </div>
        </section>
      </div>

      {/* DARK: real examples + CTA */}
      <section className="relative z-10 max-w-6xl mx-auto px-6 py-24">
        <h2 className="text-3xl font-extrabold text-center mb-12">{t("home.gallery_title")}</h2>
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
      </section>
    </>
  );
}
