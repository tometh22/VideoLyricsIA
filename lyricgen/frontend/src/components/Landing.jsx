import { useI18n } from "../i18n";

export default function Landing({ onStart, onLogin, isLoggedIn = false }) {
  const { t, lang, setLang } = useI18n();

  const FEATURES = [
    {
      title: t("feat.lyrics"),
      desc: t("feat.lyrics_desc"),
      icon: <svg className="w-6 h-6" fill="none" stroke="currentColor" strokeWidth="1.5" viewBox="0 0 24 24"><path d="M12 18.5a6.5 6.5 0 006.5-6.5V6a6.5 6.5 0 10-13 0v6a6.5 6.5 0 006.5 6.5z" strokeLinecap="round" strokeLinejoin="round"/><path d="M19 10v2a7 7 0 01-14 0v-2M12 18.5V22M8 22h8" strokeLinecap="round" strokeLinejoin="round"/></svg>,
    },
    {
      title: t("feat.backgrounds"),
      desc: t("feat.backgrounds_desc"),
      icon: <svg className="w-6 h-6" fill="none" stroke="currentColor" strokeWidth="1.5" viewBox="0 0 24 24"><rect x="2" y="2" width="20" height="20" rx="2.18"/><path d="M7 2v20M17 2v20M2 12h20M2 7h5M2 17h5M17 17h5M17 7h5"/></svg>,
    },
    {
      title: t("feat.outputs"),
      desc: t("feat.outputs_desc"),
      icon: <svg className="w-6 h-6" fill="none" stroke="currentColor" strokeWidth="1.5" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4M7 10l5 5 5-5M12 15V3" strokeLinecap="round" strokeLinejoin="round"/></svg>,
    },
    {
      title: t("feat.youtube"),
      desc: t("feat.youtube_desc"),
      icon: <svg className="w-6 h-6" fill="none" stroke="currentColor" strokeWidth="1.5" viewBox="0 0 24 24"><path d="M22.54 6.42a2.78 2.78 0 00-1.94-2C18.88 4 12 4 12 4s-6.88 0-8.6.46a2.78 2.78 0 00-1.94 2A29 29 0 001 11.75a29 29 0 00.46 5.33A2.78 2.78 0 003.4 19.13C5.12 19.56 12 19.56 12 19.56s6.88 0 8.6-.46a2.78 2.78 0 001.94-2A29 29 0 0023 11.75a29 29 0 00-.46-5.33z"/><polygon points="9.75 15.02 15.5 11.75 9.75 8.48 9.75 15.02"/></svg>,
    },
    {
      title: t("feat.batch"),
      desc: t("feat.batch_desc"),
      icon: <svg className="w-6 h-6" fill="none" stroke="currentColor" strokeWidth="1.5" viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>,
    },
    {
      title: t("feat.commercial"),
      desc: t("feat.commercial_desc"),
      icon: <svg className="w-6 h-6" fill="none" stroke="currentColor" strokeWidth="1.5" viewBox="0 0 24 24"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" strokeLinecap="round" strokeLinejoin="round"/></svg>,
    },
  ];

  const STEPS = [
    { num: "01", title: t("landing.step1"), desc: t("landing.step1_desc") },
    { num: "02", title: t("landing.step2"), desc: t("landing.step2_desc") },
    { num: "03", title: t("landing.step3"), desc: t("landing.step3_desc") },
  ];

  const PLANS = [
    { name: "Free", videos: "5", price: "0", perVideo: "0", popular: false, free: true },
    { name: "100", videos: "100", price: "900", perVideo: "9.00", popular: false },
    { name: "250", videos: "250", price: "2,000", perVideo: "8.00", popular: true },
    { name: "500", videos: "500", price: "3,500", perVideo: "7.00", popular: false },
    { name: "1,000", videos: "1,000", price: "6,000", perVideo: "6.00", popular: false },
  ];

  const FAQS = [
    { q: t("faq.q1"), a: t("faq.a1") },
    { q: t("faq.q2"), a: t("faq.a2") },
    { q: t("faq.q3"), a: t("faq.a3") },
    { q: t("faq.q4"), a: t("faq.a4") },
    { q: t("faq.q5"), a: t("faq.a5") },
    { q: t("faq.q6"), a: t("faq.a6") },
  ];

  return (
    <div className="min-h-screen bg-surface relative overflow-hidden">
      <div className="fixed inset-0 pointer-events-none">
        <div className="absolute top-[-20%] left-[10%] w-[700px] h-[700px] bg-brand/[0.05] rounded-full blur-[150px]" />
        <div className="absolute bottom-[-10%] right-[5%] w-[500px] h-[500px] bg-brand-light/[0.04] rounded-full blur-[120px]" />
        <div className="absolute top-[40%] right-[20%] w-[300px] h-[300px] bg-accent/[0.03] rounded-full blur-[100px]" />
      </div>

      {/* Sticky Nav */}
      <nav className="sticky top-0 z-30 bg-surface/80 backdrop-blur-xl border-b border-white/[0.04]">
        <div className="flex items-center justify-between px-8 py-4 max-w-6xl mx-auto">
          <div className="flex items-center gap-3">
            <div className="w-9 h-9 rounded-xl bg-gradient-to-br from-brand to-brand-light flex items-center justify-center shadow-glow">
              <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
                <path d="M9 18V5l12-2v13" /><circle cx="6" cy="18" r="3" /><circle cx="18" cy="16" r="3" />
              </svg>
            </div>
            <span className="text-lg font-bold tracking-tight">GenLy AI</span>
          </div>
          <div className="hidden md:flex items-center gap-6">
            <a href="#features" className="text-xs text-gray-400 hover:text-white transition-colors">{t("landing.features")}</a>
            <a href="#pricing" className="text-xs text-gray-400 hover:text-white transition-colors">{t("landing.pricing")}</a>
            <a href="#faq" className="text-xs text-gray-400 hover:text-white transition-colors">FAQ</a>

            {/* Language switcher */}
            <div className="flex items-center gap-1 ml-2">
              {["es", "en", "pt"].map((code) => (
                <button
                  key={code}
                  onClick={() => setLang(code)}
                  className={`text-[10px] font-bold px-2 py-1 rounded-md transition-all uppercase
                    ${lang === code ? "text-white bg-white/10" : "text-gray-600 hover:text-gray-400"}`}
                >
                  {code}
                </button>
              ))}
            </div>

            {isLoggedIn ? (
              <button onClick={onStart} className="btn-primary text-xs py-2 px-5">{t("nav.dashboard")}</button>
            ) : (
              <div className="flex items-center gap-2">
                <button onClick={onLogin} className="text-xs text-gray-400 hover:text-white transition-colors">
                  {t("login.title")}
                </button>
                <button onClick={onLogin} className="btn-primary text-xs py-2 px-5">{t("nav.start")}</button>
              </div>
            )}
          </div>
          <button onClick={isLoggedIn ? onStart : onLogin} className="md:hidden btn-primary text-xs py-2 px-5">{t("nav.start")}</button>
        </div>
      </nav>

      {/* Hero */}
      <section className="relative z-10 pt-16 pb-8 px-6 max-w-6xl mx-auto">
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-12 items-center">
          <div>
            <div className="inline-block px-4 py-1.5 rounded-full glass text-xs text-gray-400 mb-6 animate-fade-in">
              {t("landing.badge")}
            </div>
            <h1 className="text-5xl sm:text-6xl font-extrabold tracking-tight mb-6 leading-[1.1] animate-fade-in">
              <span className="bg-gradient-to-r from-white via-white to-gray-400 bg-clip-text text-transparent">{t("landing.hero1")}</span><br />
              <span className="bg-gradient-to-r from-brand to-brand-light bg-clip-text text-transparent">{t("landing.hero2")}</span><br />
              <span className="bg-gradient-to-r from-white via-white to-gray-400 bg-clip-text text-transparent">{t("landing.hero3")}</span>
            </h1>
            <p className="text-gray-400 text-lg max-w-lg leading-relaxed mb-8 animate-fade-in">
              {t("landing.hero_sub")}
            </p>
            <button onClick={onStart} className="btn-primary text-lg py-4 px-10 animate-fade-in">
              {t("landing.cta")}
              <svg className="inline-block ml-2 w-5 h-5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24" strokeLinecap="round" strokeLinejoin="round"><path d="M5 12h14M12 5l7 7-7 7" /></svg>
            </button>
          </div>

          {/* Real video mockup */}
          <div className="relative animate-slide-up hidden lg:block">
            <div className="glass rounded-3xl p-3 shadow-glow-lg">
              <div className="rounded-2xl overflow-hidden aspect-video relative bg-black">
                <video autoPlay muted loop playsInline className="w-full h-full object-cover" src="/demo.mp4" />
                <div className="absolute bottom-3 left-3 right-3 flex items-center gap-2 bg-black/30 backdrop-blur-sm rounded-lg px-3 py-1.5">
                  <svg className="w-3.5 h-3.5 text-white/70" fill="currentColor" viewBox="0 0 24 24"><path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/></svg>
                  <div className="h-1 flex-1 bg-white/20 rounded-full overflow-hidden"><div className="h-full w-1/2 bg-brand rounded-full"/></div>
                  <span className="text-[10px] text-white/50">HD</span>
                </div>
              </div>
            </div>
            {/* Floating Short */}
            <div className="absolute -bottom-6 -left-8 glass rounded-2xl p-2 shadow-glow w-28 animate-pulse-slow">
              <div className="rounded-xl bg-gradient-to-b from-pink-500/20 to-brand/20 aspect-[9/16] flex items-center justify-center">
                <p className="text-[8px] font-bold text-white/70 uppercase">YouTube Short</p>
              </div>
            </div>
            {/* Floating YouTube badge */}
            <div className="absolute -top-4 -right-6 glass rounded-2xl px-4 py-3 shadow-glow flex items-center gap-2">
              <svg className="w-5 h-5 text-red-500" fill="currentColor" viewBox="0 0 24 24"><path d="M22.54 6.42a2.78 2.78 0 00-1.94-2C18.88 4 12 4 12 4s-6.88 0-8.6.46a2.78 2.78 0 00-1.94 2A29 29 0 001 11.75a29 29 0 00.46 5.33A2.78 2.78 0 003.4 19.13C5.12 19.56 12 19.56 12 19.56s6.88 0 8.6-.46a2.78 2.78 0 001.94-2A29 29 0 0023 11.75a29 29 0 00-.46-5.33z"/><polygon points="9.75 15.02 15.5 11.75 9.75 8.48 9.75 15.02" fill="white"/></svg>
              <div>
                <p className="text-[9px] font-bold text-white/80">{t("feat.youtube").split(" ").slice(0, 2).join(" ")}</p>
                <p className="text-[7px] text-white/40">SEO</p>
              </div>
            </div>
          </div>
        </div>

        {/* Stats */}
        <div className="flex justify-center gap-16 mt-20 pt-12 border-t border-white/[0.04]">
          {[
            { value: "< 5 min", label: t("landing.per_video") },
            { value: "3", label: t("landing.outputs") },
            { value: "100%", label: t("landing.commercial") },
            { value: "6+", label: t("landing.languages") },
          ].map((s) => (
            <div key={s.label} className="text-center">
              <p className="text-3xl font-bold bg-gradient-to-r from-white to-gray-400 bg-clip-text text-transparent">{s.value}</p>
              <p className="text-xs text-gray-500 mt-1">{s.label}</p>
            </div>
          ))}
        </div>
      </section>

      {/* Integrations */}
      <section className="relative z-10 py-12 px-6 max-w-4xl mx-auto text-center">
        <p className="text-xs text-gray-600 uppercase tracking-widest mb-6">{t("landing.integrated")}</p>
        <div className="flex justify-center items-center gap-10 opacity-40">
          {/* YouTube */}
          <div className="flex items-center gap-2">
            <svg className="w-5 h-5 text-red-500" fill="currentColor" viewBox="0 0 24 24"><path d="M22.54 6.42a2.78 2.78 0 00-1.94-2C18.88 4 12 4 12 4s-6.88 0-8.6.46a2.78 2.78 0 00-1.94 2A29 29 0 001 11.75a29 29 0 00.46 5.33A2.78 2.78 0 003.4 19.13C5.12 19.56 12 19.56 12 19.56s6.88 0 8.6-.46a2.78 2.78 0 001.94-2A29 29 0 0023 11.75a29 29 0 00-.46-5.33z"/><polygon points="9.75 15.02 15.5 11.75 9.75 8.48" fill="white"/></svg>
            <span className="text-sm font-semibold text-white">YouTube API</span>
          </div>
          {/* Google AI */}
          <div className="flex items-center gap-2">
            <svg className="w-5 h-5" viewBox="0 0 24 24" fill="none"><path d="M12 2L2 7l10 5 10-5-10-5z" fill="#4285F4"/><path d="M2 17l10 5 10-5" stroke="#34A853" strokeWidth="2"/><path d="M2 12l10 5 10-5" stroke="#FBBC05" strokeWidth="2"/></svg>
            <span className="text-sm font-semibold text-white">Google AI</span>
          </div>
          {/* Whisper */}
          <div className="flex items-center gap-2">
            <svg className="w-5 h-5 text-gray-300" fill="none" stroke="currentColor" strokeWidth="1.5" viewBox="0 0 24 24"><path d="M12 18.5a6.5 6.5 0 006.5-6.5V6a6.5 6.5 0 10-13 0v6a6.5 6.5 0 006.5 6.5z"/><path d="M19 10v2a7 7 0 01-14 0v-2"/></svg>
            <span className="text-sm font-semibold text-white">Whisper AI</span>
          </div>
        </div>
      </section>

      {/* How it works */}
      <section className="relative z-10 py-24 px-6 max-w-5xl mx-auto">
        <h2 className="text-3xl font-bold text-center mb-4">{t("landing.how")}</h2>
        <p className="text-gray-500 text-center mb-16 max-w-md mx-auto">{t("landing.how_sub")}</p>
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-10">
          {STEPS.map((step, i) => (
            <div key={step.num} className="relative text-center group">
              <div className="text-6xl font-extrabold text-brand/10 group-hover:text-brand/20 transition-colors mb-4">{step.num}</div>
              <h3 className="text-lg font-bold mb-3">{step.title}</h3>
              <p className="text-sm text-gray-400 leading-relaxed">{step.desc}</p>
              {i < 2 && <div className="hidden sm:block absolute top-8 -right-6 text-gray-700"><svg className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24"><path d="M5 12h14M12 5l7 7-7 7" strokeLinecap="round" strokeLinejoin="round"/></svg></div>}
            </div>
          ))}
        </div>
      </section>

      {/* Outputs */}
      <section className="relative z-10 py-20 px-6 max-w-5xl mx-auto">
        <h2 className="text-3xl font-bold text-center mb-4">{t("landing.outputs_title")}</h2>
        <p className="text-gray-500 text-center mb-16 max-w-md mx-auto">{t("landing.outputs_sub")}</p>
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-6">
          {[
            { title: "Lyric Video", res: "1920 x 1080", desc: "Full HD", gradient: "from-brand/30 to-brand-dark/30", aspect: "aspect-video" },
            { title: "YouTube Short", res: "1080 x 1920", desc: "Vertical 30s", gradient: "from-pink-500/20 to-rose-600/30", aspect: "aspect-[9/16] max-h-52" },
            { title: "Thumbnail", res: "1280 x 720", desc: "1280x720", gradient: "from-amber-500/20 to-orange-600/30", aspect: "aspect-video" },
          ].map((item) => (
            <div key={item.title} className="glass rounded-3xl p-5 text-center glass-hover">
              <div className={`rounded-2xl bg-gradient-to-br ${item.gradient} ${item.aspect} mx-auto mb-4 flex items-center justify-center`}>
                <p className="text-xs font-bold text-white/60">{item.res}</p>
              </div>
              <h3 className="font-semibold mb-1">{item.title}</h3>
              <p className="text-xs text-gray-500">{item.desc}</p>
            </div>
          ))}
        </div>
      </section>

      {/* Features */}
      <section id="features" className="relative z-10 py-20 px-6 max-w-5xl mx-auto scroll-mt-20">
        <h2 className="text-3xl font-bold text-center mb-4">{t("landing.features")}</h2>
        <p className="text-gray-500 text-center mb-16 max-w-lg mx-auto">{t("landing.features_sub")}</p>
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-5">
          {FEATURES.map((f) => (
            <div key={f.title} className="glass rounded-2xl p-6 glass-hover">
              <div className="w-11 h-11 rounded-xl bg-brand/10 flex items-center justify-center text-brand mb-4">{f.icon}</div>
              <h3 className="font-semibold mb-2">{f.title}</h3>
              <p className="text-sm text-gray-400 leading-relaxed">{f.desc}</p>
            </div>
          ))}
        </div>
      </section>

      {/* Pricing */}
      <section id="pricing" className="relative z-10 py-24 px-6 max-w-5xl mx-auto scroll-mt-20">
        <h2 className="text-3xl font-bold text-center mb-4">{t("landing.pricing")}</h2>
        <p className="text-gray-500 text-center mb-16 max-w-md mx-auto">{t("landing.pricing_sub")}</p>
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-5 gap-4">
          {PLANS.map((plan) => (
            <div key={plan.name} className={`glass rounded-3xl p-6 text-center relative ${plan.popular ? "border-brand/30 shadow-glow" : ""}`}>
              {plan.popular && <div className="absolute -top-3 left-1/2 -translate-x-1/2 px-3 py-0.5 rounded-full bg-brand text-[10px] font-bold uppercase tracking-wider">{t("landing.popular")}</div>}
              <p className="text-sm text-gray-400 mb-1">{plan.videos} {t("landing.videos_month")}</p>
              <p className="text-3xl font-bold my-3">
                {plan.free ? (
                  <span className="text-accent">Free</span>
                ) : (
                  <><span className="text-lg text-gray-500">USD</span> {plan.price}</>
                )}
              </p>
              <p className="text-xs text-gray-500 mb-5">
                {plan.free ? t("landing.free_trial") || "Start for free" : `$${plan.perVideo} ${t("landing.per_video")}`}
              </p>
              <button onClick={onLogin || onStart} className={`w-full py-2.5 rounded-xl text-sm font-medium transition-all ${plan.popular ? "btn-primary" : plan.free ? "btn-primary !from-accent !to-accent" : "btn-secondary"}`}>
                {plan.free ? (t("login.register_submit") || "Sign up") : t("nav.start")}
              </button>
            </div>
          ))}
        </div>
        <p className="text-center text-xs text-gray-500 mt-6">{t("landing.overage")}</p>
        <p className="text-center text-xs text-gray-600 mt-2">{t("landing.yt_addon")}</p>
      </section>

      {/* FAQ */}
      <section id="faq" className="relative z-10 py-20 px-6 max-w-3xl mx-auto scroll-mt-20">
        <h2 className="text-3xl font-bold text-center mb-16">{t("landing.faq")}</h2>
        <div className="space-y-4">
          {FAQS.map((faq, i) => (
            <details key={i} className="glass rounded-2xl group">
              <summary className="px-6 py-4 cursor-pointer text-sm font-medium text-white flex items-center justify-between list-none">
                {faq.q}
                <svg className="w-4 h-4 text-gray-500 group-open:rotate-180 transition-transform" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24"><path d="M6 9l6 6 6-6"/></svg>
              </summary>
              <div className="px-6 pb-4 text-sm text-gray-400 leading-relaxed">{faq.a}</div>
            </details>
          ))}
        </div>
      </section>

      {/* CTA */}
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

      {/* Footer */}
      <footer className="relative z-10 border-t border-white/[0.04] py-8 px-8 text-center">
        <p className="text-xs text-gray-600">{t("landing.footer")}</p>
      </footer>
    </div>
  );
}
