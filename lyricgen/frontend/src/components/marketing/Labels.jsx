import { useNavigate } from "react-router-dom";
import { useI18n } from "../../i18n";

// For Labels page (strategic — UMG-style). Stage 1: enterprise value block +
// CTA to sales. Stage 2 will add the comparison table, ownership block and the
// full lead form here.
export default function Labels() {
  const { t } = useI18n();
  const navigate = useNavigate();
  const feats = [
    { t: t("labels.f1_t"), d: t("labels.f1_d") },
    { t: t("labels.f2_t"), d: t("labels.f2_d") },
    { t: t("labels.f3_t"), d: t("labels.f3_d") },
    { t: t("labels.f4_t"), d: t("labels.f4_d") },
    { t: t("labels.f5_t"), d: t("labels.f5_d") },
    { t: t("labels.f6_t"), d: t("labels.f6_d") },
  ];
  return (
    <div className="px-6 pt-24 pb-28 max-w-5xl mx-auto">
      <div className="text-center max-w-2xl mx-auto mb-14">
        <span className="text-[11px] uppercase tracking-[0.25em] text-brand-light">{t("nav.for_labels")}</span>
        <h1 className="text-4xl sm:text-5xl font-extrabold tracking-tight mt-3 mb-4">{t("labels.title")}</h1>
        <p className="text-gray-400 text-lg leading-relaxed mb-8">{t("labels.sub")}</p>
        <button onClick={() => navigate("/login")} className="btn-primary text-lg py-4 px-10">{t("labels.cta")}</button>
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-5">
        {feats.map((f) => (
          <div key={f.t} className="glass rounded-card p-6 flex gap-3 items-start">
            <svg className="w-5 h-5 shrink-0 text-accent mt-0.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24" strokeLinecap="round" strokeLinejoin="round"><path d="M9 12l2 2 4-4" /><circle cx="12" cy="12" r="10" /></svg>
            <div>
              <h3 className="font-semibold mb-1 text-sm">{f.t}</h3>
              <p className="text-xs text-gray-400 leading-relaxed">{f.d}</p>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
