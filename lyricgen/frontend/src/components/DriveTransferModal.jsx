import { useEffect, useRef, useState } from "react";
import { useI18n } from "../i18n";

const API = import.meta.env.VITE_API_URL || "";

function authHeaders() {
  const token = localStorage.getItem("genly_token");
  return token
    ? { Authorization: `Bearer ${token}`, "Content-Type": "application/json" }
    : { "Content-Type": "application/json" };
}

function formatBytes(n) {
  if (!n || n < 0) return "0 B";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

/**
 * Modal que dispara una transferencia R2 → Drive y polea el progress.
 * Props:
 *  - jobId, fileType: qué transferir
 *  - onClose: callback al cerrar
 */
export default function DriveTransferModal({ jobId, fileType, onClose }) {
  const { t } = useI18n();
  const [transferId, setTransferId] = useState(null);
  const [status, setStatus] = useState(null); // {status, progress_pct, bytes_*, web_view_link, error}
  const [submitError, setSubmitError] = useState(null);
  const mountedRef = useRef(true);
  const pollTimerRef = useRef(null);

  useEffect(() => () => {
    mountedRef.current = false;
    if (pollTimerRef.current) clearTimeout(pollTimerRef.current);
  }, []);

  // Disparar la transferencia al montar
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch(`${API}/jobs/${jobId}/deliver-to-drive`, {
          method: "POST",
          headers: authHeaders(),
          body: JSON.stringify({ file_type: fileType }),
        });
        if (!res.ok) {
          const data = await res.json().catch(() => ({}));
          throw new Error(data.detail || `Error ${res.status}`);
        }
        const data = await res.json();
        if (!cancelled && mountedRef.current) {
          setTransferId(data.transfer_id);
          setStatus({ status: "queued", progress_pct: 0 });
        }
      } catch (err) {
        if (!cancelled && mountedRef.current) {
          setSubmitError(err.message || String(err));
        }
      }
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId, fileType]);

  // Polling loop
  useEffect(() => {
    if (!transferId) return;
    let cancelled = false;

    const poll = async () => {
      try {
        const res = await fetch(`${API}/drive/transfers/${transferId}`, { headers: authHeaders() });
        if (!res.ok) {
          // Si 404 (transferencia desapareció) o 401 (token expirado),
          // paramos el loop con error visible. Otros transient → reintento.
          if (res.status === 404 || res.status === 401) {
            const data = await res.json().catch(() => ({}));
            throw new Error(data.detail || `Error ${res.status}`);
          }
          // Transient — reintento próximo tick
          if (!cancelled && mountedRef.current) {
            pollTimerRef.current = setTimeout(poll, 5000);
          }
          return;
        }
        const data = await res.json();
        if (cancelled || !mountedRef.current) return;
        setStatus(data);

        // Si terminó (done o error), paramos el polling.
        if (data.status === "done" || data.status === "error") return;

        // Sino, próximo poll en 3s.
        pollTimerRef.current = setTimeout(poll, 3000);
      } catch (err) {
        if (!cancelled && mountedRef.current) {
          setSubmitError(err.message || String(err));
        }
      }
    };

    poll();
    return () => {
      cancelled = true;
      if (pollTimerRef.current) clearTimeout(pollTimerRef.current);
    };
  }, [transferId]);

  const isDone = status?.status === "done";
  const isError = status?.status === "error" || !!submitError;
  const errorMessage = submitError || status?.error;
  const progress = status?.progress_pct ?? 0;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm"
      onClick={() => {
        // Solo permitimos close click-outside cuando done/error.
        // Durante la transferencia el user debe usar el botón Cerrar
        // para evitar dismiss accidental mientras corre.
        if (isDone || isError) onClose?.();
      }}
    >
      <div
        className="w-full max-w-md mx-4 bg-surface-1 ring-1 ring-white/[0.08] rounded-2xl p-6 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 className="text-lg font-semibold text-white mb-1">
          {t("drive.transfer_title")}
        </h3>
        <p className="text-xs text-gray-400 mb-5">
          {isDone
            ? t("drive.transfer_done_desc")
            : isError
            ? t("drive.transfer_error_desc")
            : t("drive.transfer_running_desc")}
        </p>

        {/* Progress */}
        {!isError && (
          <div className="space-y-2">
            <div className="h-2 rounded-full bg-surface-3/60 overflow-hidden">
              <div
                className="h-full bg-gradient-to-r from-brand to-brand-light transition-[width] duration-700 ease-out"
                style={{ width: `${Math.max(2, progress)}%` }}
              />
            </div>
            <div className="flex justify-between text-[11px] font-mono text-gray-500">
              <span>{progress}%</span>
              <span>
                {formatBytes(status?.bytes_transferred)}
                {status?.bytes_total ? ` / ${formatBytes(status.bytes_total)}` : ""}
              </span>
            </div>
            <div className="text-[11px] text-gray-500">
              {status?.status === "queued" && t("drive.transfer_queued")}
              {status?.status === "running" && t("drive.transfer_running")}
              {status?.status === "done" && t("drive.transfer_complete")}
            </div>
          </div>
        )}

        {/* Error */}
        {isError && (
          <div className="text-xs text-red-300 px-3 py-2 rounded-md bg-red-500/10 ring-1 ring-red-500/30">
            {errorMessage || t("drive.transfer_error_generic")}
          </div>
        )}

        {/* Actions */}
        <div className="flex gap-2 mt-6">
          {isDone && status?.web_view_link && (
            <a
              href={status.web_view_link}
              target="_blank"
              rel="noopener noreferrer"
              className="flex-1 py-2 rounded-md text-sm font-medium text-center text-white bg-brand hover:bg-brand-strong ring-1 ring-brand/30 transition-colors"
            >
              {t("drive.transfer_view_in_drive")}
            </a>
          )}
          <button
            type="button"
            onClick={onClose}
            disabled={!isDone && !isError}
            className="flex-1 py-2 rounded-md text-sm font-medium text-gray-300 bg-surface-3/40 hover:bg-surface-3/60 ring-1 ring-white/[0.04] transition-colors disabled:opacity-40"
          >
            {isDone || isError ? t("drive.transfer_close") : t("drive.transfer_running_keep_open")}
          </button>
        </div>
      </div>
    </div>
  );
}
