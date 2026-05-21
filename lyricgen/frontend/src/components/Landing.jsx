import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useI18n } from "../i18n";
import SocialProofWall from "./SocialProofWall";
import Testimonials from "./Testimonials";

const API = import.meta.env.VITE_API_URL || "";

// Home page. Rendered inside MarketingLayout — the nav, announcement bar and
// footer live in the layout, not here. Navigation actions route to /login.
export default function Landing() {
  const { t } = useI18n();
  const navigate = useNavigate();
  const onStart = () => navigate("/login");
  const onLogin = () => navigate("/login");

  // Lead form → POST /api/leads. Falls back to a prefilled mailto if the API fails.
  const [formState, setFormState] = useState("idle"); // idle | loading | sent | error
  const handleSalesSubmit = async (e) => {
    e.preventDefault();
    const f = e.target;
    const payload = {
      name: f.name.value.trim(),
      company: f.company.value.trim(),
      email: f.email.value.trim(),
      volume: f.volume.value,
      message: f.message.value.trim(),
    };
    setFormState("loading");
    try {
      const res = await fetch(`${API}/api/leads`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setFormState("sent");
      f.reset();
    } catch {
      setFormState("error");
      // Fallback: open a prefilled email so the lead is never lost.
      const subject = `GenLy AI — Consulta de ventas${payload.company ? ` (${payload.company})` : ""}`;
      const body = [
        `Nombre: ${payload.name}`,
        `Sello/empresa: ${payload.company}`,
        `Email: ${payload.email}`,
        `Volumen estimado: ${payload.volume}`,
        "",
        payload.message,
      ].join("\n");
      window.location.href = `mailto:tomas@epical.digital?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(body)}`;
    }
  };

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
    { kind: "free", name: t("landing.plan_free"), desc: t("landing.plan_free_desc"), price: "0" },
    { kind: "indie", name: t("landing.plan_indie"), desc: t("landing.plan_indie_desc"), price: t("landing.plan_indie_price"), popular: true },
    { kind: "label", name: t("landing.plan_label"), desc: t("landing.plan_label_desc"), price: t("landing.plan_label_price"), contact: true },
  ];

  const FAQS = [
    { q: t("faq.q1"), a: t("faq.a1") },
    { q: t("faq.q2"), a: t("faq.a2") },
    { q: t("faq.q3"), a: t("faq.a3") },
    { q: t("faq.q4"), a: t("faq.a4") },
    { q: t("faq.q5"), a: t("faq.a5") },
    { q: t("faq.q6"), a: t("faq.a6") },
    { q: t("faq.q7"), a: t("faq.a7") },
    { q: t("faq.q8"), a: t("faq.a8") },
    { q: t("faq.q9"), a: t("faq.a9") },
    { q: t("faq.q10"), a: t("faq.a10") },
  ];

  return (
    <>

      {/* Hero — kinetic lyric typography over a neon stage. The words animate
          in like a lyric video; minimal chrome; one strong CTA. */}
      <section className="relative min-h-screen w-full overflow-hidden flex flex-col">
        {/* Neon stage: drifting glows + faint grid */}
        <div className="absolute inset-0 overflow-hidden pointer-events-none">
          <div className="absolute -top-[20%] -left-[10%] w-[60vw] h-[60vw] rounded-full bg-brand/40 blur-[130px] animate-drift" />
          <div className="absolute -bottom-[15%] -right-[8%] w-[48vw] h-[48vw] rounded-full bg-accent/30 blur-[130px] animate-drift" style={{ animationDirection: "reverse", animationDuration: "20s" }} />
          <div className="absolute inset-0" style={{ background: "radial-gradient(ellipse at 50% 38%, rgba(109,74,255,.16), transparent 60%)" }} />
          <div className="absolute inset-0 opacity-[0.05]" style={{ backgroundImage: "linear-gradient(#fff 1px,transparent 1px),linear-gradient(90deg,#fff 1px,transparent 1px)", backgroundSize: "64px 64px" }} />
        </div>

        <div className="relative z-20 flex-1 flex flex-col items-center justify-center text-center px-6 pt-24 pb-16">
          {/* Equalizer + now-playing cue */}
          <div className="flex items-center gap-1.5 mb-8 h-6 animate-fade-in">
            {[0, 0.2, 0.4, 0.1, 0.3].map((d, i) => (
              <span key={i} className="w-[3px] rounded-full bg-accent animate-eq" style={{ animationDelay: `${d}s`, height: "6px" }} />
            ))}
            <span className="ml-3 text-[11px] uppercase tracking-[0.25em] text-white/40">{t("landing.badge")}</span>
          </div>

          {/* Giant kinetic claim — words fade in one by one */}
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

        {/* Scroll cue */}
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

      {/* Customer logos — null until real logos are provided */}
      <SocialProofWall title="Confían en GenLy" />

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
            { title: "Lyric Video", res: "1920 x 1080", desc: "Full HD", gradient: "from-brand/30 to-brand-dark/30", aspect: "aspect-video", img: "/samples/sample-reef.png" },
            { title: "YouTube Short", res: "1080 x 1920", desc: "Vertical 30s", gradient: "from-pink-500/20 to-rose-600/30", aspect: "aspect-[9/16] max-h-52", img: null },
            { title: "Thumbnail", res: "1280 x 720", desc: "1280x720", gradient: "from-amber-500/20 to-orange-600/30", aspect: "aspect-video", img: "/samples/sample-forest.png" },
          ].map((item) => (
            <div key={item.title} className="glass rounded-3xl p-5 text-center glass-hover">
              <div className={`rounded-2xl overflow-hidden bg-gradient-to-br ${item.gradient} ${item.aspect} mx-auto mb-4 flex items-center justify-center`}>
                {item.img ? (
                  <img src={item.img} alt={item.title} className="w-full h-full object-cover" />
                ) : (
                  <p className="text-xs font-bold text-white/60">{item.res}</p>
                )}
              </div>
              <h3 className="font-semibold mb-1">{item.title}</h3>
              <p className="text-xs text-gray-500">{item.desc}</p>
            </div>
          ))}
        </div>
      </section>

      {/* Examples — real generated clips */}
      <section id="examples" className="relative z-10 py-20 px-6 max-w-5xl mx-auto scroll-mt-20">
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

      {/* Why GenLy — differentiation */}
      <section className="relative z-10 py-20 px-6 max-w-5xl mx-auto">
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
        {/* Trust strip */}
        <div className="flex flex-wrap justify-center gap-x-8 gap-y-3 mt-12 pt-8 border-t border-white/[0.04]">
          {[t("landing.trust_sla"), t("landing.trust_owned"), t("landing.trust_isolation"), t("landing.trust_noroyalty")].map((label) => (
            <div key={label} className="flex items-center gap-2 text-xs text-gray-400">
              <svg className="w-4 h-4 text-accent" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24" strokeLinecap="round" strokeLinejoin="round"><path d="M9 12l2 2 4-4" /><circle cx="12" cy="12" r="10" /></svg>
              {label}
            </div>
          ))}
        </div>
      </section>

      {/* Comparison — GenLy vs studio vs self-serve tools */}
      <section className="relative z-10 py-20 px-6 max-w-4xl mx-auto">
        <h2 className="text-3xl font-bold text-center mb-4">{t("cmp.title")}</h2>
        <p className="text-gray-500 text-center mb-12 max-w-md mx-auto">{t("cmp.sub")}</p>
        <div className="glass rounded-3xl overflow-hidden text-sm">
          <div className="grid grid-cols-[1.4fr_1fr_1fr_1fr] text-center border-b border-white/[0.06]">
            <div className="p-4" />
            <div className="p-4 font-bold text-brand-light bg-brand/10">{t("cmp.genly")}</div>
            <div className="p-4 text-xs text-gray-400">{t("cmp.studio")}</div>
            <div className="p-4 text-xs text-gray-400">{t("cmp.tools")}</div>
          </div>
          {[
            { l: t("cmp.r_price"), g: t("cmp.r_price_g"), s: t("cmp.r_price_s"), o: t("cmp.r_price_t") },
            { l: t("cmp.r_time"), g: t("cmp.r_time_g"), s: t("cmp.r_time_s"), o: t("cmp.r_time_t") },
            { l: t("cmp.r_bg"), g: "yes", s: "no", o: "partial" },
            { l: t("cmp.r_rights"), g: "yes", s: "no", o: "partial" },
            { l: t("cmp.r_prores"), g: "yes", s: "yes", o: "no" },
            { l: t("cmp.r_batch"), g: "yes", s: "no", o: "no" },
            { l: t("cmp.r_langs"), g: "yes", s: "partial", o: "partial" },
          ].map((row, i) => {
            const cell = (v) =>
              v === "yes" ? <span className="text-accent font-bold">✓</span>
              : v === "no" ? <span className="text-gray-600">–</span>
              : v === "partial" ? <span className="text-gray-500">{t("cmp.partial")}</span>
              : <span>{v}</span>;
            return (
              <div key={i} className="grid grid-cols-[1.4fr_1fr_1fr_1fr] text-center items-center border-b border-white/[0.04] last:border-0">
                <div className="p-4 text-left text-gray-300">{row.l}</div>
                <div className="p-4 bg-brand/[0.06] font-medium text-white">{cell(row.g)}</div>
                <div className="p-4 text-gray-500">{cell(row.s)}</div>
                <div className="p-4 text-gray-500">{cell(row.o)}</div>
              </div>
            );
          })}
        </div>
      </section>

      {/* For labels — enterprise block */}
      <section className="relative z-10 py-16 px-6 max-w-5xl mx-auto">
        <div className="glass rounded-3xl p-8 sm:p-10 border border-brand/20 shadow-glow">
          <div className="text-center mb-10">
            <span className="text-[11px] uppercase tracking-[0.25em] text-brand-light">{t("landing.plan_label")}</span>
            <h2 className="text-2xl sm:text-3xl font-bold mt-2 mb-3">{t("labels.title")}</h2>
            <p className="text-gray-400 max-w-lg mx-auto">{t("labels.sub")}</p>
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-5">
            {[
              { t: t("labels.f1_t"), d: t("labels.f1_d") },
              { t: t("labels.f2_t"), d: t("labels.f2_d") },
              { t: t("labels.f3_t"), d: t("labels.f3_d") },
              { t: t("labels.f4_t"), d: t("labels.f4_d") },
              { t: t("labels.f5_t"), d: t("labels.f5_d") },
              { t: t("labels.f6_t"), d: t("labels.f6_d") },
            ].map((f) => (
              <div key={f.t} className="flex gap-3 items-start">
                <svg className="w-5 h-5 shrink-0 text-accent mt-0.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24" strokeLinecap="round" strokeLinejoin="round"><path d="M9 12l2 2 4-4" /><circle cx="12" cy="12" r="10" /></svg>
                <div>
                  <h3 className="font-semibold mb-1 text-sm">{f.t}</h3>
                  <p className="text-xs text-gray-400 leading-relaxed">{f.d}</p>
                </div>
              </div>
            ))}
          </div>
          <div className="text-center mt-10">
            <a href="#contact" className="btn-primary py-3 px-8 inline-flex items-center justify-center">
              {t("labels.cta")}
              <svg className="inline-block ml-2 w-4 h-4" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24" strokeLinecap="round" strokeLinejoin="round"><path d="M5 12h14M12 5l7 7-7 7" /></svg>
            </a>
          </div>
        </div>
      </section>

      {/* Features */}
      <section id="features" className="relative z-10 py-20 px-6 max-w-5xl mx-auto scroll-mt-20">
        <h2 className="text-3xl font-bold text-center mb-4">{t("landing.features")}</h2>
        <p className="text-gray-500 text-center mb-16 max-w-lg mx-auto">{t("landing.features_sub")}</p>
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-5">
          {FEATURES.map((f) => (
            <div key={f.title} className="glass rounded-card p-6 glass-hover">
              <div className="w-11 h-11 rounded-xl bg-brand/10 flex items-center justify-center text-brand mb-4">{f.icon}</div>
              <h3 className="font-semibold mb-2">{f.title}</h3>
              <p className="text-sm text-gray-400 leading-relaxed">{f.desc}</p>
            </div>
          ))}
        </div>
      </section>

      {/* Testimonials — null until real quotes are provided */}
      <Testimonials title="Lo que dicen los artistas" />

      {/* Ownership / rights — the closer */}
      <section className="relative z-10 py-16 px-6 max-w-4xl mx-auto">
        <div className="glass rounded-3xl p-10 border border-accent/20 shadow-glow text-center">
          <h2 className="text-2xl sm:text-3xl font-bold mb-3">{t("landing.own_title")}</h2>
          <p className="text-gray-400 mb-8 max-w-lg mx-auto">{t("landing.own_sub")}</p>
          <div className="space-y-4 max-w-2xl mx-auto text-left">
            {[t("landing.own1"), t("landing.own2"), t("landing.own3")].map((line) => (
              <div key={line} className="flex gap-3 items-start">
                <svg className="w-5 h-5 shrink-0 text-accent mt-0.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24" strokeLinecap="round" strokeLinejoin="round"><path d="M9 12l2 2 4-4" /><circle cx="12" cy="12" r="10" /></svg>
                <p className="text-sm text-gray-300 leading-relaxed">{line}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* Novedades — "alive" signal: real shipped capabilities */}
      <section className="relative z-10 py-20 px-6 max-w-5xl mx-auto">
        <h2 className="text-3xl font-bold text-center mb-4">{t("news.title")}</h2>
        <p className="text-gray-500 text-center mb-12 max-w-md mx-auto">{t("news.sub")}</p>
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-5">
          {[
            { t: t("news.1_t"), d: t("news.1_d") },
            { t: t("news.2_t"), d: t("news.2_d") },
            { t: t("news.3_t"), d: t("news.3_d") },
          ].map((n) => (
            <div key={n.t} className="glass rounded-card p-6 glass-hover">
              <span className="inline-block px-2 py-0.5 rounded-full bg-accent/15 text-accent text-[10px] font-bold uppercase tracking-wider mb-3">Nuevo</span>
              <h3 className="font-semibold mb-1.5">{n.t}</h3>
              <p className="text-sm text-gray-400 leading-relaxed">{n.d}</p>
            </div>
          ))}
        </div>
      </section>

      {/* Pricing */}
      <section id="pricing" className="relative z-10 py-24 px-6 max-w-5xl mx-auto scroll-mt-20">
        <h2 className="text-3xl font-bold text-center mb-4">{t("landing.pricing")}</h2>
        <p className="text-gray-500 text-center mb-4 max-w-md mx-auto">{t("landing.pricing_sub")}</p>
        <p className="text-center text-sm text-accent font-medium mb-12 max-w-xl mx-auto">{t("landing.pricing_scale")}</p>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-5 max-w-4xl mx-auto">
          {PLANS.map((plan) => (
            <div key={plan.kind} className={`glass rounded-3xl p-7 text-center relative flex flex-col ${plan.popular ? "border-brand/30 shadow-glow" : ""}`}>
              {plan.popular && <div className="absolute -top-3 left-1/2 -translate-x-1/2 px-3 py-0.5 rounded-full bg-brand text-[10px] font-bold uppercase tracking-wider">{t("landing.popular")}</div>}
              <h3 className="text-base font-semibold mb-2">{plan.name}</h3>
              <p className="text-3xl font-bold my-2">
                {plan.kind === "free" ? (
                  <span className="text-accent">Free</span>
                ) : plan.contact ? (
                  <span className="text-2xl">{plan.price}</span>
                ) : (
                  <>{plan.price}<span className="text-sm text-gray-500"> / {t("landing.per_video")}</span></>
                )}
              </p>
              <p className="text-xs text-gray-400 leading-relaxed mb-6 flex-1">{plan.desc}</p>
              {plan.contact ? (
                <a href="#contact" className="btn-secondary w-full py-2.5 rounded-xl text-sm font-medium inline-flex items-center justify-center">
                  {t("landing.plan_contact_cta")}
                </a>
              ) : (
                <button onClick={onLogin || onStart} className={`w-full py-2.5 rounded-xl text-sm font-medium transition-all ${plan.popular ? "btn-primary" : "btn-primary !from-accent !to-accent"}`}>
                  {plan.kind === "free" ? (t("login.register_submit") || "Sign up") : t("nav.start")}
                </button>
              )}
            </div>
          ))}
        </div>
        <p className="text-center text-xs text-gray-600 mt-8">{t("landing.yt_addon")}</p>
      </section>

      {/* FAQ */}
      <section id="faq" className="relative z-10 py-20 px-6 max-w-3xl mx-auto scroll-mt-20">
        <h2 className="text-3xl font-bold text-center mb-16">{t("landing.faq")}</h2>
        <div className="space-y-4">
          {FAQS.map((faq, i) => (
            <details key={i} className="glass rounded-card group">
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

      {/* Contact / sales lead form */}
      <section id="contact" className="relative z-10 py-24 px-6 max-w-2xl mx-auto scroll-mt-20">
        <h2 className="text-3xl font-bold text-center mb-3">{t("landing.contact_title")}</h2>
        <p className="text-gray-400 text-center mb-4">{t("landing.contact_sub")}</p>
        <p className="text-center text-sm text-accent font-medium mb-10 max-w-lg mx-auto">{t("landing.contact_magnet")}</p>
        <form onSubmit={handleSalesSubmit} className="glass rounded-3xl p-8 space-y-4">
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <input name="name" required placeholder={t("landing.form_name")} className="bg-surface-1/60 border border-white/10 rounded-xl px-4 py-3 text-sm text-white placeholder-gray-500 focus:outline-none focus:border-brand/50" />
            <input name="company" placeholder={t("landing.form_company")} className="bg-surface-1/60 border border-white/10 rounded-xl px-4 py-3 text-sm text-white placeholder-gray-500 focus:outline-none focus:border-brand/50" />
          </div>
          <input name="email" type="email" required placeholder={t("landing.form_email")} className="w-full bg-surface-1/60 border border-white/10 rounded-xl px-4 py-3 text-sm text-white placeholder-gray-500 focus:outline-none focus:border-brand/50" />
          <div>
            <label className="block text-xs text-gray-500 mb-2">{t("landing.form_volume_label")}</label>
            <select name="volume" defaultValue={t("landing.form_volume_opt1")} className="w-full bg-surface-1/60 border border-white/10 rounded-xl px-4 py-3 text-sm text-white focus:outline-none focus:border-brand/50">
              <option>{t("landing.form_volume_opt1")}</option>
              <option>{t("landing.form_volume_opt2")}</option>
              <option>{t("landing.form_volume_opt3")}</option>
            </select>
          </div>
          <textarea name="message" rows="3" placeholder={t("landing.form_message")} className="w-full bg-surface-1/60 border border-white/10 rounded-xl px-4 py-3 text-sm text-white placeholder-gray-500 focus:outline-none focus:border-brand/50 resize-none" />
          <button type="submit" disabled={formState === "loading" || formState === "sent"} className="btn-primary w-full py-3 disabled:opacity-60">
            {formState === "loading" ? t("landing.form_sending") : t("landing.form_submit")}
          </button>
          {formState === "sent" && (
            <p className="text-center text-sm text-accent font-medium pt-1">{t("landing.form_sent")}</p>
          )}
          {formState === "error" && (
            <p className="text-center text-sm text-red-400 pt-1">{t("landing.form_error")}</p>
          )}
        </form>
        {/* TODO(ventas): cuando exista backend, postear a /api/leads en vez de mailto.
            Drop-in de prueba social real (logos/testimonios de sellos) iría arriba de este form. */}
      </section>

    </>
  );
}
