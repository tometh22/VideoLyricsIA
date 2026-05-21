import { useNavigate } from "react-router-dom";

// Cinematic page header for marketing sub-pages: a real AI-generated background
// (full-bleed) under a dark overlay that fades into the page surface, with
// eyebrow + title + sub + optional CTA. Gives every sub-page the same premium,
// consistent feel as the Home hero.
export default function PageHero({ eyebrow, title, sub, ctaLabel, ctaTo = "/login", bg }) {
  const navigate = useNavigate();
  return (
    <section className="relative overflow-hidden">
      {bg && <img src={bg} alt="" className="absolute inset-0 w-full h-full object-cover" />}
      <div className="absolute inset-0 bg-black/55" />
      <div className="absolute inset-0 bg-gradient-to-t from-surface via-surface/40 to-transparent" />
      <div className="relative z-10 max-w-4xl mx-auto px-6 pt-36 pb-24 text-center">
        {eyebrow && <span className="text-[11px] uppercase tracking-[0.25em] text-brand-light">{eyebrow}</span>}
        <h1 className="text-4xl sm:text-6xl font-extrabold tracking-tight mt-3 mb-5 leading-[1.02] drop-shadow-[0_3px_30px_rgba(0,0,0,.7)]">{title}</h1>
        <p className="text-white/85 text-lg sm:text-xl max-w-2xl mx-auto leading-relaxed drop-shadow-[0_1px_18px_rgba(0,0,0,.8)]">{sub}</p>
        {ctaLabel && (
          <div className="mt-8">
            <button onClick={() => navigate(ctaTo)} className="px-9 py-4 rounded-full bg-white text-black font-bold text-lg hover:scale-[1.03] transition-transform shadow-glow-lg">
              {ctaLabel}
              <svg className="inline-block ml-2 w-5 h-5" fill="none" stroke="currentColor" strokeWidth="2.5" viewBox="0 0 24 24" strokeLinecap="round" strokeLinejoin="round"><path d="M5 12h14M12 5l7 7-7 7" /></svg>
            </button>
          </div>
        )}
      </div>
    </section>
  );
}
