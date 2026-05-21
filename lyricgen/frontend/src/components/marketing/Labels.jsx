import { useState } from "react";
import { useI18n } from "../../i18n";
import PageHero from "./PageHero";

const API = import.meta.env.VITE_API_URL || "";

// For Labels page (strategic — UMG-style): enterprise value, ownership/rights,
// and the sales lead form.
export default function Labels() {
  const { t } = useI18n();
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

  const feats = [
    { t: t("labels.f1_t"), d: t("labels.f1_d") },
    { t: t("labels.f2_t"), d: t("labels.f2_d") },
    { t: t("labels.f3_t"), d: t("labels.f3_d") },
    { t: t("labels.f4_t"), d: t("labels.f4_d") },
    { t: t("labels.f5_t"), d: t("labels.f5_d") },
    { t: t("labels.f6_t"), d: t("labels.f6_d") },
  ];

  return (
    <>
      <PageHero
        eyebrow={t("nav.for_labels")}
        title={t("labels.title")}
        sub={t("labels.sub")}
        bg="/samples/looks/look-stage.png"
      />
      <div className="px-6 pb-28 pt-20 max-w-5xl mx-auto">
        <div className="text-center mb-12">
          <a href="#contact" className="btn-primary text-lg py-4 px-10 inline-flex items-center justify-center">{t("labels.cta")}</a>
        </div>
      {/* Enterprise features */}
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

      {/* Ownership / rights */}
      <div className="mt-20 glass rounded-3xl p-10 border border-accent/20 shadow-glow text-center">
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

      {/* Lead form */}
      <div id="contact" className="mt-24 max-w-2xl mx-auto scroll-mt-24">
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
          {formState === "sent" && <p className="text-center text-sm text-accent font-medium pt-1">{t("landing.form_sent")}</p>}
          {formState === "error" && <p className="text-center text-sm text-red-400 pt-1">{t("landing.form_error")}</p>}
        </form>
      </div>
    </div>
    </>
  );
}
