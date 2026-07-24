import { useState, useEffect, useRef } from "react";
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
    <p className="text-section text-gray-500 uppercase mb-3">
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
      aria-current={active ? "page" : undefined}
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

function Toggle({ value, onChange, label }) {
  return (
    <button
      role="switch"
      aria-checked={value}
      aria-label={label}
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

// Avatar con fallback a la inicial: si GET /auth/avatar/{id} 404ea (sin
// foto subida aún o token vencido) ocultamos el <img> y mostramos el
// círculo con la inicial. `key` fuerza recarga del <img> tras subir.
function AvatarImg({ user, size = "w-16 h-16", textSize = "text-xl", reloadKey = 0 }) {
  const [failed, setFailed] = useState(!user?.avatar_url);
  useEffect(() => { setFailed(!user?.avatar_url); }, [user?.avatar_url, reloadKey]);
  const initial = (user?.full_name || user?.username || "?").charAt(0);
  if (failed) {
    return (
      <div className={`${size} rounded-full bg-brand/20 flex items-center justify-center shrink-0 ring-1 ring-white/[0.08]`}>
        <span className={`${textSize} font-bold text-brand uppercase`}>{initial}</span>
      </div>
    );
  }
  return (
    <img
      key={reloadKey}
      src={`${API}/auth/avatar/${user.id}?v=${reloadKey}`}
      alt=""
      className={`${size} rounded-full object-cover shrink-0 ring-1 ring-white/[0.08]`}
      onError={() => setFailed(true)}
    />
  );
}

// "hace 3 minutos" — relativo simple, sin dependencias.
function relativeTime(iso, t, locale) {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "—";
  const diff = Math.max(0, Date.now() - then);
  const m = Math.floor(diff / 60000);
  if (m < 1) return t("settings.time_just_now");
  if (m < 60) return t("settings.time_minutes", { count: m });
  const h = Math.floor(m / 60);
  if (h < 24) return t("settings.time_hours", { count: h });
  const d = Math.floor(h / 24);
  if (d < 30) return t(d === 1 ? "settings.time_day_one" : "settings.time_days", { count: d });
  return new Date(iso).toLocaleDateString(locale);
}

// User-agent → "Navegador en SO" con heurística simple.
function parseUA(ua, t) {
  if (!ua) return t("settings.unknown_device");
  let browser = t("settings.browser");
  if (/Edg/.test(ua)) browser = "Edge";
  else if (/Chrome/.test(ua) && !/Chromium/.test(ua)) browser = "Chrome";
  else if (/Firefox/.test(ua)) browser = "Firefox";
  else if (/Safari/.test(ua)) browser = "Safari";
  let os = null;
  if (/iPhone|iPad/.test(ua)) os = "iOS";
  else if (/Android/.test(ua)) os = "Android";
  else if (/Mac OS X|Macintosh/.test(ua)) os = "macOS";
  else if (/Windows/.test(ua)) os = "Windows";
  else if (/Linux/.test(ua)) os = "Linux";
  if (os) return t("settings.device_on_os", { browser, os });
  return ua.length > 48 ? ua.slice(0, 48) + "…" : ua;
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
  const locale = lang === "en" ? "en-US" : lang === "pt" ? "pt-BR" : "es-AR";
  const [settings, setSettings] = useState(DEFAULT_SETTINGS);
  const [savedSettings, setSavedSettings] = useState(DEFAULT_SETTINGS);
  const [saved, setSaved] = useState(false);
  const [loading, setLoading] = useState(true);
  const [user, setUser] = useState(getUser);
  const youtubePublishingEnabled = user?.features?.youtube_publish === true;
  // Miembros de una cuenta B2B (billing_group, ej. operadores de Universal):
  // el contrato lo maneja la plataforma directamente con el cliente — los
  // operadores NO ven precios, ni la suscripción de Stripe, ni pueden
  // cambiar el plan. Solo ven su consumo (pool compartido del grupo).
  const isB2BMember = Boolean(user?.billing_group);

  const [subscription, setSubscription] = useState(null);
  const [invoices, setInvoices] = useState([]);
  const [usage, setUsage] = useState(null);
  const [billingLoading, setBillingLoading] = useState(false);
  const [paymentMethod, setPaymentMethod] = useState(null);
  // Plan-change confirmation modal + cancel/reactivate (Fase 2 / PR4)
  const [planModal, setPlanModal] = useState(null);          // { planId } | null
  const [planPreview, setPlanPreview] = useState(null);      // /change-plan/preview response
  const [planPreviewLoading, setPlanPreviewLoading] = useState(false);
  const [planPreviewError, setPlanPreviewError] = useState(null);
  const [cancelLoading, setCancelLoading] = useState(false);
  const [activeSection, setActiveSection] = useState("cuenta");
  useEffect(() => {
    if (!youtubePublishingEnabled && activeSection === "youtube") {
      setActiveSection("perfil");
    }
  }, [youtubePublishingEnabled, activeSection]);
  // Mirror into a ref so the window-focus listener (registered once, empty
  // deps) reads the current section without going stale.
  const activeSectionRef = useRef(activeSection);
  activeSectionRef.current = activeSection;

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
  const [exportError, setExportError] = useState("");

  // API keys
  const [apiKeys, setApiKeys] = useState([]);
  const [apiKeyName, setApiKeyName] = useState("");
  const [apiKeyCreating, setApiKeyCreating] = useState(false);
  const [newKeySecret, setNewKeySecret] = useState(null);
  const [keyCopied, setKeyCopied] = useState(false);
  const [revokingId, setRevokingId] = useState(null);
  const [apiKeyError, setApiKeyError] = useState("");
  const settingsRef = useRef(settings);
  const notificationQueueRef = useRef(Promise.resolve());
  const notificationMutationRef = useRef(0);
  const savedSettingsRef = useRef(savedSettings);
  const savedTimerRef = useRef(null);
  settingsRef.current = settings;
  savedSettingsRef.current = savedSettings;

  // Perfil
  const [fullName, setFullName] = useState(user?.full_name || "");
  const [savingName, setSavingName] = useState(false);
  const [nameSaved, setNameSaved] = useState(false);
  const [nameError, setNameError] = useState("");
  const [avatarUploading, setAvatarUploading] = useState(false);
  const [avatarError, setAvatarError] = useState("");
  const [avatarKey, setAvatarKey] = useState(0); // bump → recarga el <img>

  // Dispositivos (sesiones)
  const [sessions, setSessions] = useState([]);
  const [sessionsLoading, setSessionsLoading] = useState(false);
  const [sessionsError, setSessionsError] = useState("");
  const [revokingSession, setRevokingSession] = useState(null);
  const [revokingOthers, setRevokingOthers] = useState(false);

  // Mi equipo
  const [team, setTeam] = useState(null); // {tenant_id, members:[...]}
  const [teamLoading, setTeamLoading] = useState(false);

  useEffect(() => {
    fetch(`${API}/settings`, { headers: authHeaders() })
      .then((r) => r.json())
      .then((data) => {
        if (data && Object.keys(data).length) {
          const next = { ...DEFAULT_SETTINGS, ...data };
          setSettings(next);
          setSavedSettings(next);
        }
      })
      .catch(() => {})
      .finally(() => setLoading(false));

    fetch(`${API}/billing/subscription`, { headers: authHeaders() })
      .then((r) => r.json()).then(setSubscription).catch(() => {});

    fetch(`${API}/billing/invoices`, { headers: authHeaders() })
      .then((r) => r.json())
      .then((data) => { if (Array.isArray(data)) setInvoices(data); })
      .catch(() => {});

    fetch(`${API}/billing/payment-method`, { headers: authHeaders() })
      .then((r) => r.json())
      .then((data) => setPaymentMethod(data?.payment_method || null))
      .catch(() => {});

    fetch(`${API}/usage`, { headers: authHeaders() })
      .then((r) => r.json()).then(setUsage).catch(() => {});

    fetch(`${API}/auth/api-keys`, { headers: authHeaders() })
      .then((r) => r.json())
      .then((data) => { if (Array.isArray(data)) setApiKeys(data); })
      .catch(() => {});

    // /team/members al montar: lo necesitamos para decidir si mostrar la
    // tab "Mi equipo" (sólo si el workspace tiene >1 miembro).
    fetch(`${API}/team/members`, { headers: authHeaders() })
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => { if (data && Array.isArray(data.members)) setTeam(data); })
      .catch(() => {});
  }, []);

  // Deep-link support: ?tab=facturacion opens the billing tab directly. Used
  // by the global PastDueBanner / UpgradeNudge CTAs and the lifecycle emails
  // that link to /?view=settings&tab=facturacion.
  useEffect(() => {
    if (new URLSearchParams(window.location.search).get("tab") === "facturacion") {
      setActiveSection("facturacion");
    }
  }, []);

  // YouTube account connection
  const [ytStatus, setYtStatus] = useState(null); // null=loading, {connected, channel_name, ...}
  const [ytStatusError, setYtStatusError] = useState(null);
  const [ytDisconnecting, setYtDisconnecting] = useState(false);
  // Google shows two confusing screens (unverified app + unchecked
  // permission boxes). Walk the user through them before opening the popup.
  const [ytHelp, setYtHelp] = useState(false);

  const checkYtStatus = () => {
    setYtStatus(null);
    setYtStatusError(null);
    fetch(`${API}/youtube/connection-status`, { headers: authHeaders() })
      .then((r) => r.json())
      .then(setYtStatus)
      .catch(() => setYtStatusError(t("settings.yt_verify_error")));
  };

  // Quiet re-check (no loading flicker) used by the post-connect poll and
  // the window-focus listener. Returns whether the account is connected.
  const refreshYtStatusQuiet = async () => {
    try {
      const data = await fetch(`${API}/youtube/connection-status`, { headers: authHeaders() })
        .then((r) => r.json());
      setYtStatus(data);
      return !!data?.connected;
    } catch {
      return false;
    }
  };
  const ytPollRef = useRef(null);

  useEffect(() => {
    if (activeSection === "youtube" && youtubePublishingEnabled) checkYtStatus();
  }, [activeSection, youtubePublishingEnabled]);

  useEffect(() => {
    // The OAuth popup is opened with `noopener`, so its
    // `window.opener.postMessage` never reaches us — the connection would
    // only show after a manual refresh. Re-check whenever the user returns
    // to this tab (i.e. after closing the popup). The message handler is a
    // best-effort extra for browsers where the opener survives.
    const onMessage = (e) => { if (e.data === "yt_connected") refreshYtStatusQuiet(); };
    const onFocus = () => { if (activeSectionRef.current === "youtube") refreshYtStatusQuiet(); };
    window.addEventListener("message", onMessage);
    window.addEventListener("focus", onFocus);
    document.addEventListener("visibilitychange", onFocus);
    return () => {
      window.removeEventListener("message", onMessage);
      window.removeEventListener("focus", onFocus);
      document.removeEventListener("visibilitychange", onFocus);
      if (ytPollRef.current) clearInterval(ytPollRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleYtConnect = async () => {
    setYtHelp(false);
    try {
      const res = await fetch(`${API}/youtube/auth-url`, { headers: authHeaders() });
      if (!res.ok) throw new Error(t("settings.yt_auth_url_error"));
      const data = await res.json();
      const popup = window.open(data.auth_url, "_blank", "width=600,height=700,noopener");
      // Poll the connection status while the popup is open — resilient to
      // the popup's postMessage not reaching us (noopener) and to popup
      // blockers. Stops on connect, popup close, or after 2 min.
      if (ytPollRef.current) clearInterval(ytPollRef.current);
      const startedAt = Date.now();
      ytPollRef.current = setInterval(async () => {
        const connected = await refreshYtStatusQuiet();
        const popupClosed = popup && popup.closed;
        if (connected || Date.now() - startedAt > 120000 || popupClosed) {
          clearInterval(ytPollRef.current);
          ytPollRef.current = null;
          if (popupClosed && !connected) setTimeout(refreshYtStatusQuiet, 1500);
        }
      }, 2000);
    } catch (err) {
      setYtStatusError(err.message);
    }
  };

  const handleYtDisconnect = async () => {
    setYtDisconnecting(true);
    try {
      const res = await fetch(`${API}/youtube/disconnect`, { method: "POST", headers: authHeaders() });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || `Error ${res.status}`);
      }
      checkYtStatus();
    } catch (err) {
      setYtStatusError(err?.message || t("settings.youtube_disconnect_error"));
    } finally {
      setYtDisconnecting(false);
    }
  };

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
      setSavedSettings(settings);
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

  const refreshSubscription = () =>
    fetch(`${API}/billing/subscription`, { headers: authHeaders() })
      .then((r) => r.json()).then(setSubscription).catch(() => {});

  // Open the confirmation modal and fetch the proration preview. The Fase-1
  // downgrade guardrail (400) is surfaced as planPreviewError inside the modal.
  const openChangePlan = async (planId) => {
    setPlanModal({ planId });
    setPlanPreview(null);
    setPlanPreviewError(null);
    setPlanPreviewLoading(true);
    try {
      const res = await fetch(`${API}/billing/change-plan/preview`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({ plan_id: planId }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || `Error ${res.status}`);
      setPlanPreview(data);
    } catch (err) {
      setPlanPreviewError(err.message || String(err));
    } finally { setPlanPreviewLoading(false); }
  };

  const confirmChangePlan = async () => {
    if (!planModal) return;
    setBillingLoading(true);
    setPlanPreviewError(null);
    try {
      const res = await fetch(`${API}/billing/change-plan`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({ plan_id: planModal.planId }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || `Error ${res.status}`);
      setPlanModal(null);
      setTimeout(refreshSubscription, 1500); // webhook is the source of truth
    } catch (err) {
      setPlanPreviewError(err.message || String(err));
    } finally { setBillingLoading(false); }
  };

  const handleCancel = async () => {
    setCancelLoading(true); setBillingError(null);
    try {
      const res = await fetch(`${API}/billing/cancel`, { method: "POST", headers: authHeaders() });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || `Error ${res.status}`);
      await refreshSubscription();
    } catch (err) { setBillingError(err.message || String(err)); }
    finally { setCancelLoading(false); }
  };

  const handleReactivate = async () => {
    setCancelLoading(true); setBillingError(null);
    try {
      const res = await fetch(`${API}/billing/reactivate`, { method: "POST", headers: authHeaders() });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || `Error ${res.status}`);
      await refreshSubscription();
    } catch (err) { setBillingError(err.message || String(err)); }
    finally { setCancelLoading(false); }
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
    setExportError("");
    try {
      const res = await fetch(`${API}/auth/data-export`, { headers: authHeaders() });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || `Error ${res.status}`);
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "genly-data-export.json";
      a.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      setExportError(err?.message || t("settings.export_data_error"));
    } finally {
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
    const current = settingsRef.current;
    const next = { ...current, [key]: !current[key] };
    const mutationId = ++notificationMutationRef.current;
    settingsRef.current = next;
    setSettings(next);
    const persist = async () => {
      const res = await fetch(`${API}/settings`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify(next),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || `Error ${res.status}`);
      }
      savedSettingsRef.current = next;
      setSavedSettings(next);
      if (mutationId === notificationMutationRef.current) {
        setSaved(true);
        if (savedTimerRef.current) clearTimeout(savedTimerRef.current);
        savedTimerRef.current = setTimeout(() => setSaved(false), 3000);
      }
    };
    const request = notificationQueueRef.current.then(persist, persist);
    notificationQueueRef.current = request.catch(() => {});
    try {
      await request;
    } catch (err) {
      // Revertir optimistic update: si el server rechazó, el toggle
      // local no debería quedarse mostrando el nuevo estado.
      if (mutationId === notificationMutationRef.current) {
        const lastConfirmed = savedSettingsRef.current;
        settingsRef.current = lastConfirmed;
        setSettings(lastConfirmed);
      }
      setSaveError(`${t("settings.notification_save_error")}: ${err.message || err}`);
      setTimeout(() => setSaveError(null), 6000);
    }
  };

  const handleCreateApiKey = async () => {
    if (!apiKeyName.trim()) return;
    setApiKeyError("");
    setApiKeyCreating(true);
    try {
      const res = await fetch(`${API}/auth/api-keys`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({ name: apiKeyName.trim() }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || `${t("settings.api_key_create_error")} (${res.status})`);
      setNewKeySecret(data.key);
      setApiKeys((prev) => [{ id: data.id, name: data.name, prefix: data.prefix, created_at: data.created_at, last_used_at: null }, ...prev]);
      setApiKeyName("");
    } catch (err) {
      setApiKeyError(err?.message || t("settings.api_key_create_error"));
    } finally {
      setApiKeyCreating(false);
    }
  };

  const handleRevokeApiKey = async (keyId) => {
    setApiKeyError("");
    setRevokingId(keyId);
    try {
      const res = await fetch(`${API}/auth/api-keys/${keyId}`, {
        method: "DELETE",
        headers: authHeaders(),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || `${t("settings.api_key_revoke_error")} (${res.status})`);
      }
      setApiKeys((prev) => prev.filter((k) => k.id !== keyId));
    } catch (err) {
      setApiKeyError(err?.message || t("settings.api_key_revoke_error"));
    } finally {
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

  // Persiste el user actualizado en localStorage + estado en memoria, para
  // que la sidebar (que lee genly_user) refleje el nuevo nombre/avatar.
  const persistUser = (patch) => {
    const next = { ...(getUser() || {}), ...patch };
    localStorage.setItem("genly_user", JSON.stringify(next));
    setUser(next);
  };

  const handleSaveName = async () => {
    setNameError("");
    setSavingName(true);
    try {
      const res = await fetch(`${API}/auth/profile`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({ full_name: fullName }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || `Error ${res.status}`);
      }
      const data = await res.json();
      persistUser({ full_name: data.full_name, avatar_url: data.avatar_url });
      setNameSaved(true);
      setTimeout(() => setNameSaved(false), 3000);
    } catch (err) {
      setNameError(err.message || String(err));
    } finally {
      setSavingName(false);
    }
  };

  const handleAvatarUpload = async (e) => {
    const file = e.target.files?.[0];
    e.target.value = ""; // permite re-subir el mismo archivo
    if (!file) return;
    setAvatarError("");
    // Validación client-side espejo del backend (mejor UX que esperar el 400).
    if (!["image/jpeg", "image/png", "image/webp"].includes(file.type)) {
      setAvatarError(t("settings.avatar_format_error"));
      return;
    }
    if (file.size > 5 * 1024 * 1024) {
      setAvatarError("La imagen supera los 5 MB.");
      return;
    }
    setAvatarUploading(true);
    try {
      const fd = new FormData();
      fd.append("file", file);
      const res = await fetch(`${API}/auth/avatar`, {
        method: "POST",
        headers: authHeaders(),
        body: fd,
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || `Error ${res.status}`);
      }
      const data = await res.json();
      persistUser({ avatar_url: data.avatar_url });
      setAvatarKey((k) => k + 1); // fuerza recarga del <img>
    } catch (err) {
      // Un fallo de red (fetch rechazado) llega como TypeError con el
      // mensaje críptico "Load failed"/"Failed to fetch" — pasa, por
      // ejemplo, si el server está reiniciando por un deploy. Mostramos
      // algo accionable en vez del texto crudo del navegador. Los errores
      // con respuesta del backend (400/500) sí conservan su detail.
      const msg = (err instanceof TypeError)
        ? t("settings.avatar_network_error")
        : (err.message || t("settings.avatar_upload_error"));
      setAvatarError(msg);
    } finally {
      setAvatarUploading(false);
    }
  };

  const fetchSessions = async () => {
    setSessionsLoading(true);
    setSessionsError("");
    try {
      const res = await fetch(`${API}/auth/sessions`, { headers: authHeaders() });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || `Error ${res.status}`);
      }
      const data = await res.json();
      setSessions(Array.isArray(data.sessions) ? data.sessions : []);
    } catch (err) {
      setSessionsError(err.message || String(err));
    } finally {
      setSessionsLoading(false);
    }
  };

  // Logout self-contained (Settings sólo recibe onBack, no onLogout): si la
  // sesión revocada era la actual, limpiamos identidad como hace
  // handleDeleteAccount y mandamos a "/". El listener de `storage` en App
  // propaga el logout al resto de las tabs abiertas.
  const localLogout = () => {
    localStorage.removeItem("genly_token");
    localStorage.removeItem("genly_user");
    window.location.assign("/");
  };

  const handleRevokeSession = async (sid) => {
    setRevokingSession(sid);
    setSessionsError("");
    try {
      const res = await fetch(`${API}/auth/sessions/${sid}/revoke`, {
        method: "POST",
        headers: authHeaders(),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || `Error ${res.status}`);
      }
      const data = await res.json();
      if (data.was_current) { localLogout(); return; }
      await fetchSessions();
    } catch (err) {
      setSessionsError(err.message || String(err));
    } finally {
      setRevokingSession(null);
    }
  };

  const handleRevokeOthers = async () => {
    setRevokingOthers(true);
    setSessionsError("");
    try {
      const res = await fetch(`${API}/auth/sessions/revoke-others`, {
        method: "POST",
        headers: authHeaders(),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || `Error ${res.status}`);
      }
      await fetchSessions();
    } catch (err) {
      setSessionsError(err.message || String(err));
    } finally {
      setRevokingOthers(false);
    }
  };

  const fetchTeam = async () => {
    setTeamLoading(true);
    try {
      const res = await fetch(`${API}/team/members`, { headers: authHeaders() });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || `Error ${res.status}`);
      }
      const data = await res.json();
      setTeam(data);
    } catch {
      // best-effort: la tab sólo es visible si ya cargó >1 miembro al montar
    } finally {
      setTeamLoading(false);
    }
  };

  // Carga perezosa al abrir cada tab.
  useEffect(() => {
    if (activeSection === "dispositivos") fetchSessions();
    if (activeSection === "equipo") fetchTeam();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeSection]);

  // Mostramos "Mi equipo" SIEMPRE (descubrible), incluso si sos el único en
  // el workspace — adentro se muestra un estado "sos el único por ahora".
  // Esconderlo cuando estás solo confundía (parecía que la tab desaparecía).
  const showTeamTab = true;

  const currentPlan = user?.plan || "free";
  const planInfo = PLAN_INFO[currentPlan] || PLAN_INFO.free;
  const settingsDirty = JSON.stringify(settings) !== JSON.stringify(savedSettings);
  const profileDirty = fullName.trim() !== (user?.full_name || "").trim();

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
    <div className="w-full max-w-[1180px] mx-auto animate-fade-in">
      {/* ─── Header ───────────────────────────────────────────────── */}
      <div className="flex items-end gap-3 mb-8">
        <button onClick={onBack} aria-label={t("common.back")}
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

      <div className="min-h-5 mb-3" role="status" aria-live="polite">
        {saved && <InlineSuccess message={t("settings.saved") || "Cambios guardados"} />}
      </div>

      {/* ─── Tabs ─────────────────────────────────────────────────── */}
      <div className="settings-layout">
      <nav className="settings-nav" aria-label={t("settings.sections_aria")}>
        {[
          { id: "perfil",         label: t("settings.profile_tab"), icon: "◉" },
          { id: "cuenta",         label: t("settings.account") || "Cuenta", icon: "◇" },
          { id: "facturacion",    label: t("settings.billing") || "Facturación", icon: "↗" },
          // Integraciones por ahora sólo contiene Drive — escondemos
          // toda la tab cuando el feature flag está off (canary mode).
          user?.features?.drive_export
            ? { id: "integraciones", label: t("settings.integrations_tab") || "Integraciones", icon: "⌁" }
            : null,
          youtubePublishingEnabled
            ? { id: "youtube", label: "YouTube", icon: "▶" }
            : null,
          { id: "dispositivos",   label: t("settings.devices_tab"), icon: "▣" },
          // "Mi equipo" sólo si el workspace tiene >1 miembro.
          showTeamTab ? { id: "equipo", label: t("settings.team_tab"), icon: "◎" } : null,
        ].filter(Boolean).map((s) => (
          <TabPill key={s.id} active={activeSection === s.id} onClick={() => setActiveSection(s.id)}>
            <span className="settings-nav__icon" aria-hidden="true">{s.icon}</span>{s.label}
          </TabPill>
        ))}
      </nav>

      <div className="settings-content space-y-4">

        {/* ════════════════════ PERFIL ════════════════════ */}
        {activeSection === "perfil" && (
          <>
            <Card>
              <SectionLabel>{t("settings.profile_photo")}</SectionLabel>
              <p className="text-xs text-ink-secondary mb-4 -mt-1">
                {t("settings.profile_photo_sub")}
              </p>
              <div className="flex items-center gap-4">
                <AvatarImg user={user} reloadKey={avatarKey} size="w-16 h-16" textSize="text-xl" />
                <div>
                  <label className={`btn-secondary text-xs h-9 px-4 inline-flex items-center cursor-pointer ${avatarUploading ? "opacity-40 pointer-events-none" : ""}`}>
                    {avatarUploading ? t("settings.avatar_uploading") : t("settings.avatar_change")}
                    <input
                      type="file"
                      accept="image/jpeg,image/png,image/webp"
                      className="hidden"
                      onChange={handleAvatarUpload}
                      disabled={avatarUploading}
                    />
                  </label>
                  <p className="text-[10px] text-gray-600 mt-1.5">{t("settings.avatar_help")}</p>
                </div>
              </div>
              {avatarError && <div className="mt-3"><InlineError message={avatarError} /></div>}
            </Card>

            <Card>
              <SectionLabel>{t("settings.name")}</SectionLabel>
              <p className="text-xs text-ink-secondary mb-4 -mt-1">
                {t("settings.name_sub")}
              </p>
              <Field label={t("settings.full_name")}>
                <input
                  type="text"
                  value={fullName}
                  onChange={(e) => { setFullName(e.target.value); setNameError(""); }}
                  className="input-field text-sm"
                  placeholder={t("settings.name_placeholder")}
                />
              </Field>
              {nameError && <div className="mt-3"><InlineError message={nameError} /></div>}
              <div className="flex items-center justify-end gap-3 mt-4">
                {nameSaved && <InlineSuccess message={t("settings.name_saved")} />}
                <button
                  onClick={handleSaveName}
                  disabled={savingName || !profileDirty}
                  className="btn-primary px-5 disabled:opacity-40 disabled:cursor-not-allowed">
                  {savingName ? "…" : t("settings.save_profile")}
                </button>
              </div>
            </Card>
          </>
        )}

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
                    <Toggle value={!!settings[key]} onChange={() => toggleNotif(key)} label={label} />
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
              {apiKeyError && (
                <div role="alert" className="mb-4">
                  <InlineError message={apiKeyError} />
                </div>
              )}

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
                        className="shrink-0 text-label px-3 py-1.5 rounded-lg bg-red-500/10 text-red-400 ring-1 ring-red-500/15 hover:bg-red-500/20 transition-colors disabled:opacity-40">
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
              {exportError && <div className="mt-3"><InlineError message={exportError} /></div>}

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
            {/* Dunning notice — mirrors the global banner; CTA → Stripe portal */}
            {subscription?.billing_status === "past_due" && (
              <AlertBanner variant="amber">
                <div className="flex flex-col sm:flex-row sm:items-center gap-2">
                  <div className="flex-1">
                    <p className="text-xs font-semibold">
                      {t("billing.past_due_title") || "Tu último pago falló."}
                    </p>
                    <p className="text-[11px] opacity-80 mt-0.5">
                      {t("billing.past_due_body") || "Actualizá tu medio de pago para no perder acceso a la generación de videos."}
                    </p>
                  </div>
                  <button
                    onClick={handleManageBilling}
                    disabled={billingLoading}
                    className="shrink-0 px-3 py-1.5 rounded-lg text-xs font-semibold bg-amber-500 text-black hover:bg-amber-400 disabled:opacity-60 transition-colors"
                  >
                    {billingLoading
                      ? (t("common.opening") || "Abriendo…")
                      : (t("billing.update_payment") || "Actualizar medio de pago")}
                  </button>
                </div>
              </AlertBanner>
            )}

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
                        {usage.overage} {t("settings.usage_overage_videos") || "videos en overage"} · ${usage.overage_total?.toFixed(2)} {t("settings.additional")}
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
                {planInfo.price > 0 && !isB2BMember && (
                  <p className="text-xl font-bold tracking-tight text-white">
                    <span className="text-xs text-ink-secondary font-normal">USD </span>
                    {planInfo.price.toLocaleString()}
                    <span className="text-xs text-ink-secondary font-normal">/{t("settings.per_month") || "mes"}</span>
                  </p>
                )}
              </div>

              {!isB2BMember && subscription?.subscription && (
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
                  {subscription.subscription.cancel_at_period_end ? (
                    <div className="mt-2 p-2.5 rounded-xl bg-amber-500/[0.06] ring-1 ring-amber-500/15">
                      <p className="text-xs text-amber-400">
                        {(t("billing.cancel_access_until") || "Mantenés acceso hasta el {date}").replace(
                          "{date}", new Date(subscription.subscription.current_period_end * 1000).toLocaleDateString())}
                      </p>
                      <button onClick={handleReactivate} disabled={cancelLoading}
                        className="mt-2 text-xs font-semibold text-brand-light hover:text-white disabled:opacity-50">
                        {cancelLoading ? (t("common.opening") || "…") : (t("billing.reactivate") || "Reactivar suscripción")}
                      </button>
                    </div>
                  ) : (
                    <button onClick={handleCancel} disabled={cancelLoading}
                      className="mt-3 text-xs font-medium text-red-400/80 hover:text-red-400 disabled:opacity-50">
                      {cancelLoading ? (t("common.opening") || "…") : (t("billing.cancel_subscription") || "Cancelar suscripción")}
                    </button>
                  )}
                  {billingError && <div className="mt-2"><InlineError message={billingError} /></div>}
                </div>
              )}

              {paymentMethod && (
                <div className="flex items-center justify-between gap-3 mt-4 pt-4 border-t border-white/[0.04]">
                  <div className="flex items-center gap-2.5 min-w-0">
                    <svg className="w-5 h-5 shrink-0 text-ink-secondary" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                      <rect x="1" y="4" width="22" height="16" rx="2" /><line x1="1" y1="10" x2="23" y2="10" />
                    </svg>
                    <div className="min-w-0">
                      <p className="text-sm text-white truncate">
                        {(paymentMethod.brand ? paymentMethod.brand.charAt(0).toUpperCase() + paymentMethod.brand.slice(1) : (t("settings.card") || "Tarjeta"))} ···· {paymentMethod.last4}
                      </p>
                      {paymentMethod.exp_month && paymentMethod.exp_year && (
                        <p className="text-[11px] text-gray-600 mt-0.5">
                          {t("settings.card_expires") || "Vence"} {String(paymentMethod.exp_month).padStart(2, "0")}/{paymentMethod.exp_year}
                        </p>
                      )}
                    </div>
                  </div>
                  <button onClick={handleManageBilling} disabled={billingLoading}
                    className="shrink-0 text-xs text-ink-secondary hover:text-white transition-colors px-3 py-1.5 rounded-lg ring-1 ring-white/[0.06] hover:ring-white/[0.12] bg-surface-3/30 disabled:opacity-60">
                    {billingLoading ? (t("common.opening") || "Abriendo…") : (t("settings.update_card") || "Actualizar")}
                  </button>
                </div>
              )}

              {!isB2BMember && subscription?.has_subscription && (
                <button onClick={handleManageBilling} disabled={billingLoading}
                  className="btn-secondary mt-5 text-xs h-10 px-4">
                  {t("settings.manage_billing") || "Administrar facturación"}
                </button>
              )}
            </Card>

            {/* Change plan */}
            {!isB2BMember && currentPlan !== "unlimited" && (
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
                      <button key={planId} onClick={() => {
                          if (isCurrent) return;
                          // Existing subscriber → confirm with a proration preview;
                          // free user (no sub yet) → straight to Checkout.
                          if (subscription?.has_subscription) openChangePlan(planId);
                          else handleSubscribe(planId);
                        }}
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

            {/* Invoice history — oculto para miembros B2B (sus facturas
                las maneja la plataforma con el cliente, no Stripe) */}
            {!isB2BMember && (
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
            )}

            {/* Plan-change confirmation modal (proration preview + guardrail) */}
            {planModal && (
              <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4 animate-fade-in"
                onClick={() => !billingLoading && setPlanModal(null)}>
                <div className="w-full max-w-sm rounded-card bg-surface-1 ring-1 ring-white/[0.08] p-6"
                  onClick={(e) => e.stopPropagation()}>
                  <SectionLabel>{t("billing.change_confirm_title") || "Confirmar cambio de plan"}</SectionLabel>
                  <p className="text-sm text-white mb-3">
                    {(t("billing.change_confirm_to") || "Vas a cambiar al {plan}.").replace(
                      "{plan}", PLAN_INFO[planModal.planId]?.label || planModal.planId)}
                  </p>
                  {planPreviewLoading && (
                    <p className="text-xs text-ink-secondary">{t("billing.calculating") || "Calculando prorrateo…"}</p>
                  )}
                  {planPreviewError && (
                    <AlertBanner variant="red"><p className="text-xs">{planPreviewError}</p></AlertBanner>
                  )}
                  {planPreview && !planPreviewError && (() => {
                    const cents = planPreview.proration_cents;
                    const amt = Math.abs(cents) / 100;
                    const cur = (planPreview.currency || "usd").toUpperCase();
                    const line = cents > 0
                      ? (t("billing.proration_charge") || "Se te cobra ${amt} {cur} ahora · efecto inmediato")
                      : cents < 0
                      ? (t("billing.proration_credit") || "Se te acredita ${amt} {cur} · efecto inmediato")
                      : (t("billing.proration_none") || "Sin cargo adicional · efecto inmediato");
                    return (
                      <p className="text-sm text-ink-secondary">
                        {line.replace("${amt}", amt.toFixed(2)).replace("{cur}", cur)}
                      </p>
                    );
                  })()}
                  <div className="flex gap-2 mt-5">
                    <button onClick={() => setPlanModal(null)} disabled={billingLoading}
                      className="btn-secondary flex-1 h-10 text-xs">
                      {t("settings.cancel") || "Cancelar"}
                    </button>
                    <button onClick={confirmChangePlan}
                      disabled={billingLoading || planPreviewLoading || !!planPreviewError}
                      className="btn-primary flex-1 h-10 text-xs disabled:opacity-40">
                      {billingLoading ? (t("common.opening") || "…") : (t("billing.confirm_change") || "Confirmar cambio")}
                    </button>
                  </div>
                </div>
              </div>
            )}
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
        {youtubePublishingEnabled && activeSection === "youtube" && (
          <>
            {/* ── YouTube account connection ── */}
            <Card>
              <SectionLabel>{t("settings.yt_account")}</SectionLabel>
              <p className="text-xs text-ink-secondary mb-4 -mt-1">{t("settings.yt_account_sub")}</p>

              {ytStatusError && (
                <div className="mb-3"><InlineError message={ytStatusError} /></div>
              )}

              {ytStatus === null && !ytStatusError && (
                <div className="flex items-center gap-2 text-xs text-ink-secondary">
                  <div className="w-4 h-4 border-2 border-brand border-t-transparent rounded-full animate-spin" />
                  {t("settings.yt_checking")}
                </div>
              )}

              {ytStatus && !ytStatus.connected && !ytHelp && (
                <div className="flex items-center gap-4">
                  <div className="flex-1">
                    <p className="text-sm text-ink-secondary">{t("settings.yt_not_connected")}</p>
                  </div>
                  <button onClick={() => { setYtStatusError(null); setYtHelp(true); }}
                    className="shrink-0 btn-primary text-sm py-2 px-4 flex items-center gap-2">
                    <svg className="w-4 h-4" fill="currentColor" viewBox="0 0 24 24">
                      <path d="M22.54 6.42a2.78 2.78 0 00-1.94-2C18.88 4 12 4 12 4s-6.88 0-8.6.46a2.78 2.78 0 00-1.94 2A29 29 0 001 11.75a29 29 0 00.46 5.33A2.78 2.78 0 003.4 19.13C5.12 19.56 12 19.56 12 19.56s6.88 0 8.6-.46a2.78 2.78 0 001.94-2A29 29 0 0023 11.75a29 29 0 00-.46-5.33z"/>
                      <polygon points="9.75 15.02 15.5 11.75 9.75 8.48 9.75 15.02" fill="white"/>
                    </svg>
                    {t("settings.yt_connect")}
                  </button>
                </div>
              )}

              {ytStatus && !ytStatus.connected && ytHelp && (
                <div className="rounded-xl bg-brand/[0.06] ring-1 ring-brand/25 px-4 py-4">
                  <p className="text-sm font-medium text-white mb-2">{t("settings.yt_help_title")}</p>
                  <ol className="text-xs text-ink-secondary space-y-1.5 list-decimal list-inside mb-3">
                    <li>{t("settings.yt_help_unverified")}</li>
                    <li className="font-medium text-brand-light">{t("settings.yt_help_scopes")}</li>
                    <li>{t("settings.yt_help_continue")}</li>
                  </ol>
                  <div className="flex gap-3">
                    <button onClick={handleYtConnect} className="btn-primary text-sm py-2 px-5">
                      {t("settings.yt_help_go")}
                    </button>
                    <button onClick={() => setYtHelp(false)}
                      className="text-xs text-ink-secondary hover:text-white transition-colors">
                      {t("settings.cancel") || "Cancelar"}
                    </button>
                  </div>
                </div>
              )}

              {ytStatus && ytStatus.connected && (
                <div className="flex items-center gap-3">
                  {ytStatus.thumbnail ? (
                    <img src={ytStatus.thumbnail} alt="" className="w-10 h-10 rounded-full ring-1 ring-white/10 shrink-0" />
                  ) : (
                    <div className="w-10 h-10 rounded-full bg-red-500/20 flex items-center justify-center shrink-0 ring-1 ring-white/10">
                      <svg className="w-5 h-5 text-red-400" fill="currentColor" viewBox="0 0 24 24">
                        <path d="M22.54 6.42a2.78 2.78 0 00-1.94-2C18.88 4 12 4 12 4s-6.88 0-8.6.46a2.78 2.78 0 00-1.94 2A29 29 0 001 11.75a29 29 0 00.46 5.33A2.78 2.78 0 003.4 19.13C5.12 19.56 12 19.56 12 19.56s6.88 0 8.6-.46a2.78 2.78 0 001.94-2A29 29 0 0023 11.75a29 29 0 00-.46-5.33z"/>
                        <polygon points="9.75 15.02 15.5 11.75 9.75 8.48 9.75 15.02" fill="white"/>
                      </svg>
                    </div>
                  )}
                  <div className="flex-1 min-w-0">
                    <p className="text-xs text-ink-secondary mb-0.5">{t("settings.yt_connected_as")}</p>
                    <p className="text-sm font-medium text-white truncate">{ytStatus.channel_name || "—"}</p>
                    {ytStatus.channel_url && (
                      <a href={ytStatus.channel_url} target="_blank" rel="noopener noreferrer"
                        className="text-[11px] text-brand-light hover:text-white transition-colors">
                        {t("settings.yt_open_channel")} ↗
                      </a>
                    )}
                  </div>
                  <div className="flex items-center gap-1.5 shrink-0">
                    <span className="w-2 h-2 rounded-full bg-accent inline-block" />
                    <button onClick={handleYtDisconnect} disabled={ytDisconnecting}
                      className="text-xs text-ink-secondary hover:text-red-400 transition-colors disabled:opacity-50">
                      {ytDisconnecting ? t("settings.yt_disconnecting") : t("settings.yt_disconnect")}
                    </button>
                  </div>
                </div>
              )}
            </Card>

            <Card>
              <SectionLabel>{t("settings.app_lang")}</SectionLabel>
              <div className="flex flex-wrap gap-2">
                {[
                  { code: "es", label: t("lang.es") },
                  { code: "en", label: t("lang.en") },
                  { code: "pt", label: t("lang.pt") },
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
                    className="input-field text-sm" placeholder={t("settings.title_format_placeholder")} />
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
                    className="input-field text-sm" placeholder={t("settings.tags_placeholder")} />
                </Field>
                <Field label={t("settings.hashtags")} help={t("settings.hashtags_help")}>
                  <input type="text" value={settings.hashtags}
                    onChange={(e) => update("hashtags", e.target.value)}
                    className="input-field text-sm" placeholder={t("settings.hashtags_placeholder")} />
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
            {settingsDirty && <div className="settings-save-bar">
              <span className="settings-save-bar__copy">{t("settings.unsaved")}</span>
              <button onClick={() => setSettings(savedSettings)} className="btn-secondary !h-10 px-4">{t("settings.cancel")}</button>
              <button onClick={handleSave} disabled={!settingsDirty} className="btn-primary !h-10 px-5 disabled:opacity-40">
                {t("settings.save")}
              </button>
            </div>}
          </>
        )}

        {/* ════════════════════ DISPOSITIVOS ════════════════════ */}
        {activeSection === "dispositivos" && (
          <Card>
            <div className="flex items-start justify-between gap-4 mb-4">
              <div>
                <SectionLabel>{t("settings.active_sessions")}</SectionLabel>
                <p className="text-xs text-ink-secondary -mt-1">
                  {t("settings.active_sessions_sub")}
                </p>
              </div>
              <button
                onClick={handleRevokeOthers}
                disabled={revokingOthers || sessionsLoading || sessions.length <= 1}
                className="shrink-0 text-[12px] font-medium px-4 py-2 rounded-lg bg-surface-3/40 text-ink-secondary ring-1 ring-white/[0.06] hover:text-white hover:ring-white/[0.12] transition-colors disabled:opacity-40 disabled:cursor-not-allowed">
                {revokingOthers ? "…" : t("settings.close_other_sessions")}
              </button>
            </div>

            {sessionsError && <div className="mb-3"><InlineError message={sessionsError} /></div>}

            {sessionsLoading ? (
              <div className="space-y-2">
                {[1, 2].map((i) => (
                  <div key={i} className="h-14 bg-surface-3/30 rounded-xl animate-pulse" />
                ))}
              </div>
            ) : sessions.length === 0 ? (
              <p className="text-sm text-ink-secondary text-center py-4">{t("settings.no_active_sessions")}</p>
            ) : (
              <div className="space-y-0">
                {sessions.map((s, i) => (
                  <div key={s.id}
                    className={`flex items-center justify-between gap-3 py-3 ${i < sessions.length - 1 ? "border-b border-white/[0.03]" : ""}`}>
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <p className="text-sm text-white truncate">{parseUA(s.user_agent, t)}</p>
                        {s.current && (
                          <span className="shrink-0 text-[9px] bg-accent/15 text-accent px-2 py-0.5 rounded-full font-bold uppercase tracking-wider">
                            {t("settings.current_device")}
                          </span>
                        )}
                      </div>
                      <p className="text-[11px] text-gray-600 mt-0.5">
                        {s.ip_address || t("settings.unknown_ip")} · {t("settings.last_activity")} {relativeTime(s.last_seen_at || s.created_at, t, locale)}
                      </p>
                    </div>
                    <button
                      onClick={() => handleRevokeSession(s.id)}
                      disabled={revokingSession === s.id}
                      className="shrink-0 text-label px-3 py-1.5 rounded-lg bg-red-500/10 text-red-400 ring-1 ring-red-500/15 hover:bg-red-500/20 transition-colors disabled:opacity-40">
                      {revokingSession === s.id ? "…" : t("settings.close_session")}
                    </button>
                  </div>
                ))}
              </div>
            )}
          </Card>
        )}

        {/* ════════════════════ MI EQUIPO ════════════════════ */}
        {activeSection === "equipo" && (
          <Card>
            <SectionLabel>{t("settings.team_tab")}</SectionLabel>
            <p className="text-xs text-ink-secondary mb-4 -mt-1">
              {t("settings.team_access")} ({team?.tenant_id || "—"}).
            </p>
            {teamLoading && !team?.members ? (
              <div className="space-y-2">
                {[1, 2].map((i) => (
                  <div key={i} className="h-12 bg-surface-3/30 rounded-xl animate-pulse" />
                ))}
              </div>
            ) : (team?.members || []).length <= 1 ? (
              <div className="flex flex-col items-center text-center py-8 px-4">
                {team?.members?.[0] && (
                  <AvatarImg user={team.members[0]} size="w-12 h-12" textSize="text-lg" />
                )}
                <p className="text-sm text-white font-medium mt-3">{t("settings.team_only_you")}</p>
                <p className="text-xs text-ink-secondary mt-1 max-w-sm">
                  {t("settings.team_only_you_sub")}
                </p>
              </div>
            ) : (
              <div className="space-y-0">
                {(team?.members || []).map((m, i, arr) => (
                  <div key={m.id}
                    className={`flex items-center gap-3 py-3 ${i < arr.length - 1 ? "border-b border-white/[0.03]" : ""}`}>
                    <AvatarImg user={m} size="w-9 h-9" textSize="text-sm" />
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <p className="text-sm text-white truncate">{m.full_name || m.username}</p>
                        {m.is_self && (
                          <span className="shrink-0 text-[9px] bg-brand/20 text-brand-light px-2 py-0.5 rounded-full font-bold uppercase tracking-wider">
                            {t("settings.you")}
                          </span>
                        )}
                      </div>
                      {m.full_name && (
                        <p className="text-[11px] text-gray-600 mt-0.5 truncate">{m.username}</p>
                      )}
                    </div>
                    {m.role === "admin" && (
                      <span className="shrink-0 text-[10px] bg-accent/15 text-accent px-2.5 py-1 rounded-full font-medium">
                        {t("settings.admin")}
                      </span>
                    )}
                  </div>
                ))}
              </div>
            )}
          </Card>
        )}

      </div>
      </div>
    </div>
  );
}
