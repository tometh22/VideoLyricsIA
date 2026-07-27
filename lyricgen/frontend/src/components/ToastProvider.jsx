/**
 * Non-blocking toast provider. Sibling to AlertProvider — but toasts are
 * for ephemeral feedback ("Anclada a 2:58 → 3:04"), not for actionable
 * errors that need acknowledgement.
 *
 * Differences from AlertProvider:
 *  - bottom-right floater (vs centered modal)
 *  - auto-dismiss in ~2s (vs requires Cerrar click)
 *  - stacks up to 3 simultaneously (vs strict queue)
 *  - no Esc / overlay-click handling (no modal semantics)
 *
 * Usage:
 *   <ToastProvider>
 *     <App />
 *   </ToastProvider>
 *
 *   const { toast } = useToast();
 *   toast({ message: "Anclada · 2:58 → 3:04", tone: "info" });
 *   toast("Texto simple");  // shorthand → {message: ...}
 */
import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";

const ToastContext = createContext(null);

// Max toasts visible at the same time. Older toasts pop when this is full.
const MAX_TOASTS = 3;
// Default dismiss timeout (ms). Operator can pass `duration` to override.
const DEFAULT_DURATION_MS = 2000;

const TONE_STYLES = {
  info: "ring-brand/30 bg-surface-2 text-white",
  success: "ring-emerald-500/30 bg-emerald-500/[0.08] text-emerald-200",
  warning: "ring-amber-500/30 bg-amber-500/[0.08] text-amber-200",
  error: "ring-red-500/30 bg-red-500/[0.08] text-red-200",
};

let _nextToastId = 1;

export function ToastProvider({ children }) {
  const [toasts, setToasts] = useState([]);
  // Per-toast timeout ids so we can clear them on unmount or manual close.
  const timersRef = useRef(new Map());

  const removeToast = useCallback((id) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
    const timer = timersRef.current.get(id);
    if (timer) {
      clearTimeout(timer);
      timersRef.current.delete(id);
    }
  }, []);

  const toast = useCallback((arg) => {
    if (arg == null) return null;
    const payload = typeof arg === "string"
      ? { message: arg }
      : { ...arg };
    if (!payload.message) return null;
    const id = _nextToastId++;
    const duration = payload.duration ?? DEFAULT_DURATION_MS;
    const tone = payload.tone || "info";
    setToasts((prev) => {
      const next = [...prev, { id, message: payload.message, tone }];
      // Drop oldest if we're over the cap.
      if (next.length > MAX_TOASTS) {
        const dropped = next.slice(0, next.length - MAX_TOASTS);
        dropped.forEach((t) => {
          const tm = timersRef.current.get(t.id);
          if (tm) {
            clearTimeout(tm);
            timersRef.current.delete(t.id);
          }
        });
        return next.slice(-MAX_TOASTS);
      }
      return next;
    });
    const timer = setTimeout(() => removeToast(id), duration);
    timersRef.current.set(id, timer);
    return id;
  }, [removeToast]);

  // Clean up any pending timers when the provider unmounts.
  useEffect(() => {
    return () => {
      timersRef.current.forEach((t) => clearTimeout(t));
      timersRef.current.clear();
    };
  }, []);

  return (
    <ToastContext.Provider value={{ toast, dismiss: removeToast }}>
      {children}
      <div
        className="fixed bottom-4 right-4 z-[90] flex flex-col gap-2 pointer-events-none"
        aria-live="polite"
        aria-atomic="false"
      >
        {toasts.map((t) => (
          <div
            key={t.id}
            className={
              "pointer-events-auto rounded-xl ring-1 px-3.5 py-2.5 text-caption " +
              "leading-snug shadow-lg backdrop-blur-md max-w-[320px] " +
              "animate-in fade-in slide-in-from-bottom-2 duration-200 " +
              (TONE_STYLES[t.tone] || TONE_STYLES.info)
            }
            role="status"
            onClick={() => removeToast(t.id)}
          >
            {t.message}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast() {
  const ctx = useContext(ToastContext);
  if (!ctx) {
    throw new Error(
      "useToast() must be used inside <ToastProvider>. Wrap the app root in main.jsx."
    );
  }
  return ctx;
}
