import { useState } from "react";
import { useI18n } from "../i18n";

export default function LoginPage({ onLogin, onBack }) {
  const { t, lang, setLang } = useI18n();
  const [mode, setMode] = useState("login"); // login, register, forgot, reset_sent
  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState("");

  const handleLogin = async (e) => {
    e.preventDefault();
    if (!username.trim() || !password.trim()) return;
    setLoading(true);
    setError("");
    try {
      const res = await fetch("/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: username.trim(), password }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || t("login.error"));
      }
      const data = await res.json();
      onLogin(data.token, data.user);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  const handleRegister = async (e) => {
    e.preventDefault();
    if (!username.trim() || !password.trim()) return;
    if (password !== confirmPassword) {
      setError(t("login.passwords_mismatch"));
      return;
    }
    if (password.length < 8) {
      setError(t("login.password_min"));
      return;
    }
    setLoading(true);
    setError("");
    try {
      const res = await fetch("/auth/register", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: username.trim(), password, email: email.trim() }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || t("login.register_error"));
      }
      const data = await res.json();
      onLogin(data.token, data.user);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  const handleForgotPassword = async (e) => {
    e.preventDefault();
    if (!email.trim()) return;
    setLoading(true);
    setError("");
    try {
      const res = await fetch("/auth/forgot-password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: email.trim() }),
      });
      const data = await res.json();
      setMessage(data.message || t("login.reset_sent"));
      setMode("reset_sent");
    } catch {
      setError(t("login.error"));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen bg-surface flex items-center justify-center relative overflow-hidden">
      <div className="fixed inset-0 pointer-events-none">
        <div className="absolute top-[-20%] left-[10%] w-[700px] h-[700px] bg-brand/[0.05] rounded-full blur-[150px]" />
        <div className="absolute bottom-[-10%] right-[5%] w-[500px] h-[500px] bg-brand-light/[0.04] rounded-full blur-[120px]" />
        <div className="absolute top-[40%] right-[20%] w-[300px] h-[300px] bg-accent/[0.03] rounded-full blur-[100px]" />
      </div>

      {/* Top bar */}
      <div className="fixed top-0 left-0 right-0 z-20 flex items-center justify-between px-6 py-4">
        {onBack && (
          <button onClick={onBack} className="flex items-center gap-2 text-sm text-gray-400 hover:text-white transition-colors">
            <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
              <path d="M19 12H5M12 19l-7-7 7-7" strokeLinecap="round" strokeLinejoin="round"/>
            </svg>
            {t("detail.back")}
          </button>
        )}
        <div className="flex items-center gap-1 ml-auto">
          {["es", "en", "pt"].map((code) => (
            <button
              key={code}
              onClick={() => setLang(code)}
              className={`text-[10px] font-bold px-2.5 py-1.5 rounded-lg transition-all uppercase
                ${lang === code ? "text-white bg-white/10" : "text-gray-600 hover:text-gray-400"}`}
            >
              {code}
            </button>
          ))}
        </div>
      </div>

      <div className="relative z-10 w-full max-w-md mx-4 animate-fade-in">
        {/* Logo */}
        <div className="text-center mb-10">
          <div className="inline-flex items-center justify-center w-16 h-16 rounded-2xl bg-gradient-to-br from-brand to-brand-light shadow-glow mb-5">
            <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
              <path d="M9 18V5l12-2v13" /><circle cx="6" cy="18" r="3" /><circle cx="18" cy="16" r="3" />
            </svg>
          </div>
          <h1 className="text-3xl font-extrabold tracking-tight mb-2">
            <span className="bg-gradient-to-r from-white to-gray-400 bg-clip-text text-transparent">GenLy </span>
            <span className="bg-gradient-to-r from-brand to-brand-light bg-clip-text text-transparent">AI</span>
          </h1>
          <p className="text-sm text-gray-500">{t("login.platform")}</p>
        </div>

        {/* Card */}
        <div className="glass rounded-3xl p-8 shadow-glow">
          {/* Login */}
          {mode === "login" && (
            <>
              <h2 className="text-lg font-bold mb-1">{t("login.title")}</h2>
              <p className="text-xs text-gray-500 mb-6">{t("login.subtitle")}</p>
              <form onSubmit={handleLogin} className="space-y-4">
                <div>
                  <label className="block text-xs text-gray-400 mb-1.5 ml-1">{t("login.username")}</label>
                  <input type="text" value={username} onChange={(e) => setUsername(e.target.value)}
                    className="input-field" placeholder={t("login.username_placeholder")}
                    autoComplete="username" autoFocus />
                </div>
                <div>
                  <label className="block text-xs text-gray-400 mb-1.5 ml-1">{t("login.password")}</label>
                  <input type="password" value={password} onChange={(e) => setPassword(e.target.value)}
                    className="input-field" placeholder={t("login.password_placeholder")}
                    autoComplete="current-password" />
                </div>
                {error && (
                  <div className="rounded-xl bg-red-500/10 border border-red-500/20 px-4 py-3">
                    <p className="text-sm text-red-400">{error}</p>
                  </div>
                )}
                <button type="submit" disabled={loading || !username.trim() || !password.trim()} className="btn-primary w-full py-4 mt-2">
                  {loading ? (
                    <span className="flex items-center justify-center gap-2">
                      <span className="w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin" />
                      {t("login.loading")}
                    </span>
                  ) : t("login.submit")}
                </button>
              </form>
              <div className="mt-6 space-y-3 text-center">
                <button onClick={() => { setMode("register"); setError(""); }}
                  className="text-sm text-brand hover:text-brand-light transition-colors">
                  {t("login.no_account")}
                </button>
                <br />
                <button onClick={() => { setMode("forgot"); setError(""); }}
                  className="text-xs text-gray-500 hover:text-gray-300 transition-colors">
                  {t("login.forgot_password")}
                </button>
              </div>
            </>
          )}

          {/* Register */}
          {mode === "register" && (
            <>
              <h2 className="text-lg font-bold mb-1">{t("login.register_title")}</h2>
              <p className="text-xs text-gray-500 mb-6">{t("login.register_subtitle")}</p>
              <form onSubmit={handleRegister} className="space-y-4">
                <div>
                  <label className="block text-xs text-gray-400 mb-1.5 ml-1">{t("login.username")}</label>
                  <input type="text" value={username} onChange={(e) => setUsername(e.target.value)}
                    className="input-field" placeholder={t("login.username_placeholder")} autoFocus />
                </div>
                <div>
                  <label className="block text-xs text-gray-400 mb-1.5 ml-1">Email</label>
                  <input type="email" value={email} onChange={(e) => setEmail(e.target.value)}
                    className="input-field" placeholder="tu@email.com" />
                </div>
                <div>
                  <label className="block text-xs text-gray-400 mb-1.5 ml-1">{t("login.password")}</label>
                  <input type="password" value={password} onChange={(e) => setPassword(e.target.value)}
                    className="input-field" placeholder={t("login.password_min")} />
                </div>
                <div>
                  <label className="block text-xs text-gray-400 mb-1.5 ml-1">{t("login.confirm_password")}</label>
                  <input type="password" value={confirmPassword} onChange={(e) => setConfirmPassword(e.target.value)}
                    className="input-field" placeholder={t("login.confirm_password")} />
                </div>
                {error && (
                  <div className="rounded-xl bg-red-500/10 border border-red-500/20 px-4 py-3">
                    <p className="text-sm text-red-400">{error}</p>
                  </div>
                )}
                <button type="submit" disabled={loading || !username.trim() || !password.trim()}
                  className="btn-primary w-full py-4 mt-2">
                  {loading ? (
                    <span className="flex items-center justify-center gap-2">
                      <span className="w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin" />
                      {t("login.loading")}
                    </span>
                  ) : t("login.register_submit")}
                </button>
              </form>
              <div className="mt-6 text-center">
                <button onClick={() => { setMode("login"); setError(""); }}
                  className="text-sm text-gray-500 hover:text-gray-300 transition-colors">
                  {t("login.has_account")}
                </button>
              </div>
            </>
          )}

          {/* Forgot password */}
          {mode === "forgot" && (
            <>
              <h2 className="text-lg font-bold mb-1">{t("login.forgot_title")}</h2>
              <p className="text-xs text-gray-500 mb-6">{t("login.forgot_subtitle")}</p>
              <form onSubmit={handleForgotPassword} className="space-y-4">
                <div>
                  <label className="block text-xs text-gray-400 mb-1.5 ml-1">Email</label>
                  <input type="email" value={email} onChange={(e) => setEmail(e.target.value)}
                    className="input-field" placeholder="tu@email.com" autoFocus />
                </div>
                {error && (
                  <div className="rounded-xl bg-red-500/10 border border-red-500/20 px-4 py-3">
                    <p className="text-sm text-red-400">{error}</p>
                  </div>
                )}
                <button type="submit" disabled={loading || !email.trim()} className="btn-primary w-full py-4 mt-2">
                  {loading ? (
                    <span className="flex items-center justify-center gap-2">
                      <span className="w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin" />
                    </span>
                  ) : t("login.send_reset")}
                </button>
              </form>
              <div className="mt-6 text-center">
                <button onClick={() => { setMode("login"); setError(""); }}
                  className="text-sm text-gray-500 hover:text-gray-300 transition-colors">
                  {t("login.back_to_login")}
                </button>
              </div>
            </>
          )}

          {/* Reset sent confirmation */}
          {mode === "reset_sent" && (
            <div className="text-center py-4">
              <div className="w-14 h-14 mx-auto mb-4 rounded-2xl bg-accent/10 flex items-center justify-center">
                <svg className="w-7 h-7 text-accent" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                  <polyline points="20 6 9 17 4 12" />
                </svg>
              </div>
              <h2 className="text-lg font-bold mb-2">{t("login.reset_sent_title")}</h2>
              <p className="text-sm text-gray-400 mb-6">{message}</p>
              <button onClick={() => { setMode("login"); setError(""); setMessage(""); }}
                className="text-sm text-brand hover:text-brand-light transition-colors">
                {t("login.back_to_login")}
              </button>
            </div>
          )}
        </div>

        <p className="text-center text-[11px] text-gray-600 mt-8">{t("login.footer")}</p>
      </div>
    </div>
  );
}
