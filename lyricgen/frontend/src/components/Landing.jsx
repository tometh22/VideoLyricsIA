import { Link, useNavigate } from "react-router-dom";
import { useI18n } from "../i18n";
import SocialProofWall from "./SocialProofWall";
import Testimonials from "./Testimonials";

// Home page — a tight funnel. Rendered inside MarketingLayout (nav + announcement
// bar + footer live there). Deep content lives on the sub-pages: features/outputs
// → /artistas, comparison + FAQ → /precios, enterprise + lead form → /sellos,
// shipped updates → /novedades. Keep this page short and punchy.
export default function Landing() {
  const { t } = useI18n();
  const navigate = useNavigate();
  const onStart = () => navigate("/login");

  return (
    <>
      {/* Hero — kinetic lyric typography over a neon stage. */}
      <section className="relative min-h-screen w-full overflow-hidden flex flex-col">
        <div className="absolute inset-0 overflow-hidden pointer-events-none">
          <div className="absolute -top-[20%] -left-[10%] w-[60vw] h-[60vw] rounded-full bg-brand/40 blur-[130px] animate-drift" />
          <div className="absolute -bottom-[15%] -right-[8%] w-[48vw] h-[48vw] rounded-full bg-accent/30 blur-[130px] animate-drift" style={{ animationDirection: "reverse", animationDuration: "20s" }} />
          <div className="absolute inset-0" style={{ background: "radial-gradient(ellipse at 50% 38%, rgba(109,74,255,.16), transparent 60%)" }} />
          <div className="absolute inset-0 opacity-[0.05]" style={{ backgroundImage: "linear-gradient(#fff 1px,transparent 1px),linear-gradient(90deg,#fff 1px,transparent 1px)", backgroundSize: "64px 64px" }} />
        </div>

        <div className="relative z-20 flex-1 flex flex-col items-center justify-center text-center px-6 pt-24 pb-16">
          <div className="flex items-center gap-1.5 mb-8 h-6 animate-fade-in">
            {[0, 0.2, 0.4, 0.1, 0.3].map((d, i) => (
              <span key={i} className="w-[3px] rounded-full bg-accent animate-eq" style={{ animationDelay: `${d}s`, height: "6px" }} />
            ))}
            <span className="ml-3 text-[11px] uppercase tracking-[0.25em] text-white/40">{t("landing.badge")}</span>
          </div>

          <h1 className="font-extrabold tracking-tight leading-[0.95] text-[clamp(2.6rem,8vw,6rem)] max-w-5xl">
            <span className="block text-white animate-word-in" style={{ animationDelay: "0.1s" }}>{t("landing.hero1")}</span>
            <span className="block bg-gradient-to-r from-brand-light to-accent bg-clip-text text-transparent animate-word-in" style={{ animationDelay: "0.45s" }}>{t("landing.hero2")}</span>
          </h1>

          <p className="text-white/60 text-lg sm:text-xl max-w-2xl mx-auto leading-relaxed mt-8 mb-9 animate-slide-up">
            {t("landing.hero_sub")}
          </p>

          <div className="flex flex-col sm:flex-row gap-3 justify-center items-center animate-slide-up">
            <button onClick={onStart} className="px-9 py-4 rounded-full bg-white text-black font-bold text-lg hover:scale-[1.03] transition-transform shadow-glow-lg">
              {t("landing.cta")}
              <svg className="inline-block ml-2 w-5 h-5" fill="none" stroke="currentColor" strokeWidth="2.5" viewBox="0 0 24 24" strokeLinecap="round" strokeLinejoin="round"><path d="M5 12h14M12 5l7 7-7 7" /></svg>
            </button>
            <a href="#examples" className="px-7 py-4 rounded-full border border-white/20 text-white/80 text-lg hover:bg-white/5 transition-colors inline-flex items-center justify-center">
              {t("landing.cta_demo")}
            </a>
          </div>

          <span className="mt-7 inline-flex items-center gap-1.5 text-[11px] text-white/50 bg-black/30 backdrop-blur-sm px-3 py-1.5 rounded-full animate-fade-in">
            <svg className="w-3 h-3 text-accent" fill="currentColor" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>
            {t("landing.hero_trust")}
          </span>
        </div>

        <div className="relative z-20 pb-6 flex justify-center text-white/30 animate-bounce">
          <svg className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24" strokeLinecap="round" strokeLinejoin="round"><path d="M12 5v14M5 12l7 7 7-7" /></svg>
        </div>
      </section>

      {/* Stats — full-bleed band */}
      <section className="relative z-10 border-y border-white/[0.06] bg-white/[0.015]">
        <div className="grid grid-cols-2 sm:grid-cols-4 max-w-6xl mx-auto sm:divide-x divide-white/[0.06]">
          {[
            { value: "< 5 min", label: t("landing.per_video") },
            { value: "3", label: t("landing.outputs") },
            { value: "100%", label: t("landing.commercial") },
            { value: "6+", label: t("landing.languages") },
          ].map((s) => (
            <div key={s.label} className="text-center py-10 px-4">
              <p className="text-4xl font-bold bg-gradient-to-r from-white to-gray-400 bg-clip-text text-transparent">{s.value}</p>
              <p className="text-xs text-gray-500 mt-1">{s.label}</p>
            </div>
          ))}
        </div>
      </section>

      {/* Audience routing — send artists and labels to their own page */}
      <section className="relative z-10 py-16 px-6 max-w-5xl mx-auto">
        <div className="grid sm:grid-cols-2 gap-5">
          {[
            { to: "/artistas", t: t("home.aud_artists_t"), d: t("home.aud_artists_d") },
            { to: "/sellos", t: t("home.aud_labels_t"), d: t("home.aud_labels_d") },
          ].map((a) => (
            <Link key={a.to} to={a.to} className="glass rounded-3xl p-8 glass-hover group flex flex-col">
              <h3 className="text-xl font-bold mb-2">{a.t}</h3>
              <p className="text-sm text-gray-400 leading-relaxed flex-1">{a.d}</p>
              <span className="mt-4 inline-flex items-center gap-1.5 text-sm font-medium text-brand-light group-hover:gap-2.5 transition-all">
                {t("home.aud_cta")}
                <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24" strokeLinecap="round" strokeLinejoin="round"><path d="M5 12h14M12 5l7 7-7 7" /></svg>
              </span>
            </Link>
          ))}
        </div>
      </section>

      {/* Examples — real generated clips (the most sellable thing) */}
      <section id="examples" className="relative z-10 py-16 px-6 max-w-5xl mx-auto scroll-mt-24">
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
      </section>

      {/* Why GenLy — differentiation + trust strip */}
      <section className="relative z-10 py-16 px-6 max-w-5xl mx-auto">
        <h2 className="text-3xl font-bold text-center mb-4">{t("landing.why_title")}</h2>
        <p className="text-gray-500 text-center mb-14 max-w-md mx-auto">{t("landing.why_sub")}</p>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-5">
          {[
            { t: t("landing.why1_t"), d: t("landing.why1_d") },
            { t: t("landing.why2_t"), d: t("landing.why2_d") },
            { t: t("landing.why3_t"), d: t("landing.why3_d") },
            { t: t("landing.why4_t"), d: t("landing.why4_d") },
          ].map((item) => (
            <div key={item.t} className="glass rounded-card p-6 flex gap-4 items-start glass-hover">
              <div className="w-9 h-9 shrink-0 rounded-lg bg-brand/15 flex items-center justify-center text-brand">
                <svg className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth="2.5" viewBox="0 0 24 24" strokeLinecap="round" strokeLinejoin="round"><path d="M20 6L9 17l-5-5" /></svg>
              </div>
              <div>
                <h3 className="font-semibold mb-1.5">{item.t}</h3>
                <p className="text-sm text-gray-400 leading-relaxed">{item.d}</p>
              </div>
            </div>
          ))}
        </div>
        <div className="flex flex-wrap justify-center gap-x-8 gap-y-3 mt-12 pt-8 border-t border-white/[0.04]">
          {[t("landing.trust_sla"), t("landing.trust_owned"), t("landing.trust_isolation"), t("landing.trust_noroyalty")].map((label) => (
            <div key={label} className="flex items-center gap-2 text-xs text-gray-400">
              <svg className="w-4 h-4 text-accent" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24" strokeLinecap="round" strokeLinejoin="round"><path d="M9 12l2 2 4-4" /><circle cx="12" cy="12" r="10" /></svg>
              {label}
            </div>
          ))}
        </div>
      </section>

      {/* Customer logos + testimonials — null until real assets are provided */}
      <SocialProofWall title="Confían en GenLy" />
      <Testimonials title="Lo que dicen los artistas" />

      {/* Final CTA */}
      <section className="relative z-10 py-24 px-6 text-center">
        <div className="max-w-2xl mx-auto glass rounded-3xl p-12 shadow-glow-lg">
          <h2 className="text-3xl font-bold mb-4">{t("landing.ready")}</h2>
          <p className="text-gray-400 mb-8">{t("landing.ready_sub")}</p>
          <button onClick={onStart} className="btn-primary text-lg py-4 px-10">
            {t("landing.create")}
            <svg className="inline-block ml-2 w-5 h-5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24" strokeLinecap="round" strokeLinejoin="round"><path d="M5 12h14M12 5l7 7-7 7" /></svg>
          </button>
        </div>
      </section>
    </>
  );
}
