import { useState } from "react";
import { useI18n } from "../i18n";

export default function LoginPage({ onLogin }) {
  const { t, lang, setLang } = useI18n();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e) => {
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

  return (
    <div className="min-h-screen bg-surface flex items-center justify-center relative overflow-hidden">
      <div className="fixed inset-0 pointer-events-none">
        <div className="absolute top-[-20%] left-[10%] w-[700px] h-[700px] bg-brand/[0.05] rounded-full blur-[150px]" />
        <div className="absolute bottom-[-10%] right-[5%] w-[500px] h-[500px] bg-brand-light/[0.04] rounded-full blur-[120px]" />
        <div className="absolute top-[40%] right-[20%] w-[300px] h-[300px] bg-accent/[0.03] rounded-full blur-[100px]" />
      </div>

      {/* Language selector - top right */}
      <div className="fixed top-5 right-5 z-20">
        <select
          value={lang}
          onChange={(e) => setLang(e.target.value)}
          className="px-3 py-1.5 rounded-lg bg-surface-2/60 backdrop-blur-xl border border-white/[0.06]
            text-xs text-gray-400 appearance-none cursor-pointer focus:outline-none hover:border-white/[0.1] transition-all"
        >
          <option value="es">Español</option>
          <option value="en">English</option>
          <option value="pt">Português</option>
        </select>
      </div>

      <div className="relative z-10 w-full max-w-md mx-4 animate-fade-in">
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

        <div className="glass rounded-3xl p-8 shadow-glow">
          <h2 className="text-lg font-bold mb-1">{t("login.title")}</h2>
          <p className="text-xs text-gray-500 mb-6">{t("login.subtitle")}</p>

          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <label className="block text-xs text-gray-400 mb-1.5 ml-1">{t("login.username")}</label>
              <input
                type="text" value={username} onChange={(e) => setUsername(e.target.value)}
                className="input-field" placeholder={t("login.username_placeholder")}
                autoComplete="username" autoFocus
              />
            </div>
            <div>
              <label className="block text-xs text-gray-400 mb-1.5 ml-1">{t("login.password")}</label>
              <input
                type="password" value={password} onChange={(e) => setPassword(e.target.value)}
                className="input-field" placeholder={t("login.password_placeholder")}
                autoComplete="current-password"
              />
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
        </div>

        <p className="text-center text-[11px] text-gray-600 mt-8">{t("login.footer")}</p>
      </div>
    </div>
  );
}
