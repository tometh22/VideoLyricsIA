import { useEffect, useRef, useState } from "react";
import { useI18n } from "../i18n";

const API = import.meta.env.VITE_API_URL || "";

function authHeaders() {
  const token = localStorage.getItem("genly_token");
  return token ? { Authorization: `Bearer ${token}` } : {};
}

/**
 * Card en Settings para conectar/desconectar Google Drive. Polea
 * /drive/status al mount y muestra el estado adecuado.
 *
 * Flow OAuth: click "Conectar" → fetch /drive/auth-url → window.location
 * redirige a Google → user autoriza → Google redirige a /drive/callback
 * (backend) → backend redirige a /settings?drive=connected. Acá detectamos
 * el query param y refrescamos el status.
 */
export default function DriveConnectButton() {
  const { t } = useI18n();
  const [status, setStatus] = useState(null); // null = loading, {connected, email?} else
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const mountedRef = useRef(true);
  useEffect(() => () => { mountedRef.current = false; }, []);

  const loadStatus = async () => {
    try {
      const res = await fetch(`${API}/drive/status`, { headers: authHeaders() });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || `Error ${res.status}`);
      }
      const data = await res.json();
      if (mountedRef.current) setStatus(data);
    } catch (err) {
      if (mountedRef.current) {
        setError(err.message || String(err));
        setStatus({ connected: false });
      }
    }
  };

  useEffect(() => {
    loadStatus();

    // Si volvemos del OAuth callback (?drive=connected|error en la URL),
    // refrescamos el status y mostramos toast adecuado.
    const params = new URLSearchParams(window.location.search);
    const driveParam = params.get("drive");
    if (driveParam === "connected") {
      // Limpiar el query param de la URL sin recargar
      const url = new URL(window.location.href);
      url.searchParams.delete("drive");
      window.history.replaceState({}, "", url.toString());
      // El status se refresca a su tiempo via el fetch inicial
    } else if (driveParam === "error") {
      const reason = params.get("reason") || "unknown";
      setError(t("drive.callback_error") + ": " + reason);
      const url = new URL(window.location.href);
      url.searchParams.delete("drive");
      url.searchParams.delete("reason");
      window.history.replaceState({}, "", url.toString());
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleConnect = async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(`${API}/drive/auth-url`, { headers: authHeaders() });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || `Error ${res.status}`);
      }
      const { auth_url } = await res.json();
      // Full redirect — el callback de Google vuelve a /drive/callback en
      // backend, que redirige a /settings?drive=connected en frontend.
      window.location.href = auth_url;
    } catch (err) {
      if (mountedRef.current) {
        setError(err.message || String(err));
        setBusy(false);
      }
    }
  };

  const handleDisconnect = async () => {
    if (busy) return;
    if (!window.confirm(t("drive.disconnect_confirm"))) return;
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(`${API}/drive/disconnect`, {
        method: "DELETE",
        headers: authHeaders(),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || `Error ${res.status}`);
      }
      if (mountedRef.current) {
        setStatus({ connected: false });
      }
    } catch (err) {
      if (mountedRef.current) {
        setError(err.message || String(err));
      }
    } finally {
      if (mountedRef.current) setBusy(false);
    }
  };

  if (status === null) {
    return (
      <div className="rounded-card bg-surface-2/40 ring-1 ring-white/[0.04] p-5 flex items-center gap-3">
        <div className="w-5 h-5 border-2 border-brand border-t-transparent rounded-full animate-spin" />
        <span className="text-sm text-gray-400">{t("drive.loading_status")}</span>
      </div>
    );
  }

  if (status.connected) {
    return (
      <div className="rounded-card bg-surface-2/40 ring-1 ring-white/[0.04] p-5">
        <div className="flex items-start gap-3">
          <div className="w-10 h-10 shrink-0 rounded-xl bg-accent/15 ring-1 ring-accent/30 flex items-center justify-center text-accent">
            <svg className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
              <path d="M5 13l4 4L19 7" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </div>
          <div className="flex-1 min-w-0">
            <div className="text-sm font-semibold text-white">{t("drive.connected_title")}</div>
            <div className="text-xs text-ink-secondary mt-0.5">
              {status.email
                ? `${t("drive.connected_as")} ${status.email}`
                : t("drive.connected_generic")}
            </div>
          </div>
          <button
            type="button"
            onClick={handleDisconnect}
            disabled={busy}
            className="shrink-0 px-3 py-1.5 rounded-md text-xs font-medium text-gray-300 bg-surface-3/40 hover:bg-surface-3/60 ring-1 ring-white/[0.04] transition-colors disabled:opacity-40"
          >
            {t("drive.disconnect")}
          </button>
        </div>
        {error && (
          <div className="mt-3 text-xs text-red-300 px-3 py-2 rounded-md bg-red-500/10 ring-1 ring-red-500/30">
            {error}
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="rounded-card bg-surface-2/40 ring-1 ring-white/[0.04] p-5">
      <div className="flex items-start gap-3">
        <div className="w-10 h-10 shrink-0 rounded-xl bg-brand/10 ring-1 ring-brand/30 flex items-center justify-center text-brand">
          <svg className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
            <path d="M12 4v12m0 0l-4-4m4 4l4-4M4 20h16" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </div>
        <div className="flex-1 min-w-0">
          <div className="text-sm font-semibold text-white">{t("drive.connect_title")}</div>
          <div className="text-xs text-ink-secondary mt-0.5">
            {t("drive.connect_description")}
          </div>
        </div>
        <button
          type="button"
          onClick={handleConnect}
          disabled={busy}
          className="shrink-0 px-4 py-2 rounded-md text-sm font-medium text-white bg-brand hover:bg-brand-strong ring-1 ring-brand/30 transition-colors disabled:opacity-40"
        >
          {busy ? t("drive.connecting") : t("drive.connect_button")}
        </button>
      </div>
      {error && (
        <div className="mt-3 text-xs text-red-300 px-3 py-2 rounded-md bg-red-500/10 ring-1 ring-red-500/30">
          {error}
        </div>
      )}
    </div>
  );
}
