import { Link, useNavigate } from "react-router-dom";
import { useI18n } from "../i18n";
import SocialProofWall from "./SocialProofWall";
import Testimonials from "./Testimonials";

// Home page — a tight, wide, image-forward funnel. Rendered inside
// MarketingLayout (nav + announcement bar + footer live there). Deep content
// lives on the sub-pages. Sections alternate full-bleed bands + big visuals to
// fill the viewport (no narrow column floating in black).
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
            <a href="#showcase" className="px-7 py-4 rounded-full border border-white/20 text-white/80 text-lg hover:bg-white/5 transition-colors inline-flex items-center justify-center">
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

      {/* Formats — what you get, visual cards (Rotor-style) */}
      <section className="relative z-10 max-w-6xl mx-auto px-6 py-24">
        <h2 className="text-3xl sm:text-4xl font-extrabold text-center mb-3">{t("home.formats_title")}</h2>
        <p className="text-gray-500 text-center mb-12 max-w-md mx-auto">{t("home.formats_sub")}</p>
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-5">
          {[
            { title: "Lyric Video", desc: "1920×1080 · Full HD", img: "/samples/sample-reef.png", aspect: "aspect-video" },
            { title: "YouTube Short", desc: "1080×1920 · Vertical", img: "/samples/sample-forest.png", aspect: "aspect-[3/4]" },
            { title: "Thumbnail", desc: "1280×720", img: "/samples/sample-forest.png", aspect: "aspect-video" },
            { title: t("home.fmt_prores_t"), desc: t("home.fmt_prores_d"), img: null, aspect: "aspect-video" },
          ].map((f) => (
            <div key={f.title} className="glass rounded-2xl p-3 glass-hover">
              <div className={`rounded-xl overflow-hidden ${f.aspect} bg-gradient-to-br from-brand/25 to-accent/15 mb-3 flex items-center justify-center`}>
                {f.img ? <img src={f.img} alt={f.title} className="w-full h-full object-cover" /> : <span className="text-sm font-bold text-white/70">ProRes</span>}
              </div>
              <h3 className="font-semibold text-sm">{f.title}</h3>
              <p className="text-xs text-gray-500">{f.desc}</p>
            </div>
          ))}
        </div>
      </section>

      {/* Showcase — big lyric video in action, text left / video right */}
      <section id="showcase" className="relative z-10 scroll-mt-24 border-b border-white/[0.06] bg-gradient-to-b from-brand/[0.06] to-transparent">
        <div className="max-w-6xl mx-auto px-6 py-24 grid lg:grid-cols-2 gap-12 items-center">
          <div>
            <span className="text-[11px] uppercase tracking-[0.25em] text-brand-light">{t("home.show_label")}</span>
            <h2 className="text-3xl sm:text-4xl font-extrabold tracking-tight mt-3 mb-5 leading-[1.05]">{t("home.show_title")}</h2>
            <p className="text-gray-400 text-lg leading-relaxed mb-7">{t("landing.examples_sub")}</p>
            <ul className="space-y-3 mb-8">
              {[t("home.show_b1"), t("home.show_b2"), t("home.show_b3")].map((b) => (
                <li key={b} className="flex gap-3 items-start text-sm text-gray-300">
                  <svg className="w-5 h-5 shrink-0 text-accent mt-0.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24" strokeLinecap="round" strokeLinejoin="round"><path d="M9 12l2 2 4-4" /><circle cx="12" cy="12" r="10" /></svg>
                  {b}
                </li>
              ))}
            </ul>
            <button onClick={onStart} className="btn-primary text-lg py-4 px-9">{t("landing.cta")}</button>
          </div>
          <div className="relative">
            <div className="absolute -inset-4 bg-brand/20 blur-3xl rounded-full pointer-events-none" />
            <div className="relative glass rounded-3xl p-3 shadow-glow-lg">
              <div className="rounded-2xl overflow-hidden aspect-video bg-black">
                <video autoPlay muted loop playsInline className="w-full h-full object-cover" src="/samples/ex1.mp4" />
              </div>
            </div>
          </div>
        </div>
      </section>

      {/* Examples gallery — wide, big */}
      <section className="relative z-10 max-w-6xl mx-auto px-6 py-24">
        <h2 className="text-3xl font-bold mb-3">{t("home.gallery_title")}</h2>
        <p className="text-gray-500 mb-10 max-w-md">{t("landing.examples_sub")}</p>
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-6">
          {["/samples/ex2.mp4", "/samples/ex3.mp4", "/samples/ex1.mp4"].map((src) => (
            <div key={src} className="glass rounded-2xl p-2 glass-hover">
              <div className="rounded-xl overflow-hidden aspect-video bg-black">
                <video autoPlay muted loop playsInline className="w-full h-full object-cover" src={src} />
              </div>
            </div>
          ))}
        </div>
      </section>

      {/* Movement styles — variety showcase (Rotor "styles/filters") with real assets */}
      <section className="relative z-10 max-w-6xl mx-auto px-6 py-24">
        <h2 className="text-3xl sm:text-4xl font-extrabold text-center mb-3">{t("home.styles_title")}</h2>
        <p className="text-gray-500 text-center mb-12 max-w-lg mx-auto">{t("home.styles_sub")}</p>
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-5">
          {[
            { src: "/movement_samples/sutil.mp4", label: t("home.style_sutil") },
            { src: "/movement_samples/estandar.mp4", label: t("home.style_estandar") },
            { src: "/movement_samples/foto-parallax.mp4", label: t("home.style_parallax") },
            { src: "/movement_samples/animado.mp4", label: t("home.style_animado") },
          ].map((s) => (
            <div key={s.src} className="glass rounded-2xl p-2 glass-hover">
              <div className="rounded-xl overflow-hidden aspect-video bg-black relative">
                <video autoPlay muted loop playsInline className="w-full h-full object-cover" src={s.src} />
                <span className="absolute bottom-2 left-2 text-[11px] font-semibold text-white bg-black/55 backdrop-blur-sm px-2 py-1 rounded-full">{s.label}</span>
              </div>
            </div>
          ))}
        </div>
      </section>

      {/* How it works — "just add your music" (Rotor-style) */}
      <section className="relative z-10 border-y border-white/[0.06] bg-white/[0.015]">
        <div className="max-w-6xl mx-auto px-6 py-24">
          <h2 className="text-3xl sm:text-4xl font-extrabold text-center mb-3">{t("home.how_title")}</h2>
          <p className="text-gray-500 text-center mb-12 max-w-md mx-auto">{t("home.how_sub")}</p>

          {/* Visual input → output equation (Rotor-style) */}
          <div className="flex flex-col sm:flex-row items-center justify-center gap-4 mb-16">
            <div className="glass rounded-2xl px-6 py-5 flex items-center gap-3 w-full sm:w-auto justify-center">
              <svg className="w-5 h-5 text-white/70" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24" strokeLinecap="round" strokeLinejoin="round"><path d="M9 18V5l12-2v13" /><circle cx="6" cy="18" r="3" /><circle cx="18" cy="16" r="3" /></svg>
              <span className="text-sm font-medium">{t("home.eq_in")}</span>
            </div>
            <svg className="w-5 h-5 text-brand-light rotate-90 sm:rotate-0 shrink-0" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24" strokeLinecap="round" strokeLinejoin="round"><path d="M5 12h14M12 5l7 7-7 7" /></svg>
            <div className="rounded-2xl px-6 py-5 bg-gradient-to-r from-brand/30 to-accent/20 ring-1 ring-brand/30 flex items-center gap-3 w-full sm:w-auto justify-center">
              <svg className="w-5 h-5 text-accent" fill="currentColor" viewBox="0 0 24 24"><path d="M11.5 2l1.6 5.3L18.5 9l-5.4 1.7L11.5 16l-1.6-5.3L4.5 9l5.4-1.7z"/></svg>
              <span className="text-sm font-semibold">{t("home.eq_proc")}</span>
            </div>
            <svg className="w-5 h-5 text-brand-light rotate-90 sm:rotate-0 shrink-0" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24" strokeLinecap="round" strokeLinejoin="round"><path d="M5 12h14M12 5l7 7-7 7" /></svg>
            <div className="glass rounded-2xl px-6 py-5 flex items-center gap-3 w-full sm:w-auto justify-center">
              <svg className="w-5 h-5 text-accent" fill="currentColor" viewBox="0 0 24 24"><path d="M8 5v14l11-7z" /></svg>
              <span className="text-sm font-medium">{t("home.eq_out")}</span>
            </div>
          </div>

          <div className="grid sm:grid-cols-3 gap-10">
            {[
              { n: "01", t: t("landing.step1"), d: t("landing.step1_desc") },
              { n: "02", t: t("landing.step2"), d: t("landing.step2_desc") },
              { n: "03", t: t("landing.step3"), d: t("landing.step3_desc") },
            ].map((s, i) => (
              <div key={s.n} className="relative text-center">
                <div className="text-6xl font-extrabold text-brand/15 mb-4">{s.n}</div>
                <h3 className="text-lg font-bold mb-2">{s.t}</h3>
                <p className="text-sm text-gray-400 leading-relaxed">{s.d}</p>
                {i < 2 && <div className="hidden sm:block absolute top-8 -right-5 text-gray-700"><svg className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24"><path d="M5 12h14M12 5l7 7-7 7" strokeLinecap="round" strokeLinejoin="round"/></svg></div>}
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* Why GenLy — image left / differentiators right, full-bleed band */}
      <section className="relative z-10 border-y border-white/[0.06] bg-white/[0.015]">
        <div className="max-w-6xl mx-auto px-6 py-24 grid lg:grid-cols-2 gap-12 items-center">
          <div className="relative order-2 lg:order-1">
            <div className="absolute -inset-4 bg-accent/15 blur-3xl rounded-full pointer-events-none" />
            <img src="/samples/sample-reef.png" alt="" className="relative rounded-3xl w-full object-cover shadow-glow-lg" />
          </div>
          <div className="order-1 lg:order-2">
            <span className="text-[11px] uppercase tracking-[0.25em] text-brand-light">{t("home.why_label")}</span>
            <h2 className="text-3xl sm:text-4xl font-extrabold tracking-tight mt-3 mb-3 leading-[1.05]">{t("landing.why_title")}</h2>
            <p className="text-gray-400 text-lg mb-8">{t("landing.why_sub")}</p>
            <div className="space-y-5">
              {[
                { t: t("landing.why1_t"), d: t("landing.why1_d") },
                { t: t("landing.why2_t"), d: t("landing.why2_d") },
                { t: t("landing.why3_t"), d: t("landing.why3_d") },
                { t: t("landing.why4_t"), d: t("landing.why4_d") },
              ].map((item) => (
                <div key={item.t} className="flex gap-4 items-start">
                  <div className="w-9 h-9 shrink-0 rounded-lg bg-brand/15 flex items-center justify-center text-brand">
                    <svg className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth="2.5" viewBox="0 0 24 24" strokeLinecap="round" strokeLinejoin="round"><path d="M20 6L9 17l-5-5" /></svg>
                  </div>
                  <div>
                    <h3 className="font-semibold mb-1">{item.t}</h3>
                    <p className="text-sm text-gray-400 leading-relaxed">{item.d}</p>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>
      </section>

      {/* Audience routing — wide cards */}
      <section className="relative z-10 max-w-6xl mx-auto px-6 py-24">
        <div className="grid sm:grid-cols-2 gap-6">
          {[
            { to: "/artistas", t: t("home.aud_artists_t"), d: t("home.aud_artists_d") },
            { to: "/sellos", t: t("home.aud_labels_t"), d: t("home.aud_labels_d") },
          ].map((a) => (
            <Link key={a.to} to={a.to} className="glass rounded-3xl p-10 glass-hover group flex flex-col min-h-[200px]">
              <h3 className="text-2xl font-bold mb-3">{a.t}</h3>
              <p className="text-gray-400 leading-relaxed flex-1">{a.d}</p>
              <span className="mt-5 inline-flex items-center gap-1.5 text-sm font-medium text-brand-light group-hover:gap-2.5 transition-all">
                {t("home.aud_cta")}
                <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24" strokeLinecap="round" strokeLinejoin="round"><path d="M5 12h14M12 5l7 7-7 7" /></svg>
              </span>
            </Link>
          ))}
        </div>
      </section>

      {/* Partners — full-bleed strip */}
      <section className="relative z-10 border-y border-white/[0.06] bg-white/[0.015] py-12">
        <div className="max-w-4xl mx-auto px-6 text-center">
          <p className="text-xs text-gray-600 uppercase tracking-widest mb-7">{t("landing.integrated")}</p>
          <div className="flex flex-wrap justify-center items-center gap-x-12 gap-y-5 opacity-50">
            <div className="flex items-center gap-2">
              <svg className="w-5 h-5 text-red-500" fill="currentColor" viewBox="0 0 24 24"><path d="M22.54 6.42a2.78 2.78 0 00-1.94-2C18.88 4 12 4 12 4s-6.88 0-8.6.46a2.78 2.78 0 00-1.94 2A29 29 0 001 11.75a29 29 0 00.46 5.33A2.78 2.78 0 003.4 19.13C5.12 19.56 12 19.56 12 19.56s6.88 0 8.6-.46a2.78 2.78 0 001.94-2A29 29 0 0023 11.75a29 29 0 00-.46-5.33z"/><polygon points="9.75 15.02 15.5 11.75 9.75 8.48" fill="white"/></svg>
              <span className="text-sm font-semibold text-white">YouTube API</span>
            </div>
            <div className="flex items-center gap-2">
              <svg className="w-5 h-5" viewBox="0 0 24 24" fill="none"><path d="M12 2L2 7l10 5 10-5-10-5z" fill="#4285F4"/><path d="M2 17l10 5 10-5" stroke="#34A853" strokeWidth="2"/><path d="M2 12l10 5 10-5" stroke="#FBBC05" strokeWidth="2"/></svg>
              <span className="text-sm font-semibold text-white">Google AI</span>
            </div>
            <div className="flex items-center gap-2">
              <svg className="w-5 h-5 text-gray-300" fill="none" stroke="currentColor" strokeWidth="1.5" viewBox="0 0 24 24"><path d="M12 18.5a6.5 6.5 0 006.5-6.5V6a6.5 6.5 0 10-13 0v6a6.5 6.5 0 006.5 6.5z"/><path d="M19 10v2a7 7 0 01-14 0v-2"/></svg>
              <span className="text-sm font-semibold text-white">Whisper AI</span>
            </div>
          </div>
        </div>
      </section>

      {/* Social proof slots — null until real assets exist */}
      <SocialProofWall title="Confían en GenLy" />
      <Testimonials title="Lo que dicen los artistas" />

      {/* Final CTA — full-bleed gradient band */}
      <section className="relative z-10 border-t border-white/[0.06] bg-gradient-to-b from-brand/[0.08] to-transparent py-28 px-6 text-center">
        <h2 className="text-4xl sm:text-5xl font-extrabold tracking-tight mb-4">{t("landing.ready")}</h2>
        <p className="text-gray-400 text-lg mb-9 max-w-md mx-auto">{t("landing.ready_sub")}</p>
        <button onClick={onStart} className="px-10 py-5 rounded-full bg-white text-black font-bold text-lg hover:scale-[1.03] transition-transform shadow-glow-lg">
          {t("landing.create")}
          <svg className="inline-block ml-2 w-5 h-5" fill="none" stroke="currentColor" strokeWidth="2.5" viewBox="0 0 24 24" strokeLinecap="round" strokeLinejoin="round"><path d="M5 12h14M12 5l7 7-7 7" /></svg>
        </button>
      </section>
    </>
  );
}
