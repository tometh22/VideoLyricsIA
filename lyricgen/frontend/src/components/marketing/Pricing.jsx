import { useNavigate } from "react-router-dom";
import { useI18n } from "../../i18n";

// Pricing page. Stage 1: the 3 plans + scale note. Stage 2 will move the
// comparison table and FAQ here.
export default function Pricing() {
  const { t } = useI18n();
  const navigate = useNavigate();
  const plans = [
    { kind: "free", name: t("landing.plan_free"), desc: t("landing.plan_free_desc"), price: "0" },
    { kind: "indie", name: t("landing.plan_indie"), desc: t("landing.plan_indie_desc"), price: t("landing.plan_indie_price"), popular: true },
    { kind: "label", name: t("landing.plan_label"), desc: t("landing.plan_label_desc"), price: t("landing.plan_label_price"), contact: true },
  ];
  return (
    <div className="px-6 pt-24 pb-28 max-w-5xl mx-auto">
      <div className="text-center mb-12">
        <h1 className="text-4xl sm:text-5xl font-extrabold tracking-tight mb-4">{t("landing.pricing")}</h1>
        <p className="text-gray-500 max-w-md mx-auto mb-3">{t("landing.pricing_sub")}</p>
        <p className="text-sm text-accent font-medium max-w-xl mx-auto">{t("landing.pricing_scale")}</p>
      </div>
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
    </div>
  );
}
