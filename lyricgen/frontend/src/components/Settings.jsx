import { useState, useEffect } from "react";
import { useI18n } from "../i18n";
import { startReplaySession } from "./OnboardingTour";
import DriveConnectButton from "./DriveConnectButton";

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
  channels: [],
  notif_quota_80: true,
  notif_quota_100: true,
  notif_billing: true,
  notif_jobs: false,
};

const PLAN_INFO = {
  free:      { label: "Free",      videos: 5,    price: 0,    color: "text-ink-secondary" },
  "100":     { label: "Plan 100",  videos: 100,  price: 900,  color: "text-brand-light" },
  "250":     { label: "Plan 250",  videos: 250,  price: 2000, color: "text-brand-light" },
  "500":     { label: "Plan 500",  videos: 500,  price: 3500, color: "text-brand-light" },
  "1000":    { label: "Plan 1000", videos: 1000, price: 6000, color: "text-brand-light" },
  unlimited: { label: "Unlimited", videos: "∞",  price: 0,    color: "text-accent" },
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

function InlineSuccess({ message }) {
  return (
    <span className="text-xs text-accent flex items-center gap-1.5 animate-fade-in">
      <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2.5" viewBox="0 0 24 24">
        <polyline points="20 6 9 17 4 12" />
      </svg>
      {message}
    </span>
  );
}

function InlineError({ message }) {
  return (
    <p className="text-xs text-red-400 flex items-center gap-1.5">
      <svg className="w-3.5 h-3.5 shrink-0" fill="none" stroke="currentColor" strokeWidth="2.5" viewBox="0 0 24 24">
        <circle cx="12" cy="12" r="10" /><line x1="12" y1="8" x2="12" y2="12" /><line x1="12" y1="16" x2="12.01" y2="16" />
      </svg>
      {message}
    </p>
  );
}

function UsageBar({ percent, alert80, alert100 }) {
  const color = alert100 ? "bg-red-500" : alert80 ? "bg-amber-400" : "bg-brand";
  return (
    <div className="h-1.5 w-full rounded-full bg-surface-3/40 overflow-hidden">
      <div
        className={`h-full rounded-full transition-all duration-700 ${color}`}
        style={{ width: `${Math.min(percent, 100)}%` }}
      />
    </div>
  );
}

function Toggle({ value, onChange }) {
  return (
    <button
      role="switch"
      aria-checked={value}
      onClick={() => onChange(!value)}
      className={`relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors ${
        value ? "bg-brand" : "bg-surface-3/60 ring-1 ring-white/[0.08]"
      }`}
    >
      <span className={`inline-block h-3.5 w-3.5 transform rounded-full bg-white shadow-sm transition-transform ${
        value ? "translate-x-[18px]" : "translate-x-0.5"
      }`} />
    </button>
  );
}

function AlertBanner({ variant, children }) {
  const styles = {
    amber: "bg-amber-500/[0.06] ring-amber-500/15 text-amber-400",
    red:   "bg-red-500/[0.06] ring-red-500/15 text-red-400",
  };
  return (
    <div className={`mt-3 flex items-start gap-2 p-3 rounded-xl ring-1 ${styles[variant]}`}>
      <svg className="w-4 h-4 shrink-0 mt-0.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
        <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
        <line x1="12" y1="9" x2="12" y2="13" /><line x1="12" y1="17" x2="12.01" y2="17" />
      </svg>
      <div>{children}</div>
    </div>
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
  const [usage, setUsage] = useState(null);
  const [billingLoading, setBillingLoading] = useState(false);
  const [activeSection, setActiveSection] = useState("cuenta");

  // Change password
  const [pwCurrent, setPwCurrent] = useState("");
  const [pwNew, setPwNew] = useState("");
  const [pwConfirm, setPwConfirm] = useState("");
  const [pwLoading, setPwLoading] = useState(false);
  const [pwSuccess, setPwSuccess] = useState(false);
  const [pwError, setPwError] = useState("");

  // Delete account
  const [showDelete, setShowDelete] = useState(false);
  const [deletePassword, setDeletePassword] = useState("");
  const [deleteLoading, setDeleteLoading] = useState(false);
  const [deleteError, setDeleteError] = useState("");

  // Data export
  const [exportLoading, setExportLoading] = useState(false);

  // API keys
  const [apiKeys, setApiKeys] = useState([]);
  const [apiKeyName, setApiKeyName] = useState("");
  const [apiKeyCreating, setApiKeyCreating] = useState(false);
  const [newKeySecret, setNewKeySecret] = useState(null);
  const [keyCopied, setKeyCopied] = useState(false);
  const [revokingId, setRevokingId] = useState(null);

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

    fetch(`${API}/usage`, { headers: authHeaders() })
      .then((r) => r.json()).then(setUsage).catch(() => {});

    fetch(`${API}/auth/api-keys`, { headers: authHeaders() })
      .then((r) => r.json())
      .then((data) => { if (Array.isArray(data)) setApiKeys(data); })
      .catch(() => {});
  }, []);

  const [saveError, setSaveError] = useState(null);
  const handleSave = async () => {
    setSaveError(null);
    try {
      const res = await fetch(`${API}/settings`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify(settings),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || `Error ${res.status}`);
      }
      setSaved(true);
      setTimeout(() => setSaved(false), 3000);
    } catch (err) {
      setSaveError(err.message || String(err));
      setTimeout(() => setSaveError(null), 6000);
    }
  };

  const update = (key, value) => {
    setSettings((prev) => ({ ...prev, [key]: value }));
    setSaved(false);
  };

  const [billingError, setBillingError] = useState(null);
  const handleSubscribe = async (planId) => {
    setBillingLoading(true);
    setBillingError(null);
    try {
      const res = await fetch(`${API}/billing/checkout`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({ plan_id: planId }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || `Error ${res.status}`);
      }
      const data = await res.json();
      if (data.checkout_url) window.location.href = data.checkout_url;
    } catch (err) {
      setBillingError(err.message || String(err));
    } finally { setBillingLoading(false); }
  };

  const handleManageBilling = async () => {
    setBillingLoading(true);
    setBillingError(null);
    try {
      const res = await fetch(`${API}/billing/portal`, { method: "POST", headers: authHeaders() });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || `Error ${res.status}`);
      }
      const data = await res.json();
      if (data.portal_url) window.location.href = data.portal_url;
    } catch (err) {
      setBillingError(err.message || String(err));
    } finally { setBillingLoading(false); }
  };

  const handleChangePassword = async () => {
    setPwError("");
    if (pwNew !== pwConfirm) {
      setPwError(t("settings.password_error_match"));
      return;
    }
    if (pwNew.length < 8) {
      setPwError(t("settings.password_error_strength"));
      return;
    }
    setPwLoading(true);
    try {
      const res = await fetch(`${API}/auth/change-password`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({ current_password: pwCurrent, new_password: pwNew }),
      });
      const data = await res.json();
      if (!res.ok) {
        setPwError(data.detail || t("settings.password_error_current"));
      } else {
        setPwSuccess(true);
        setPwCurrent(""); setPwNew(""); setPwConfirm("");
        setTimeout(() => setPwSuccess(false), 4000);
      }
    } catch {
      setPwError(t("settings.password_error_current"));
    } finally {
      setPwLoading(false);
    }
  };

  const handleExportData = async () => {
    setExportLoading(true);
    try {
      const res = await fetch(`${API}/auth/data-export`, { headers: authHeaders() });
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "genly-data-export.json";
      a.click();
      URL.revokeObjectURL(url);
    } catch {} finally {
      setExportLoading(false);
    }
  };

  const handleDeleteAccount = async () => {
    setDeleteError("");
    setDeleteLoading(true);
    try {
      const res = await fetch(`${API}/auth/account`, {
        method: "DELETE",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({ password: deletePassword }),
      });
      if (!res.ok) {
        const data = await res.json();
        setDeleteError(data.detail || t("settings.delete_error"));
      } else {
        localStorage.removeItem("genly_token");
        localStorage.removeItem("genly_user");
        window.location.assign("/");
      }
    } catch {
      setDeleteError(t("settings.delete_error"));
    } finally {
      setDeleteLoading(false);
    }
  };

  const toggleNotif = async (key) => {
    const prev = settings;
    const next = { ...settings, [key]: !settings[key] };
    setSettings(next);
    try {
      const res = await fetch(`${API}/settings`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify(next),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || `Error ${res.status}`);
      }
    } catch (err) {
      // Revertir optimistic update: si el server rechazó, el toggle
      // local no debería quedarse mostrando el nuevo estado.
      setSettings(prev);
      setSaveError(`Toggle de notificación falló: ${err.message || err}`);
      setTimeout(() => setSaveError(null), 6000);
    }
  };

  const handleCreateApiKey = async () => {
    if (!apiKeyName.trim()) return;
    setApiKeyCreating(true);
    try {
      const res = await fetch(`${API}/auth/api-keys`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({ name: apiKeyName.trim() }),
      });
      const data = await res.json();
      if (!res.ok) return;
      setNewKeySecret(data.key);
      setApiKeys((prev) => [{ id: data.id, name: data.name, prefix: data.prefix, created_at: data.created_at, last_used_at: null }, ...prev]);
      setApiKeyName("");
    } catch {} finally {
      setApiKeyCreating(false);
    }
  };

  const handleRevokeApiKey = async (keyId) => {
    setRevokingId(keyId);
    try {
      const res = await fetch(`${API}/auth/api-keys/${keyId}`, {
        method: "DELETE",
        headers: authHeaders(),
      });
      if (res.ok) setApiKeys((prev) => prev.filter((k) => k.id !== keyId));
    } catch {} finally {
      setRevokingId(null);
    }
  };

  const handleCopyKey = async (key) => {
    try {
      await navigator.clipboard.writeText(key);
      setKeyCopied(true);
      setTimeout(() => setKeyCopied(false), 2500);
    } catch {}
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
      {/* ─── Header ───────────────────────────────────────────────── */}
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

      {/* ─── Tabs ─────────────────────────────────────────────────── */}
      <div className="flex flex-wrap gap-2 mb-6">
        {[
          { id: "cuenta",         label: t("settings.account") || "Cuenta" },
          { id: "facturacion",    label: t("settings.billing") || "Facturación" },
          // Integraciones por ahora sólo contiene Drive — escondemos
          // toda la tab cuando el feature flag está off (canary mode).
          user?.features?.drive_export
            ? { id: "integraciones", label: t("settings.integrations_tab") || "Integraciones" }
            : null,
          { id: "youtube",        label: "YouTube" },
        ].filter(Boolean).map((s) => (
          <TabPill key={s.id} active={activeSection === s.id} onClick={() => setActiveSection(s.id)}>
            {s.label}
          </TabPill>
        ))}
      </div>

      <div className="space-y-4">

        {/* ════════════════════ CUENTA ════════════════════ */}
        {activeSection === "cuenta" && (
          <>
            {/* Account info */}
            <Card>
              <SectionLabel>{t("settings.account_info") || "Información de cuenta"}</SectionLabel>
              <div>
                {[
                  { label: t("login.username"),                           value: user?.username },
                  { label: "Email",                                       value: user?.email || "—" },
                  { label: t("settings.current_plan") || "Plan",         value: planInfo.label, valueClass: planInfo.color + " font-medium" },
                  { label: "Rol",                                         value: user?.role || "user" },
                ].map((row, i, arr) => (
                  <div key={row.label}
                    className={`flex items-center justify-between py-3 ${i < arr.length - 1 ? "border-b border-white/[0.03]" : ""}`}>
                    <span className="text-xs text-ink-secondary">{row.label}</span>
                    <span className={`text-sm truncate max-w-[60%] text-right ${row.valueClass || "text-white"}`}>{row.value}</span>
                  </div>
                ))}
              </div>
            </Card>

            {/* Notifications */}
            <Card>
              <SectionLabel>{t("settings.notifications") || "Notificaciones por email"}</SectionLabel>
              <p className="text-xs text-ink-secondary mb-4 -mt-1">
                {t("settings.notifications_sub") || "Elegí qué alertas recibir en tu casilla."}
              </p>
              <div className="space-y-0">
                {[
                  { key: "notif_quota_80",  label: t("settings.notif_quota_80"),  sub: t("settings.notif_quota_80_sub") },
                  { key: "notif_quota_100", label: t("settings.notif_quota_100"), sub: t("settings.notif_quota_100_sub") },
                  { key: "notif_billing",   label: t("settings.notif_billing"),   sub: t("settings.notif_billing_sub") },
                  { key: "notif_jobs",      label: t("settings.notif_jobs"),      sub: t("settings.notif_jobs_sub") },
                ].map(({ key, label, sub }, i, arr) => (
                  <div key={key}
                    className={`flex items-center justify-between gap-4 py-3 ${i < arr.length - 1 ? "border-b border-white/[0.03]" : ""}`}>
                    <div>
                      <p className="text-sm text-white">{label}</p>
                      <p className="text-xs text-ink-secondary mt-0.5">{sub}</p>
                    </div>
                    <Toggle value={!!settings[key]} onChange={() => toggleNotif(key)} />
                  </div>
                ))}
              </div>
            </Card>

            {/* API Keys */}
            <Card>
              <SectionLabel>{t("settings.api_keys") || "API Keys"}</SectionLabel>
              <p className="text-xs text-ink-secondary mb-4 -mt-1">
                {t("settings.api_keys_sub") || "Tokens de acceso para integraciones externas."}
              </p>

              {/* New key disclosed once */}
              {newKeySecret && (
                <div className="mb-4 p-3 rounded-xl bg-accent/[0.06] ring-1 ring-accent/20 animate-fade-in">
                  <p className="text-xs font-medium text-accent mb-2">
                    {t("settings.api_key_created_hint") || "Guardá esta clave ahora. No se mostrará de nuevo."}
                  </p>
                  <div className="flex items-center gap-2">
                    <code className="flex-1 text-[11px] font-mono text-white bg-surface-3/50 px-3 py-2 rounded-lg truncate">
                      {newKeySecret}
                    </code>
                    <button onClick={() => handleCopyKey(newKeySecret)}
                      className="shrink-0 text-xs font-medium px-3 py-2 rounded-lg bg-accent/15 text-accent ring-1 ring-accent/30 hover:bg-accent/25 transition-colors">
                      {keyCopied ? (t("settings.api_key_copied") || "Copiado") : (t("settings.api_key_copy") || "Copiar")}
                    </button>
                  </div>
                  <button onClick={() => setNewKeySecret(null)}
                    className="mt-2 text-[11px] text-ink-secondary hover:text-white transition-colors">
                    × {t("settings.cancel") || "Cerrar"}
                  </button>
                </div>
              )}

              {/* Existing keys */}
              {apiKeys.length === 0 && !newKeySecret ? (
                <p className="text-xs text-ink-secondary mb-4">
                  {t("settings.api_key_no_keys") || "Sin API keys."}
                </p>
              ) : (
                <div className="mb-4 space-y-0">
                  {apiKeys.map((k, i) => (
                    <div key={k.id}
                      className={`flex items-center justify-between gap-3 py-2.5 ${i < apiKeys.length - 1 ? "border-b border-white/[0.03]" : ""}`}>
                      <div className="min-w-0">
                        <p className="text-sm text-white truncate">{k.name}</p>
                        <p className="text-[11px] text-gray-600 font-mono mt-0.5">{k.prefix}•••</p>
                        <p className="text-[10px] text-gray-600 mt-0.5">
                          {t("settings.api_key_last_used") || "Último uso"}:{" "}
                          {k.last_used_at
                            ? new Date(k.last_used_at).toLocaleDateString()
                            : (t("settings.api_key_never_used") || "Nunca usado")}
                        </p>
                      </div>
                      <button
                        onClick={() => handleRevokeApiKey(k.id)}
                        disabled={revokingId === k.id}
                        className="shrink-0 text-[11px] font-medium px-3 py-1.5 rounded-lg bg-red-500/10 text-red-400 ring-1 ring-red-500/15 hover:bg-red-500/20 transition-colors disabled:opacity-40">
                        {revokingId === k.id ? "…" : (t("settings.api_key_revoke") || "Revocar")}
                      </button>
                    </div>
                  ))}
                </div>
              )}

              {/* Create new key */}
              {apiKeys.length < 10 && (
                <div className="flex gap-2 pt-2 border-t border-white/[0.03]">
                  <input
                    type="text"
                    value={apiKeyName}
                    onChange={(e) => setApiKeyName(e.target.value)}
                    onKeyDown={(e) => e.key === "Enter" && handleCreateApiKey()}
                    placeholder={t("settings.api_key_new_name") || "Nombre de la integración"}
                    className="input-field text-sm flex-1 h-9"
                  />
                  <button
                    onClick={handleCreateApiKey}
                    disabled={apiKeyCreating || !apiKeyName.trim()}
                    className="shrink-0 btn-primary text-xs h-9 px-4 disabled:opacity-40 disabled:cursor-not-allowed">
                    {apiKeyCreating ? "…" : (t("settings.api_key_create") || "Crear")}
                  </button>
                </div>
              )}
              {apiKeys.length >= 10 && (
                <p className="text-[11px] text-ink-secondary pt-2 border-t border-white/[0.03]">
                  {t("settings.api_key_limit") || "Máx 10 por cuenta"}
                </p>
              )}
            </Card>

            {/* Change password */}
            <Card>
              <SectionLabel>{t("settings.change_password") || "Cambiar contraseña"}</SectionLabel>
              <p className="text-xs text-ink-secondary mb-4 -mt-1">
                {t("settings.change_password_sub") || "Elegí una contraseña nueva para tu cuenta."}
              </p>
              <div className="space-y-3">
                <Field label={t("settings.current_password") || "Contraseña actual"}>
                  <input type="password" value={pwCurrent}
                    onChange={(e) => { setPwCurrent(e.target.value); setPwError(""); }}
                    className="input-field text-sm" autoComplete="current-password" />
                </Field>
                <Field label={t("settings.new_password") || "Nueva contraseña"}>
                  <input type="password" value={pwNew}
                    onChange={(e) => { setPwNew(e.target.value); setPwError(""); }}
                    className="input-field text-sm" autoComplete="new-password" />
                </Field>
                <Field label={t("settings.confirm_new_password") || "Confirmar nueva contraseña"}>
                  <input type="password" value={pwConfirm}
                    onChange={(e) => { setPwConfirm(e.target.value); setPwError(""); }}
                    className="input-field text-sm" autoComplete="new-password" />
                </Field>
              </div>
              {pwError && <div className="mt-3"><InlineError message={pwError} /></div>}
              <div className="flex items-center justify-end gap-3 mt-4">
                {pwSuccess && <InlineSuccess message={t("settings.password_updated") || "Contraseña actualizada"} />}
                <button onClick={handleChangePassword}
                  disabled={pwLoading || !pwCurrent || !pwNew || !pwConfirm}
                  className="btn-primary px-5 disabled:opacity-40 disabled:cursor-not-allowed">
                  {pwLoading ? "…" : (t("settings.update_password") || "Actualizar contraseña")}
                </button>
              </div>
            </Card>

            {/* Guided tour */}
            <Card>
              <SectionLabel>{t("settings.tour_label") || "Tour guiado"}</SectionLabel>
              <div className="flex items-center justify-between gap-4 py-1">
                <div className="min-w-0">
                  <p className="text-sm text-white">{t("settings.tour_replay_title") || "Volver a ver el tour"}</p>
                  <p className="text-xs text-ink-secondary mt-0.5">
                    {t("settings.tour_replay_hint") || "Repasá las funciones del Inicio, Crear video y Editor."}
                  </p>
                </div>
                <button
                  onClick={() => { startReplaySession(); window.location.assign("/"); }}
                  className="shrink-0 text-[12px] font-medium px-4 py-2 rounded-lg bg-brand/15 text-brand-light ring-1 ring-brand/30 hover:bg-brand/25 transition-colors"
                >
                  {t("settings.tour_replay_btn") || "Ver tour de nuevo"}
                </button>
              </div>
            </Card>

            {/* Danger zone */}
            <Card className="ring-red-500/[0.08]">
              <SectionLabel>{t("settings.danger_zone") || "Zona de peligro"}</SectionLabel>

              {/* Data export */}
              <div className="flex items-center justify-between gap-4 py-3 border-b border-white/[0.03]">
                <div>
                  <p className="text-sm text-white">{t("settings.export_data") || "Exportar mis datos"}</p>
                  <p className="text-xs text-ink-secondary mt-0.5">
                    {t("settings.export_data_sub") || "Descargá un JSON con tu cuenta, configuración e historial de videos (GDPR)."}
                  </p>
                </div>
                <button onClick={handleExportData} disabled={exportLoading}
                  className="shrink-0 flex items-center gap-1.5 text-[12px] font-medium px-4 py-2 rounded-lg bg-surface-3/40 text-ink-secondary ring-1 ring-white/[0.06] hover:text-white hover:ring-white/[0.12] transition-colors disabled:opacity-40">
                  <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                    <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                    <polyline points="7 10 12 15 17 10" /><line x1="12" y1="15" x2="12" y2="3" />
                  </svg>
                  {exportLoading ? "…" : (t("settings.export_data_btn") || "Exportar")}
                </button>
              </div>

              {/* Delete account */}
              <div className="pt-3">
                <div className="flex items-center justify-between gap-4">
                  <div>
                    <p className="text-sm text-white">{t("settings.delete_account") || "Eliminar cuenta"}</p>
                    <p className="text-xs text-ink-secondary mt-0.5">
                      {t("settings.delete_account_sub") || "Acción irreversible. Todos tus datos serán eliminados."}
                    </p>
                  </div>
                  <button
                    onClick={() => { setShowDelete(!showDelete); setDeleteError(""); setDeletePassword(""); }}
                    className="shrink-0 text-[12px] font-medium px-4 py-2 rounded-lg bg-red-500/10 text-red-400 ring-1 ring-red-500/20 hover:bg-red-500/20 transition-colors">
                    {showDelete ? (t("settings.cancel") || "Cancelar") : (t("settings.delete_account") || "Eliminar")}
                  </button>
                </div>

                {showDelete && (
                  <div className="mt-4 pt-4 border-t border-red-500/10 space-y-3 animate-fade-in">
                    <Field label={t("settings.delete_confirm_label") || "Ingresá tu contraseña para confirmar"}>
                      <input type="password" value={deletePassword}
                        onChange={(e) => { setDeletePassword(e.target.value); setDeleteError(""); }}
                        className="input-field text-sm" placeholder="••••••••" />
                    </Field>
                    {deleteError && <InlineError message={deleteError} />}
                    <button onClick={handleDeleteAccount}
                      disabled={!deletePassword || deleteLoading}
                      className="w-full h-10 rounded-xl bg-red-500/15 text-red-400 text-sm font-medium ring-1 ring-red-500/25 hover:bg-red-500/25 transition-colors disabled:opacity-40 disabled:cursor-not-allowed">
                      {deleteLoading ? "…" : (t("settings.delete_confirm_btn") || "Confirmar eliminación")}
                    </button>
                  </div>
                )}
              </div>
            </Card>
          </>
        )}

        {/* ════════════════════ FACTURACIÓN ════════════════════ */}
        {activeSection === "facturacion" && (
          <>
            {/* Usage widget */}
            {usage && (
              <Card>
                <div className="flex items-start justify-between mb-4">
                  <div>
                    <SectionLabel>{t("settings.usage") || "Uso del plan"}</SectionLabel>
                    <div className="flex items-baseline gap-1.5">
                      <span className="text-3xl font-bold tracking-tight text-white tabular-nums">{usage.used}</span>
                      <span className="text-sm text-ink-secondary">
                        {t("settings.usage_of") || "de"} {usage.limit >= 999999 ? "∞" : usage.limit} {t("settings.usage_videos") || "videos este mes"}
                      </span>
                    </div>
                  </div>
                  <div className="text-right">
                    <span className={`text-2xl font-bold tabular-nums ${
                      usage.alert_100 ? "text-red-400" : usage.alert_80 ? "text-amber-400" : "text-brand-light"
                    }`}>
                      {usage.percent}%
                    </span>
                    <p className="text-[10px] text-ink-secondary mt-0.5">
                      {usage.remaining} {t("settings.usage_remaining") || "restantes"}
                    </p>
                  </div>
                </div>
                <UsageBar percent={usage.percent} alert80={usage.alert_80} alert100={usage.alert_100} />

                {usage.alert_100 && (
                  <AlertBanner variant="red">
                    <p className="text-xs font-medium">{t("settings.usage_alert_100") || "Límite mensual alcanzado"}</p>
                    {usage.overage > 0 && (
                      <p className="text-[11px] opacity-70 mt-0.5">
                        {usage.overage} {t("settings.usage_overage_videos") || "videos en overage"} · ${usage.overage_total?.toFixed(2)} adicionales
                      </p>
                    )}
                  </AlertBanner>
                )}
                {usage.alert_80 && !usage.alert_100 && (
                  <AlertBanner variant="amber">
                    <p className="text-xs">{t("settings.usage_alert_80") || "Estás al 80% de tu cuota mensual."}</p>
                  </AlertBanner>
                )}
              </Card>
            )}

            {/* Current plan */}
            <Card>
              <SectionLabel>{t("settings.current_plan") || "Plan actual"}</SectionLabel>
              <div className="flex items-end justify-between mb-4">
                <div>
                  <p className={`text-2xl font-bold tracking-tight ${planInfo.color}`}>{planInfo.label}</p>
                  <p className="text-xs text-ink-secondary mt-1">
                    {planInfo.videos} {t("settings.videos_month") || "videos/mes"}
                  </p>
                </div>
                {planInfo.price > 0 && (
                  <p className="text-xl font-bold tracking-tight text-white">
                    <span className="text-xs text-ink-secondary font-normal">USD </span>
                    {planInfo.price.toLocaleString()}
                    <span className="text-xs text-ink-secondary font-normal">/{t("settings.per_month") || "mes"}</span>
                  </p>
                )}
              </div>

              {subscription?.subscription && (
                <div className="space-y-2 pt-4 border-t border-white/[0.04]">
                  <div className="flex justify-between text-xs">
                    <span className="text-ink-secondary">{t("settings.status") || "Estado"}</span>
                    <span className={`font-medium ${subscription.subscription.status === "active" ? "text-accent" : "text-amber-400"}`}>
                      {subscription.subscription.status === "active"
                        ? (t("settings.status_active") || "Activo")
                        : subscription.subscription.status}
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
                    <div className="mt-2 p-2.5 rounded-xl bg-amber-500/[0.06] ring-1 ring-amber-500/15">
                      <p className="text-xs text-amber-400">{t("settings.cancel_notice") || "Se cancela al final del período"}</p>
                    </div>
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

            {/* Change plan */}
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
                        <p className="text-xs text-ink-secondary mb-1">
                          {p.videos} {t("settings.videos_month") || "videos/mes"}
                        </p>
                        <p className="text-2xl font-bold tracking-tight text-white">
                          <span className="text-xs text-ink-secondary font-normal">$</span>
                          {p.price.toLocaleString()}
                        </p>
                        <p className="text-[10px] text-gray-600 mt-1">
                          ${(p.price / p.videos).toFixed(2)}/{t("settings.per_video") || "video"}
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
                    <div key={inv.id}
                      className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2 py-3 border-b border-white/[0.03] last:border-0">
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
                            className="flex items-center gap-1.5 text-xs text-ink-secondary hover:text-white transition-colors px-3 py-1.5 rounded-lg ring-1 ring-white/[0.06] hover:ring-white/[0.12] bg-surface-3/30">
                            <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                              <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                              <polyline points="7 10 12 15 17 10" /><line x1="12" y1="15" x2="12" y2="3" />
                            </svg>
                            {t("settings.download_invoice") || "PDF"}
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

        {/* ════════════════════ INTEGRACIONES ════════════════════ */}
        {/* Canary: el card Drive sólo aparece si features.drive_export.
            Por ahora = admin only. Cuando se abra a tenant X, los users
            de ese tenant verán automáticamente la card sin más cambios. */}
        {activeSection === "integraciones" && user?.features?.drive_export && (
          <>
            <Card>
              <SectionLabel>{t("settings.integrations_drive_label") || "Google Drive"}</SectionLabel>
              <p className="text-xs text-ink-secondary mb-3">
                {t("settings.integrations_drive_help") ||
                  "Conectá Google Drive para subir los ProRes desde la app sin pasar por tu máquina. Para archivos grandes (16 GB) es ~30x más rápido que descargar y resubir."}
              </p>
              <DriveConnectButton />
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
              <SectionLabel>{t("settings.yt_template")}</SectionLabel>
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
                <Field label={t("settings.hashtags")} help={t("settings.hashtags_help")}>
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
              <SectionLabel>{t("settings.channels") || "Canales de YouTube"}</SectionLabel>
              <p className="text-xs text-ink-secondary mb-4 -mt-1">
                {t("settings.channels_sub") || "Gestioná los canales asociados a esta cuenta (máx 5)."}
              </p>

              {(!settings.channels || settings.channels.length === 0) ? (
                <p className="text-xs text-ink-secondary mb-4">
                  {t("settings.channel_empty") || "Ningún canal configurado."}
                </p>
              ) : (
                <div className="mb-4 space-y-0">
                  {settings.channels.map((ch, idx) => (
                    <div key={idx}
                      className={`flex items-center gap-3 py-2.5 ${idx < settings.channels.length - 1 ? "border-b border-white/[0.03]" : ""}`}>
                      <div className="flex-1 min-w-0">
                        <input
                          type="text"
                          value={ch.name}
                          onChange={(e) => {
                            const next = settings.channels.map((c, i) => i === idx ? { ...c, name: e.target.value } : c);
                            update("channels", next);
                          }}
                          placeholder={t("settings.channel_placeholder_name") || "Nombre del canal"}
                          className="input-field text-sm w-full"
                        />
                      </div>
                      <div className="w-32 shrink-0">
                        <input
                          type="text"
                          value={ch.handle || ""}
                          onChange={(e) => {
                            const next = settings.channels.map((c, i) => i === idx ? { ...c, handle: e.target.value } : c);
                            update("channels", next);
                          }}
                          placeholder="@handle"
                          className="input-field text-sm w-full"
                        />
                      </div>
                      <button
                        onClick={() => update("channels", settings.channels.filter((_, i) => i !== idx))}
                        className="shrink-0 w-8 h-8 flex items-center justify-center rounded-lg text-gray-600 hover:text-red-400 hover:bg-red-500/10 transition-colors">
                        <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2.5" viewBox="0 0 24 24">
                          <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
                        </svg>
                      </button>
                    </div>
                  ))}
                </div>
              )}

              {(settings.channels || []).length < 5 && (
                <button
                  onClick={() => update("channels", [...(settings.channels || []), { name: "", handle: "" }])}
                  className="flex items-center gap-1.5 text-xs font-medium text-brand-light hover:text-white transition-colors">
                  <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth="2.5" viewBox="0 0 24 24">
                    <line x1="12" y1="5" x2="12" y2="19" /><line x1="5" y1="12" x2="19" y2="12" />
                  </svg>
                  {t("settings.channel_add") || "Agregar canal"}
                </button>
              )}
              {(settings.channels || []).length >= 5 && (
                <p className="text-[11px] text-ink-secondary">{t("settings.channel_max") || "Máximo 5 canales."}</p>
              )}
            </Card>

            {(saveError || billingError) && (
              <div className="rounded-card bg-red-500/[0.08] ring-1 ring-red-500/30 px-4 py-3 text-sm text-red-200">
                {saveError || billingError}
              </div>
            )}
            <div className="flex items-center justify-end gap-3 pt-2">
              {saved && <InlineSuccess message={t("settings.saved")} />}
              <button onClick={handleSave} className="btn-primary px-6">
                {t("settings.save")}
              </button>
            </div>
          </>
        )}

      </div>
    </div>
  );
}
