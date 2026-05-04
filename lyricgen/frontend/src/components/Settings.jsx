import { useState, useEffect } from "react";
import { useI18n } from "../i18n";

const API = "";

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
  free: { label: "Free", videos: 5, price: 0, color: "text-gray-400" },
  "100": { label: "Plan 100", videos: 100, price: 900, color: "text-brand" },
  "250": { label: "Plan 250", videos: 250, price: 2000, color: "text-brand" },
  "500": { label: "Plan 500", videos: 500, price: 3500, color: "text-brand-light" },
  "1000": { label: "Plan 1000", videos: 1000, price: 6000, color: "text-brand-light" },
  unlimited: { label: "Unlimited", videos: "∞", price: 0, color: "text-accent" },
};

export default function Settings({ onBack }) {
  const { t, lang, setLang } = useI18n();
  const [settings, setSettings] = useState(DEFAULT_SETTINGS);
  const [saved, setSaved] = useState(false);
  const [loading, setLoading] = useState(true);
  const user = getUser();

  // Billing
  const [subscription, setSubscription] = useState(null);
  const [invoices, setInvoices] = useState([]);
  const [billingLoading, setBillingLoading] = useState(false);
  const [activeSection, setActiveSection] = useState("youtube"); // youtube, billing, account

  useEffect(() => {
    fetch(`${API}/settings`, { headers: authHeaders() })
      .then((r) => r.json())
      .then((data) => { if (data && Object.keys(data).length) setSettings({ ...DEFAULT_SETTINGS, ...data }); })
      .catch(() => {})
      .finally(() => setLoading(false));

    // Load billing info
    fetch(`${API}/billing/subscription`, { headers: authHeaders() })
      .then(r => r.json())
      .then(setSubscription)
      .catch(() => {});

    fetch(`${API}/billing/invoices`, { headers: authHeaders() })
      .then(r => r.json())
      .then(data => { if (Array.isArray(data)) setInvoices(data); })
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
      if (data.checkout_url) {
        window.location.href = data.checkout_url;
      }
    } catch {} finally {
      setBillingLoading(false);
    }
  };

  const handleManageBilling = async () => {
    setBillingLoading(true);
    try {
      const res = await fetch(`${API}/billing/portal`, {
        method: "POST",
        headers: authHeaders(),
      });
      const data = await res.json();
      if (data.portal_url) {
        window.location.href = data.portal_url;
      }
    } catch {} finally {
      setBillingLoading(false);
    }
  };

  const currentPlan = user?.plan || "free";
  const planInfo = PLAN_INFO[currentPlan] || PLAN_INFO.free;

  if (loading) return (
    <div className="w-full max-w-2xl animate-fade-in">
      <div className="space-y-6">
        {[1,2,3].map(i => (
          <div key={i} className="glass rounded-2xl p-6">
            <div className="h-5 w-40 bg-surface-3/30 rounded animate-pulse mb-4" />
            <div className="h-10 bg-surface-3/20 rounded-xl animate-pulse" />
          </div>
        ))}
      </div>
    </div>
  );

  return (
    <div className="w-full max-w-2xl animate-fade-in">
      <div className="flex items-center gap-3 mb-8">
        <button onClick={onBack}
          className="w-9 h-9 rounded-xl glass flex items-center justify-center text-gray-400 hover:text-white transition-colors">
          <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
            <path d="M19 12H5M12 19l-7-7 7-7" />
          </svg>
        </button>
        <div>
          <h1 className="text-2xl font-bold">{t("settings.title")}</h1>
          <p className="text-sm text-gray-500">{t("settings.subtitle")}</p>
        </div>
      </div>

      {/* Section tabs */}
      <div className="flex gap-1 mb-8 glass rounded-xl p-1 w-fit">
        {[
          { id: "youtube", label: "YouTube" },
          { id: "billing", label: t("settings.billing") || "Billing" },
          { id: "account", label: t("settings.account") || "Account" },
        ].map(s => (
          <button key={s.id} onClick={() => setActiveSection(s.id)}
            className={`px-5 py-2 rounded-lg text-sm font-medium transition-all ${
              activeSection === s.id ? "bg-brand text-white" : "text-gray-400 hover:text-white"
            }`}>
            {s.label}
          </button>
        ))}
      </div>

      <div className="space-y-6">

        {/* ======================== BILLING SECTION ======================== */}
        {activeSection === "billing" && (
          <>
            {/* Current plan */}
            <div className="glass rounded-2xl p-6">
              <h3 className="font-semibold mb-4">{t("settings.current_plan") || "Current Plan"}</h3>
              <div className="flex items-center justify-between mb-4">
                <div>
                  <p className={`text-2xl font-bold ${planInfo.color}`}>{planInfo.label}</p>
                  <p className="text-xs text-gray-500 mt-1">{planInfo.videos} videos/month</p>
                </div>
                {planInfo.price > 0 && (
                  <p className="text-xl font-bold">
                    <span className="text-sm text-gray-500">USD</span> {planInfo.price.toLocaleString()}
                    <span className="text-xs text-gray-500">/mo</span>
                  </p>
                )}
              </div>
              {subscription?.subscription && (
                <div className="space-y-2 pt-3 border-t border-white/[0.06]">
                  <div className="flex justify-between text-xs">
                    <span className="text-gray-400">Status</span>
                    <span className={`font-medium ${subscription.subscription.status === "active" ? "text-accent" : "text-amber-400"}`}>
                      {subscription.subscription.status}
                    </span>
                  </div>
                  {subscription.subscription.current_period_end && (
                    <div className="flex justify-between text-xs">
                      <span className="text-gray-400">{t("settings.next_billing") || "Next billing"}</span>
                      <span className="text-gray-300">
                        {new Date(subscription.subscription.current_period_end * 1000).toLocaleDateString()}
                      </span>
                    </div>
                  )}
                  {subscription.subscription.cancel_at_period_end && (
                    <p className="text-xs text-amber-400 mt-2">{t("settings.cancel_notice") || "Cancels at end of period"}</p>
                  )}
                </div>
              )}
              {subscription?.has_subscription && (
                <button onClick={handleManageBilling} disabled={billingLoading}
                  className="btn-secondary mt-4 text-sm !py-2.5">
                  {t("settings.manage_billing") || "Manage Billing"}
                </button>
              )}
            </div>

            {/* Upgrade/downgrade */}
            {currentPlan !== "unlimited" && (
              <div className="glass rounded-2xl p-6">
                <h3 className="font-semibold mb-1">{t("settings.change_plan") || "Change Plan"}</h3>
                <p className="text-xs text-gray-500 mb-5">{t("settings.change_plan_sub") || "Upgrade or downgrade your subscription"}</p>
                <div className="grid grid-cols-2 gap-3">
                  {["100", "250", "500", "1000"].map(planId => {
                    const p = PLAN_INFO[planId];
                    const isCurrent = currentPlan === planId;
                    return (
                      <button key={planId} onClick={() => !isCurrent && handleSubscribe(planId)}
                        disabled={isCurrent || billingLoading}
                        className={`rounded-2xl p-4 text-center transition-all ${
                          isCurrent
                            ? "glass border-brand/30 shadow-glow cursor-default"
                            : "glass glass-hover"
                        }`}>
                        <p className="text-sm font-bold mb-1">{p.videos} videos</p>
                        <p className="text-xl font-bold">
                          <span className="text-xs text-gray-500">$</span>{p.price.toLocaleString()}
                        </p>
                        <p className="text-[10px] text-gray-500 mt-1">
                          ${(p.price / p.videos).toFixed(2)}/video
                        </p>
                        {isCurrent && (
                          <span className="inline-block mt-2 text-[9px] bg-brand/20 text-brand px-2 py-0.5 rounded-full font-bold uppercase">
                            {t("settings.current") || "Current"}
                          </span>
                        )}
                      </button>
                    );
                  })}
                </div>
              </div>
            )}

            {/* Invoice history */}
            <div className="glass rounded-2xl p-6">
              <h3 className="font-semibold mb-4">{t("settings.invoice_history") || "Invoice History"}</h3>
              {invoices.length === 0 ? (
                <p className="text-sm text-gray-500 text-center py-4">{t("settings.no_invoices") || "No invoices yet"}</p>
              ) : (
                <div className="space-y-2">
                  {invoices.map(inv => (
                    <div key={inv.id} className="flex items-center justify-between py-2 border-b border-white/[0.03] last:border-0">
                      <div>
                        <p className="text-sm">{inv.description || "Subscription"}</p>
                        <p className="text-[11px] text-gray-500">
                          {inv.created_at ? new Date(inv.created_at).toLocaleDateString() : "—"}
                        </p>
                      </div>
                      <div className="flex items-center gap-3">
                        <span className={`text-sm font-medium ${inv.status === "paid" ? "text-accent" : "text-red-400"}`}>
                          ${inv.amount?.toFixed(2)}
                        </span>
                        {inv.invoice_url && (
                          <a href={inv.invoice_url} target="_blank" rel="noopener noreferrer"
                            className="text-xs text-brand hover:text-brand-light">
                            {t("settings.view_invoice") || "View"}
                          </a>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </>
        )}

        {/* ======================== YOUTUBE SECTION ======================== */}
        {activeSection === "youtube" && (
          <>
            {/* App Language */}
            <div className="glass rounded-2xl p-6">
              <h3 className="font-semibold mb-1">{t("settings.app_lang")}</h3>
              <p className="text-xs text-gray-500 mb-4">
                {lang === "es" ? "Cambia el idioma de toda la interfaz." :
                 lang === "en" ? "Change the language of the entire interface." :
                 "Mude o idioma de toda a interface."}
              </p>
              <div className="flex gap-2">
                {[
                  { code: "es", label: "Español" },
                  { code: "en", label: "English" },
                  { code: "pt", label: "Português" },
                ].map((l) => (
                  <button key={l.code} onClick={() => setLang(l.code)}
                    className={`px-4 py-2 rounded-xl text-sm font-medium transition-all ${
                      lang === l.code ? "bg-brand text-white shadow-glow" : "glass glass-hover text-gray-400"
                    }`}>
                    {l.label}
                  </button>
                ))}
              </div>
            </div>

            {/* YouTube Templates */}
            <div className="glass rounded-2xl p-6">
              <h3 className="font-semibold mb-1 flex items-center gap-2">
                <svg className="w-5 h-5 text-red-500" fill="currentColor" viewBox="0 0 24 24">
                  <path d="M22.54 6.42a2.78 2.78 0 00-1.94-2C18.88 4 12 4 12 4s-6.88 0-8.6.46a2.78 2.78 0 00-1.94 2A29 29 0 001 11.75a29 29 0 00.46 5.33A2.78 2.78 0 003.4 19.13C5.12 19.56 12 19.56 12 19.56s6.88 0 8.6-.46a2.78 2.78 0 001.94-2A29 29 0 0023 11.75a29 29 0 00-.46-5.33z"/><polygon points="9.75 15.02 15.5 11.75 9.75 8.48 9.75 15.02" fill="white"/>
                </svg>
                {t("settings.yt_template")}
              </h3>
              <p className="text-xs text-gray-500 mb-5">{t("settings.yt_template_sub")}</p>

              <div className="space-y-4">
                <div>
                  <label className="block text-xs font-medium text-gray-400 mb-1.5">{t("settings.title_format")}</label>
                  <input type="text" value={settings.titleFormat}
                    onChange={(e) => update("titleFormat", e.target.value)}
                    className="input-field text-sm" placeholder="{artista} - {cancion} (Letra/Lyrics)" />
                  <p className="text-[10px] text-gray-600 mt-1">{t("settings.title_format_help")}</p>
                </div>
                <div>
                  <label className="block text-xs font-medium text-gray-400 mb-1.5">{t("settings.desc_header")}</label>
                  <textarea value={settings.descriptionHeader}
                    onChange={(e) => update("descriptionHeader", e.target.value)}
                    rows={3} className="input-field text-sm resize-none"
                    placeholder={t("settings.desc_header_placeholder")} />
                  <p className="text-[10px] text-gray-600 mt-1">{t("settings.desc_header_help")}</p>
                </div>
                <div>
                  <label className="block text-xs font-medium text-gray-400 mb-1.5">{t("settings.desc_footer")}</label>
                  <textarea value={settings.descriptionFooter}
                    onChange={(e) => update("descriptionFooter", e.target.value)}
                    rows={3} className="input-field text-sm resize-none"
                    placeholder={t("settings.desc_footer_placeholder")} />
                  <p className="text-[10px] text-gray-600 mt-1">{t("settings.desc_footer_help")}</p>
                </div>
                <div>
                  <label className="block text-xs font-medium text-gray-400 mb-1.5">{t("settings.mandatory_tags")}</label>
                  <input type="text" value={settings.mandatoryTags}
                    onChange={(e) => update("mandatoryTags", e.target.value)}
                    className="input-field text-sm" placeholder={t("settings.mandatory_tags_help")} />
                </div>
                <div>
                  <label className="block text-xs font-medium text-gray-400 mb-1.5">{t("settings.hashtags")}</label>
                  <input type="text" value={settings.hashtags}
                    onChange={(e) => update("hashtags", e.target.value)}
                    className="input-field text-sm" placeholder="#lyrics #letra #musica" />
                </div>
                <div>
                  <label className="block text-xs font-medium text-gray-400 mb-1.5">{t("settings.metadata_lang")}</label>
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
                </div>
                <div>
                  <label className="block text-xs font-medium text-gray-400 mb-1.5">{t("settings.privacy")}</label>
                  <select value={settings.defaultPrivacy}
                    onChange={(e) => update("defaultPrivacy", e.target.value)}
                    className="input-field text-sm appearance-none cursor-pointer">
                    <option value="unlisted">{t("settings.privacy_unlisted")}</option>
                    <option value="private">{t("settings.privacy_private")}</option>
                    <option value="public">{t("settings.privacy_public")}</option>
                  </select>
                </div>
              </div>
            </div>

            {/* Channel info */}
            <div className="glass rounded-2xl p-6">
              <h3 className="font-semibold mb-1">{t("settings.channel")}</h3>
              <p className="text-xs text-gray-500 mb-4">{t("settings.channel_sub")}</p>
              <div>
                <label className="block text-xs font-medium text-gray-400 mb-1.5">{t("settings.channel_name")}</label>
                <input type="text" value={settings.channelName}
                  onChange={(e) => update("channelName", e.target.value)}
                  className="input-field text-sm" placeholder={t("settings.channel_name")} />
              </div>
              {settings.channelName && (
                <p className="text-[10px] text-gray-600 mt-2">{t("settings.channel_connected")}</p>
              )}
            </div>

            {/* Save */}
            <div className="flex items-center gap-4">
              <button onClick={handleSave} className="btn-primary py-3 px-8">
                {t("settings.save")}
              </button>
              {saved && (
                <span className="text-sm text-accent animate-fade-in flex items-center gap-1.5">
                  <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="2.5" viewBox="0 0 24 24">
                    <polyline points="20 6 9 17 4 12"/>
                  </svg>
                  {t("settings.saved")}
                </span>
              )}
            </div>
          </>
        )}

        {/* ======================== ACCOUNT SECTION ======================== */}
        {activeSection === "account" && (
          <>
            <div className="glass rounded-2xl p-6">
              <h3 className="font-semibold mb-4">{t("settings.account_info") || "Account Info"}</h3>
              <div className="space-y-3">
                <div className="flex items-center justify-between py-2 border-b border-white/[0.03]">
                  <span className="text-xs text-gray-400">{t("login.username")}</span>
                  <span className="text-sm font-medium">{user?.username}</span>
                </div>
                <div className="flex items-center justify-between py-2 border-b border-white/[0.03]">
                  <span className="text-xs text-gray-400">Email</span>
                  <span className="text-sm text-gray-300">{user?.email || "—"}</span>
                </div>
                <div className="flex items-center justify-between py-2 border-b border-white/[0.03]">
                  <span className="text-xs text-gray-400">Plan</span>
                  <span className={`text-sm font-medium ${planInfo.color}`}>{planInfo.label}</span>
                </div>
                <div className="flex items-center justify-between py-2">
                  <span className="text-xs text-gray-400">Role</span>
                  <span className="text-sm text-gray-300">{user?.role || "user"}</span>
                </div>
              </div>
            </div>

            <div className="glass rounded-2xl p-6 mt-4">
              <div className="flex items-center justify-between gap-4">
                <div>
                  <h3 className="font-semibold mb-1">{t("tour.replay")}</h3>
                  <p className="text-xs text-gray-500">{t("tour.replay_sub")}</p>
                </div>
                <button
                  onClick={() => {
                    localStorage.removeItem("genly_onboarding_done");
                    window.dispatchEvent(new CustomEvent("genly:replay-tour"));
                    onBack?.();
                  }}
                  className="btn-secondary text-sm px-4 py-2 shrink-0"
                >
                  {t("tour.replay")}
                </button>
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
