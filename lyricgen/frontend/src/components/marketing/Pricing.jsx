import { useNavigate } from "react-router-dom";
import { useI18n } from "../../i18n";
import PageHero from "./PageHero";

// Pricing page: cinematic hero + plans + comparison table + FAQ.
export default function Pricing() {
  const { t } = useI18n();
  const navigate = useNavigate();

  const plans = [
    { kind: "free", name: t("landing.plan_free"), desc: t("landing.plan_free_desc"), price: "0" },
    { kind: "indie", name: t("landing.plan_indie"), desc: t("landing.plan_indie_desc"), price: t("landing.plan_indie_price"), popular: true },
    { kind: "label", name: t("landing.plan_label"), desc: t("landing.plan_label_desc"), price: t("landing.plan_label_price"), contact: true },
  ];
  const rows = [
    { l: t("cmp.r_price"), g: t("cmp.r_price_g"), s: t("cmp.r_price_s"), o: t("cmp.r_price_t") },
    { l: t("cmp.r_time"), g: t("cmp.r_time_g"), s: t("cmp.r_time_s"), o: t("cmp.r_time_t") },
    { l: t("cmp.r_bg"), g: "yes", s: "no", o: "partial" },
    { l: t("cmp.r_rights"), g: "yes", s: "no", o: "partial" },
    { l: t("cmp.r_prores"), g: "yes", s: "yes", o: "no" },
    { l: t("cmp.r_batch"), g: "yes", s: "no", o: "no" },
    { l: t("cmp.r_langs"), g: "yes", s: "partial", o: "partial" },
  ];
  const cell = (v) =>
    v === "yes" ? <span className="text-accent font-bold">✓</span>
    : v === "no" ? <span className="text-gray-600">–</span>
    : v === "partial" ? <span className="text-gray-500">{t("cmp.partial")}</span>
    : <span>{v}</span>;
  const faqs = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10].map((i) => ({ q: t(`faq.q${i}`), a: t(`faq.a${i}`) }));

  return (
    <>
      <PageHero
        title={t("landing.pricing")}
        sub={t("landing.pricing_sub")}
        bg="/samples/looks/look-vinyl.png"
      />
      <div className="px-6 pt-20 pb-28 max-w-5xl mx-auto">
      <p className="text-center text-sm text-accent font-medium max-w-xl mx-auto mb-12">{t("landing.pricing_scale")}</p>
      <div className="grid grid-cols-1 md:grid-cols-3 gap-5 max-w-4xl mx-auto">
        {plans.map((plan) => (
          <div key={plan.kind} className={`glass rounded-3xl p-7 text-center relative flex flex-col ${plan.popular ? "border-brand/30 shadow-glow" : ""}`}>
            {plan.popular && <div className="absolute -top-3 left-1/2 -translate-x-1/2 px-3 py-0.5 rounded-full bg-brand text-[10px] font-bold uppercase tracking-wider">{t("landing.popular")}</div>}
            <h3 className="text-base font-semibold mb-2">{plan.name}</h3>
            <p className="text-3xl font-bold my-2">
              {plan.kind === "free" ? <span className="text-accent">Free</span>
                : plan.contact ? <span className="text-2xl">{plan.price}</span>
                : <>{plan.price}<span className="text-sm text-gray-500"> / {t("landing.per_video")}</span></>}
            </p>
            <p className="text-xs text-gray-400 leading-relaxed mb-6 flex-1">{plan.desc}</p>
            {plan.contact ? (
              <button onClick={() => navigate("/sellos")} className="btn-secondary w-full py-2.5 rounded-xl text-sm font-medium">{t("landing.plan_contact_cta")}</button>
            ) : (
              <button onClick={() => navigate("/login")} className={`w-full py-2.5 rounded-xl text-sm font-medium transition-all ${plan.popular ? "btn-primary" : "btn-primary !from-accent !to-accent"}`}>
                {plan.kind === "free" ? (t("login.register_submit") || "Sign up") : t("nav.start")}
              </button>
            )}
          </div>
        ))}
      </div>
      <p className="text-center text-xs text-gray-600 mt-8">{t("landing.yt_addon")}</p>

      {/* Comparison */}
      <div className="mt-24">
        <h2 className="text-3xl font-bold text-center mb-4">{t("cmp.title")}</h2>
        <p className="text-gray-500 text-center mb-12 max-w-md mx-auto">{t("cmp.sub")}</p>
        <div className="glass rounded-3xl overflow-hidden text-sm max-w-3xl mx-auto">
          <div className="grid grid-cols-[1.4fr_1fr_1fr_1fr] text-center border-b border-white/[0.06]">
            <div className="p-4" />
            <div className="p-4 font-bold text-brand-light bg-brand/10">{t("cmp.genly")}</div>
            <div className="p-4 text-xs text-gray-400">{t("cmp.studio")}</div>
            <div className="p-4 text-xs text-gray-400">{t("cmp.tools")}</div>
          </div>
          {rows.map((row, i) => (
            <div key={i} className="grid grid-cols-[1.4fr_1fr_1fr_1fr] text-center items-center border-b border-white/[0.04] last:border-0">
              <div className="p-4 text-left text-gray-300">{row.l}</div>
              <div className="p-4 bg-brand/[0.06] font-medium text-white">{cell(row.g)}</div>
              <div className="p-4 text-gray-500">{cell(row.s)}</div>
              <div className="p-4 text-gray-500">{cell(row.o)}</div>
            </div>
          ))}
        </div>
      </div>

      {/* FAQ */}
      <div id="faq" className="mt-24 max-w-3xl mx-auto scroll-mt-24">
        <h2 className="text-3xl font-bold text-center mb-12">{t("landing.faq")}</h2>
        <div className="space-y-4">
          {faqs.map((faq, i) => (
            <details key={i} className="glass rounded-card group">
              <summary className="px-6 py-4 cursor-pointer text-sm font-medium text-white flex items-center justify-between list-none">
                {faq.q}
                <svg className="w-4 h-4 text-gray-500 group-open:rotate-180 transition-transform" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24"><path d="M6 9l6 6 6-6"/></svg>
              </summary>
              <div className="px-6 pb-4 text-sm text-gray-400 leading-relaxed">{faq.a}</div>
            </details>
          ))}
        </div>
      </div>
    </div>
    </>
  );
}
