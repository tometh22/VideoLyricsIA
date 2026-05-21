import { useNavigate } from "react-router-dom";
import { useI18n } from "../../i18n";

// For Artists page. Stage 1: lean but real (header + key features + CTA).
// Stage 2 will move the full features/outputs/examples sections here.
export default function Artists() {
  const { t } = useI18n();
  const navigate = useNavigate();
  const feats = [
    { t: t("feat.lyrics"), d: t("feat.lyrics_desc") },
    { t: t("feat.backgrounds"), d: t("feat.backgrounds_desc") },
    { t: t("feat.outputs"), d: t("feat.outputs_desc") },
    { t: t("feat.youtube"), d: t("feat.youtube_desc") },
  ];
  return (
    <div className="px-6 pt-24 pb-28 max-w-5xl mx-auto">
      <div className="text-center max-w-2xl mx-auto mb-14">
        <span className="text-[11px] uppercase tracking-[0.25em] text-brand-light">{t("nav.for_artists")}</span>
        <h1 className="text-4xl sm:text-5xl font-extrabold tracking-tight mt-3 mb-4">{t("home.aud_artists_t")}</h1>
        <p className="text-gray-400 text-lg leading-relaxed mb-8">{t("home.aud_artists_d")}</p>
        <button onClick={() => navigate("/login")} className="btn-primary text-lg py-4 px-10">{t("landing.cta")}</button>
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-5">
        {feats.map((f) => (
          <div key={f.t} className="glass rounded-card p-6">
            <h3 className="font-semibold mb-2">{f.t}</h3>
            <p className="text-sm text-gray-400 leading-relaxed">{f.d}</p>
          </div>
        ))}
      </div>
    </div>
  );
}
