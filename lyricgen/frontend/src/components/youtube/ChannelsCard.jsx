import { useState, useEffect, useCallback } from "react";
import { useI18n } from "../../i18n";

const API = import.meta.env.VITE_API_URL || "";

function authHeaders() {
  const token = localStorage.getItem("genly_token");
  return token ? { Authorization: `Bearer ${token}` } : {};
}

// One-shot flash set by RootEffects when the OAuth round-trip returns.
function readFlash() {
  const ok = sessionStorage.getItem("yt_connected_flash");
  const err = sessionStorage.getItem("yt_error_flash");
  sessionStorage.removeItem("yt_connected_flash");
  sessionStorage.removeItem("yt_error_flash");
  return { ok: ok === "1", err };
}

function YouTubeIcon({ className = "w-5 h-5" }) {
  return (
    <svg className={className} fill="currentColor" viewBox="0 0 24 24">
      <path d="M22.54 6.42a2.78 2.78 0 00-1.94-2C18.88 4 12 4 12 4s-6.88 0-8.6.46a2.78 2.78 0 00-1.94 2A29 29 0 001 11.75a29 29 0 00.46 5.33A2.78 2.78 0 003.4 19.13C5.12 19.56 12 19.56 12 19.56s6.88 0 8.6-.46a2.78 2.78 0 001.94-2A29 29 0 0023 11.75a29 29 0 00-.46-5.33z" />
      <polygon points="9.75 15.02 15.5 11.75 9.75 8.48 9.75 15.02" fill="black" />
    </svg>
  );
}

function ChannelRow({ channel, onDefault, onDisconnect, onReconnect, busy }) {
  const { t } = useI18n();
  const [confirming, setConfirming] = useState(false);
  const needsReconnect = channel.status !== "active";

  return (
    <div className="flex items-center gap-3 py-3 border-b border-white/[0.04] last:border-0">
      {channel.thumbnail_url ? (
        <img src={channel.thumbnail_url} alt="" className="w-10 h-10 rounded-full shrink-0" referrerPolicy="no-referrer" />
      ) : (
        <div className="w-10 h-10 rounded-full bg-surface-3 flex items-center justify-center text-sm font-semibold text-ink-secondary shrink-0">
          {(channel.channel_title || "?").charAt(0).toUpperCase()}
        </div>
      )}

      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-sm text-white font-medium truncate">{channel.channel_title || channel.channel_id}</span>
          {channel.is_default && (
            <span className="px-2 py-0.5 rounded-full text-[10px] font-medium bg-brand/15 text-brand-light ring-1 ring-brand/40">
              {t("yt.channels.default_badge")}
            </span>
          )}
          {needsReconnect && (
            <span className="px-2 py-0.5 rounded-full text-[10px] font-medium bg-amber-500/15 text-amber-400 ring-1 ring-amber-500/30">
              {t("yt.channels.revoked")}
            </span>
          )}
        </div>
        <p className="text-[11px] text-gray-600 truncate">{channel.channel_id}</p>
      </div>

      <div className="flex items-center gap-3 shrink-0">
        {confirming ? (
          <>
            <span className="text-xs text-gray-400 hidden sm:inline">{t("yt.channels.disconnect_confirm")}</span>
            <button onClick={() => { setConfirming(false); onDisconnect(channel); }} disabled={busy}
              className="text-xs text-red-400 hover:text-red-300 font-medium transition-colors disabled:opacity-50">
              {t("yt.channels.confirm")}
            </button>
            <button onClick={() => setConfirming(false)}
              className="text-xs text-gray-500 hover:text-white transition-colors">
              {t("detail.cancel")}
            </button>
          </>
        ) : (
          <>
            {needsReconnect && (
              <button onClick={() => onReconnect()} disabled={busy}
                className="text-xs text-brand-light hover:text-white font-medium transition-colors disabled:opacity-50">
                {t("yt.channels.reconnect")}
              </button>
            )}
            {!channel.is_default && !needsReconnect && (
              <button onClick={() => onDefault(channel)} disabled={busy}
                className="text-xs text-gray-500 hover:text-white transition-colors disabled:opacity-50">
                {t("yt.channels.make_default")}
              </button>
            )}
            <button onClick={() => setConfirming(true)} disabled={busy}
              className="text-xs text-gray-600 hover:text-red-400 transition-colors disabled:opacity-50">
              {t("yt.channels.disconnect")}
            </button>
          </>
        )}
      </div>
    </div>
  );
}

export default function ChannelsCard() {
  const { t } = useI18n();
  const [channels, setChannels] = useState(null); // null = loading
  const [error, setError] = useState(null);
  const [flash, setFlash] = useState(() => readFlash());
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const res = await fetch(`${API}/youtube/channels`, { headers: authHeaders() });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setChannels(await res.json());
      setError(null);
    } catch {
      setChannels([]);
      setError(t("yt.channels.load_error"));
    }
  }, [t]);

  useEffect(() => { load(); }, [load]);

  useEffect(() => {
    if (!flash.ok && !flash.err) return;
    const timer = setTimeout(() => setFlash({ ok: false, err: null }), 6000);
    return () => clearTimeout(timer);
  }, [flash]);

  const [showHelp, setShowHelp] = useState(false);

  const connect = async () => {
    setBusy(true);
    try {
      const res = await fetch(`${API}/youtube/channels/connect`, { method: "POST", headers: authHeaders() });
      const data = await res.json();
      if (!res.ok || !data.auth_url) {
        setError(data.detail || t("yt.channels.error_generic"));
        setBusy(false);
        return;
      }
      window.location.href = data.auth_url;
    } catch {
      setError(t("yt.channels.error_generic"));
      setBusy(false);
    }
  };

  // Google shows two confusing screens ("app not verified" + unchecked
  // permission boxes). Walk the user through them before we redirect.
  const startConnect = () => {
    setError(null);
    setShowHelp(true);
  };

  const disconnect = async (channel) => {
    setBusy(true);
    try {
      await fetch(`${API}/youtube/channels/${channel.id}`, { method: "DELETE", headers: authHeaders() });
      await load();
    } catch {}
    setBusy(false);
  };

  const makeDefault = async (channel) => {
    setBusy(true);
    try {
      await fetch(`${API}/youtube/channels/${channel.id}/default`, { method: "POST", headers: authHeaders() });
      await load();
    } catch {}
    setBusy(false);
  };

  const errorFor = (code) => {
    if (code === "access_denied") return t("yt.channels.error_denied");
    if (code === "scopes") return t("yt.channels.error_scopes");
    if (code === "no_channel") return t("yt.channels.error_no_channel");
    return t("yt.channels.error_generic");
  };
  const errorMessage = flash.err ? errorFor(flash.err) : error;

  // Instructions modal shown before redirecting to Google.
  const helpSteps = showHelp && (
    <div className="mb-4 rounded-xl bg-brand/[0.06] ring-1 ring-brand/25 px-4 py-4 animate-fade-in">
      <p className="text-sm font-medium text-white mb-2">{t("yt.channels.help_title")}</p>
      <ol className="text-xs text-gray-300 space-y-1.5 list-decimal list-inside mb-3">
        <li>{t("yt.channels.help_step_unverified")}</li>
        <li className="font-medium text-brand-light">{t("yt.channels.help_step_scopes")}</li>
        <li>{t("yt.channels.help_step_continue")}</li>
      </ol>
      <div className="flex gap-3">
        <button onClick={connect} disabled={busy} className="btn-primary text-sm py-2 px-5 disabled:opacity-50">
          {busy ? "..." : t("yt.channels.help_go")}
        </button>
        <button onClick={() => setShowHelp(false)} className="text-xs text-gray-500 hover:text-white transition-colors">
          {t("detail.cancel")}
        </button>
      </div>
    </div>
  );

  return (
    <div>
      {flash.ok && (
        <div className="mb-4 rounded-xl bg-accent/[0.08] ring-1 ring-accent/25 px-4 py-3 flex items-center gap-2 animate-fade-in">
          <svg className="w-4 h-4 text-accent shrink-0" fill="none" stroke="currentColor" strokeWidth="2.5" viewBox="0 0 24 24">
            <polyline points="20 6 9 17 4 12" />
          </svg>
          <p className="text-sm text-accent">{t("yt.channels.connected_flash")}</p>
        </div>
      )}
      {errorMessage && (
        <div className="mb-4 rounded-xl bg-red-500/10 border border-red-500/20 px-4 py-3">
          <p className="text-sm text-red-400">{errorMessage}</p>
        </div>
      )}

      {helpSteps}

      {channels === null ? (
        <div className="space-y-3 animate-pulse">
          {[0, 1].map((i) => (
            <div key={i} className="flex items-center gap-3">
              <div className="w-10 h-10 rounded-full bg-surface-3" />
              <div className="flex-1 space-y-2">
                <div className="h-3 bg-surface-3 rounded w-1/3" />
                <div className="h-2 bg-surface-3 rounded w-1/4" />
              </div>
            </div>
          ))}
        </div>
      ) : channels.length === 0 ? (
        <div className="text-center py-8">
          <div className="w-12 h-12 mx-auto mb-3 rounded-2xl bg-red-500/10 flex items-center justify-center text-red-500">
            <YouTubeIcon className="w-6 h-6" />
          </div>
          <p className="text-sm font-medium text-white mb-1">{t("yt.channels.empty_title")}</p>
          <p className="text-xs text-gray-500 mb-4 max-w-sm mx-auto">{t("yt.channels.empty_sub")}</p>
          <button onClick={startConnect} disabled={busy || showHelp} className="btn-primary text-sm py-2.5 px-5 disabled:opacity-50">
            {busy ? "..." : t("yt.channels.connect")}
          </button>
        </div>
      ) : (
        <>
          <div>
            {channels.map((c) => (
              <ChannelRow key={c.id} channel={c} busy={busy}
                onDefault={makeDefault} onDisconnect={disconnect} onReconnect={connect} />
            ))}
          </div>
          <button onClick={startConnect} disabled={busy || showHelp}
            className="mt-4 text-xs text-brand-light hover:text-white transition-colors disabled:opacity-50">
            + {t("yt.channels.connect_another")}
          </button>
        </>
      )}
    </div>
  );
}
