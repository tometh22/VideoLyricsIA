import { useState, useEffect } from "react";
import { useI18n } from "../i18n";

const API = import.meta.env.VITE_API_URL || "";

function authHeaders() {
  const token = localStorage.getItem("genly_token");
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function getUser() {
  try { return JSON.parse(localStorage.getItem("genly_user") || "null"); } catch { return null; }
}

const DEFAULT_SETTINGS = {
  titleFormat: "{artista} - {cancion} (Letra/Lyrics)",
  descriptionHeader: "",
  descriptionFooter: "",
  mandatoryTags: "",
  hashtags: "#lyrics #letra",
  metadataLanguage: "es",
  defaultPrivacy: "unlisted",
  channelName: "",
};

const PLAN_INFO = {
  free:        { label: "Free",        videos: 5,    price: 0,    color: "text-ink-secondary" },
  "100":       { label: "Plan 100",    videos: 100,  price: 900,  color: "text-brand-light" },
  "250":       { label: "Plan 250",    videos: 250,  price: 2000, color: "text-brand-light" },
  "500":       { label: "Plan 500",    videos: 500,  price: 3500, color: "text-brand-light" },
  "1000":      { label: "Plan 1000",   videos: 1000, price: 6000, color: "text-brand-light" },
  unlimited:   { label: "Unlimited",   videos: "∞",  price: 0,    color: "text-accent" },
};

function SectionLabel({ children }) {
  return (
    <p className="text-[10px] font-medium text-gray-500 uppercase tracking-[0.18em] mb-3">
      {children}
    </p>
  );
}

function Card({ children, className = "" }) {
  return (
    <div className={`rounded-card bg-surface-2/40 ring-1 ring-white/[0.04] p-6 ${className}`}>
      {children}
    </div>
  );
}

function Field({ label, help, children }) {
  return (
    <div>
      <label className="block text-xs font-medium text-ink-secondary mb-1.5">{label}</label>
      {children}
      {help && <p className="text-[10px] text-gray-600 mt-1.5">{help}</p>}
    </div>
  );
}

function TabPill({ active, onClick, children }) {
  return (
    <button
      onClick={onClick}
      className={`h-9 px-4 rounded-full text-xs font-medium transition-all ${
        active
          ? "bg-brand/15 text-brand-light ring-1 ring-brand/40"
          : "bg-surface-2/40 text-ink-secondary ring-1 ring-white/[0.04] hover:ring-white/[0.08] hover:text-white"
      }`}
    >
      {children}
    </button>
  );
}

export default function Settings({ onBack }) {
  const { t, lang, setLang } = useI18n();
  const [settings, setSettings] = useState(DEFAULT_SETTINGS);
  const [saved, setSaved] = useState(false);
  const [loading, setLoading] = useState(true);
  const user = getUser();

  const [subscription, setSubscription] = useState(null);
  const [invoices, setInvoices] = useState([]);
  const [billingLoading, setBillingLoading] = useState(false);
  const [activeSection, setActiveSection] = useState("youtube");

  useEffect(() => {
    fetch(`${API}/settings`, { headers: authHeaders() })
      .then((r) => r.json())
      .then((data) => { if (data && Object.keys(data).length) setSettings({ ...DEFAULT_SETTINGS, ...data }); })
      .catch(() => {})
      .finally(() => setLoading(false));

    fetch(`${API}/billing/subscription`, { headers: authHeaders() })
      .then((r) => r.json()).then(setSubscription).catch(() => {});

    fetch(`${API}/billing/invoices`, { headers: authHeaders() })
      .then((r) => r.json())
      .then((data) => { if (Array.isArray(data)) setInvoices(data); })
      .catch(() => {});
  }, []);

  const handleSave = async () => {
    try {
      await fetch(`${API}/settings`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify(settings),
      });
      setSaved(true);
      setTimeout(() => setSaved(false), 3000);
    } catch {}
  };

  const update = (key, value) => {
    setSettings((prev) => ({ ...prev, [key]: value }));
    setSaved(false);
  };

  const handleSubscribe = async (planId) => {
    setBillingLoading(true);
    try {
      const res = await fetch(`${API}/billing/checkout`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({ plan_id: planId }),
      });
      const data = await res.json();
      if (data.checkout_url) window.location.href = data.checkout_url;
    } catch {} finally { setBillingLoading(false); }
  };

  const handleManageBilling = async () => {
    setBillingLoading(true);
    try {
      const res = await fetch(`${API}/billing/portal`, { method: "POST", headers: authHeaders() });
      const data = await res.json();
      if (data.portal_url) window.location.href = data.portal_url;
    } catch {} finally { setBillingLoading(false); }
  };

  const currentPlan = user?.plan || "free";
  const planInfo = PLAN_INFO[currentPlan] || PLAN_INFO.free;

  if (loading) return (
    <div className="w-full max-w-2xl animate-fade-in space-y-4">
      {[1, 2, 3].map((i) => (
        <div key={i} className="rounded-card bg-surface-2/40 ring-1 ring-white/[0.04] p-6">
          <div className="h-3 w-32 bg-surface-3/40 rounded animate-pulse mb-4" />
          <div className="h-10 bg-surface-3/30 rounded-xl animate-pulse" />
        </div>
      ))}
    </div>
  );

  return (
    <div className="w-full max-w-2xl animate-fade-in">
      {/* ─── Header ─────────────────────────────────────────────── */}
      <div className="flex items-end gap-3 mb-8">
        <button onClick={onBack}
          className="w-9 h-9 rounded-xl bg-surface-2/40 ring-1 ring-white/[0.04] hover:ring-white/[0.08] hover:text-white flex items-center justify-center text-gray-400 transition-colors">
          <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
            <path d="M19 12H5M12 19l-7-7 7-7" />
          </svg>
        </button>
        <div>
          <h1 className="text-[28px] leading-tight font-bold tracking-tight">{t("settings.title")}</h1>
          <p className="text-sm text-ink-secondary mt-1">{t("settings.subtitle")}</p>
        </div>
      </div>

      {/* ─── Section tabs ─────────────────────────────────────────── */}
      <div className="flex flex-wrap gap-2 mb-6">
        {[
          { id: "youtube", label: "YouTube" },
          { id: "billing", label: t("settings.billing") || "Facturación" },
          { id: "account", label: t("settings.account") || "Cuenta" },
        ].map((s) => (
          <TabPill key={s.id} active={activeSection === s.id} onClick={() => setActiveSection(s.id)}>
            {s.label}
          </TabPill>
        ))}
      </div>

      <div className="space-y-4">

        {/* ════════════════════ BILLING ════════════════════ */}
        {activeSection === "billing" && (
          <>
            {/* Current plan */}
            <Card>
              <SectionLabel>{t("settings.current_plan") || "Plan actual"}</SectionLabel>
              <div className="flex items-end justify-between mb-4">
                <div>
                  <p className={`text-2xl font-bold tracking-tight ${planInfo.color}`}>{planInfo.label}</p>
                  <p className="text-xs text-ink-secondary mt-1">{planInfo.videos} videos/mes</p>
                </div>
                {planInfo.price > 0 && (
                  <p className="text-xl font-bold tracking-tight text-white">
                    <span className="text-xs text-ink-secondary font-normal">USD </span>
                    {planInfo.price.toLocaleString()}
                    <span className="text-xs text-ink-secondary font-normal">/mes</span>
                  </p>
                )}
              </div>
              {subscription?.subscription && (
                <div className="space-y-2 pt-4 border-t border-white/[0.04]">
                  <div className="flex justify-between text-xs">
                    <span className="text-ink-secondary">Estado</span>
                    <span className={`font-medium ${subscription.subscription.status === "active" ? "text-accent" : "text-amber-400"}`}>
                      {subscription.subscription.status}
                    </span>
                  </div>
                  {subscription.subscription.current_period_end && (
                    <div className="flex justify-between text-xs">
                      <span className="text-ink-secondary">{t("settings.next_billing") || "Próximo cobro"}</span>
                      <span className="text-gray-300">
                        {new Date(subscription.subscription.current_period_end * 1000).toLocaleDateString()}
                      </span>
                    </div>
                  )}
                  {subscription.subscription.cancel_at_period_end && (
                    <p className="text-xs text-amber-400 mt-2">
                      {t("settings.cancel_notice") || "Se cancela al final del período"}
                    </p>
                  )}
                </div>
              )}
              {subscription?.has_subscription && (
                <button onClick={handleManageBilling} disabled={billingLoading}
                  className="btn-secondary mt-5 text-xs h-10 px-4">
                  {t("settings.manage_billing") || "Administrar facturación"}
                </button>
              )}
            </Card>

            {/* Upgrade/downgrade */}
            {currentPlan !== "unlimited" && (
              <Card>
                <SectionLabel>{t("settings.change_plan") || "Cambiar plan"}</SectionLabel>
                <p className="text-xs text-ink-secondary mb-5 -mt-1">
                  {t("settings.change_plan_sub") || "Subí o bajá tu suscripción cuando quieras"}
                </p>
                <div className="grid grid-cols-2 gap-3">
                  {["100", "250", "500", "1000"].map((planId) => {
                    const p = PLAN_INFO[planId];
                    const isCurrent = currentPlan === planId;
                    return (
                      <button key={planId} onClick={() => !isCurrent && handleSubscribe(planId)}
                        disabled={isCurrent || billingLoading}
                        className={`rounded-card p-4 text-left transition-all ring-1 ${
                          isCurrent
                            ? "bg-brand/[0.08] ring-brand/30 cursor-default"
                            : "bg-surface-2/40 ring-white/[0.04] hover:ring-white/[0.10] hover:bg-surface-2/70"
                        }`}>
                        <p className="text-xs text-ink-secondary mb-1">{p.videos} videos/mes</p>
                        <p className="text-2xl font-bold tracking-tight text-white">
                          <span className="text-xs text-ink-secondary font-normal">$</span>
                          {p.price.toLocaleString()}
                        </p>
                        <p className="text-[10px] text-gray-600 mt-1">
                          ${(p.price / p.videos).toFixed(2)}/video
                        </p>
                        {isCurrent && (
                          <span className="inline-block mt-3 text-[9px] bg-brand/20 text-brand-light px-2 py-0.5 rounded-full font-bold uppercase tracking-wider">
                            {t("settings.current") || "Actual"}
                          </span>
                        )}
                      </button>
                    );
                  })}
                </div>
              </Card>
            )}

            {/* Invoice history */}
            <Card>
              <SectionLabel>{t("settings.invoice_history") || "Historial de facturas"}</SectionLabel>
              {invoices.length === 0 ? (
                <p className="text-sm text-ink-secondary text-center py-4">
                  {t("settings.no_invoices") || "Sin facturas aún"}
                </p>
              ) : (
                <div>
                  {invoices.map((inv) => (
                    <div key={inv.id} className="flex items-center justify-between py-3 border-b border-white/[0.03] last:border-0">
                      <div>
                        <p className="text-sm text-white">{inv.description || "Suscripción"}</p>
                        <p className="text-[11px] text-gray-600 mt-0.5">
                          {inv.created_at ? new Date(inv.created_at).toLocaleDateString() : "—"}
                        </p>
                      </div>
                      <div className="flex items-center gap-3">
                        <span className={`text-sm font-semibold tabular-nums ${inv.status === "paid" ? "text-accent" : "text-red-400"}`}>
                          ${inv.amount?.toFixed(2)}
                        </span>
                        {inv.invoice_url && (
                          <a href={inv.invoice_url} target="_blank" rel="noopener noreferrer"
                            className="text-xs text-brand-light hover:text-brand transition-colors">
                            {t("settings.view_invoice") || "Ver"}
                          </a>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </Card>
          </>
        )}

        {/* ════════════════════ YOUTUBE ════════════════════ */}
        {activeSection === "youtube" && (
          <>
            <Card>
              <SectionLabel>{t("settings.app_lang")}</SectionLabel>
              <div className="flex flex-wrap gap-2">
                {[
                  { code: "es", label: "Español" },
                  { code: "en", label: "English" },
                  { code: "pt", label: "Português" },
                ].map((l) => (
                  <button key={l.code} onClick={() => setLang(l.code)}
                    className={`h-9 px-4 rounded-full text-xs font-medium transition-all ${
                      lang === l.code
                        ? "bg-brand/15 text-brand-light ring-1 ring-brand/40"
                        : "bg-surface-3/40 text-ink-secondary ring-1 ring-white/[0.04] hover:ring-white/[0.08] hover:text-white"
                    }`}>
                    {l.label}
                  </button>
                ))}
              </div>
            </Card>

            <Card>
              <div className="flex items-center gap-2 mb-1">
                <SectionLabel>{t("settings.yt_template")}</SectionLabel>
              </div>
              <p className="text-xs text-ink-secondary mb-5 -mt-1">{t("settings.yt_template_sub")}</p>

              <div className="space-y-4">
                <Field label={t("settings.title_format")} help={t("settings.title_format_help")}>
                  <input type="text" value={settings.titleFormat}
                    onChange={(e) => update("titleFormat", e.target.value)}
                    className="input-field text-sm" placeholder="{artista} - {cancion} (Letra/Lyrics)" />
                </Field>
                <Field label={t("settings.desc_header")} help={t("settings.desc_header_help")}>
                  <textarea value={settings.descriptionHeader}
                    onChange={(e) => update("descriptionHeader", e.target.value)}
                    rows={3} className="input-field text-sm resize-none"
                    placeholder={t("settings.desc_header_placeholder")} />
                </Field>
                <Field label={t("settings.desc_footer")} help={t("settings.desc_footer_help")}>
                  <textarea value={settings.descriptionFooter}
                    onChange={(e) => update("descriptionFooter", e.target.value)}
                    rows={3} className="input-field text-sm resize-none"
                    placeholder={t("settings.desc_footer_placeholder")} />
                </Field>
                <Field label={t("settings.mandatory_tags")} help={t("settings.mandatory_tags_help")}>
                  <input type="text" value={settings.mandatoryTags}
                    onChange={(e) => update("mandatoryTags", e.target.value)}
                    className="input-field text-sm" placeholder="lyrics, letra, musica" />
                </Field>
                <Field label={t("settings.hashtags")}>
                  <input type="text" value={settings.hashtags}
                    onChange={(e) => update("hashtags", e.target.value)}
                    className="input-field text-sm" placeholder="#lyrics #letra #musica" />
                </Field>
                <div className="grid grid-cols-2 gap-3">
                  <Field label={t("settings.metadata_lang")}>
                    <select value={settings.metadataLanguage}
                      onChange={(e) => update("metadataLanguage", e.target.value)}
                      className="input-field text-sm appearance-none cursor-pointer">
                      <option value="es">{t("lang.es")}</option>
                      <option value="en">{t("lang.en")}</option>
                      <option value="pt">{t("lang.pt")}</option>
                      <option value="fr">{t("lang.fr")}</option>
                      <option value="it">{t("lang.it")}</option>
                      <option value="de">{t("lang.de")}</option>
                    </select>
                  </Field>
                  <Field label={t("settings.privacy")}>
                    <select value={settings.defaultPrivacy}
                      onChange={(e) => update("defaultPrivacy", e.target.value)}
                      className="input-field text-sm appearance-none cursor-pointer">
                      <option value="unlisted">{t("settings.privacy_unlisted")}</option>
                      <option value="private">{t("settings.privacy_private")}</option>
                      <option value="public">{t("settings.privacy_public")}</option>
                    </select>
                  </Field>
                </div>
              </div>
            </Card>

            <Card>
              <SectionLabel>{t("settings.channel")}</SectionLabel>
              <p className="text-xs text-ink-secondary mb-4 -mt-1">{t("settings.channel_sub")}</p>
              <Field label={t("settings.channel_name")}>
                <input type="text" value={settings.channelName}
                  onChange={(e) => update("channelName", e.target.value)}
                  className="input-field text-sm" placeholder={t("settings.channel_name")} />
              </Field>
              {settings.channelName && (
                <p className="text-[10px] text-gray-600 mt-2">{t("settings.channel_connected")}</p>
              )}
            </Card>

            <div className="flex items-center justify-end gap-3 pt-2">
              {saved && (
                <span className="text-xs text-accent flex items-center gap-1.5 animate-fade-in">
                  <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2.5" viewBox="0 0 24 24">
                    <polyline points="20 6 9 17 4 12"/>
                  </svg>
                  {t("settings.saved")}
                </span>
              )}
              <button onClick={handleSave} className="btn-primary px-6">
                {t("settings.save")}
              </button>
            </div>
          </>
        )}

        {/* ════════════════════ ACCOUNT ════════════════════ */}
        {activeSection === "account" && (
          <Card>
            <SectionLabel>{t("settings.account_info") || "Información de cuenta"}</SectionLabel>
            <div>
              {[
                { label: t("login.username"), value: user?.username },
                { label: "Email",             value: user?.email || "—" },
                { label: "Plan",              value: planInfo.label, valueClass: planInfo.color + " font-medium" },
                { label: "Rol",               value: user?.role || "user" },
              ].map((row, i, arr) => (
                <div key={row.label}
                  className={`flex items-center justify-between py-3 ${i < arr.length - 1 ? "border-b border-white/[0.03]" : ""}`}>
                  <span className="text-xs text-ink-secondary">{row.label}</span>
                  <span className={`text-sm ${row.valueClass || "text-white"}`}>{row.value}</span>
                </div>
              ))}
            </div>
          </Card>
        )}
      </div>
    </div>
  );
}
