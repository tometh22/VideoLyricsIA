/**
 * Provider + hook that replaces native `window.alert()` everywhere.
 *
 * Usage:
 *   // At app root (App.jsx or main.jsx):
 *   <AlertProvider>
 *     <App />
 *   </AlertProvider>
 *
 *   // In any component:
 *   const { alert } = useAlert();
 *   alert({ title: "No pudimos guardar", description: "...", tone: "error" });
 *   alert("Plain string");  // shorthand → {title: string}
 *
 * Behavior:
 * - Only one modal visible at a time (matches native alert ergonomics)
 * - If a second alert fires while one is open, it queues behind it.
 * - Each close pops the next from the queue, or empties the modal.
 * - `useAlert` outside the provider throws — fail loud at boot rather
 *   than silently dropping messages.
 */
import { createContext, useCallback, useContext, useRef, useState } from "react";
import AlertModal from "./AlertModal";

const AlertContext = createContext(null);

export function AlertProvider({ children }) {
  const [current, setCurrent] = useState(null);
  // Pending queue lives in a ref so concurrent alert() calls don't race
  // each other through React's state batching — we always read/write
  // the queue synchronously.
  const queueRef = useRef([]);

  const _normalize = (arg) => {
    if (arg == null) return null;
    if (typeof arg === "string") return { title: arg };
    if (typeof arg === "object") return { title: "", ...arg };
    return { title: String(arg) };
  };

  const alert = useCallback((arg) => {
    const payload = _normalize(arg);
    if (!payload || !payload.title) return;
    setCurrent((prev) => {
      if (prev) {
        queueRef.current.push(payload);
        return prev;
      }
      return payload;
    });
  }, []);

  const handleClose = useCallback(() => {
    if (queueRef.current.length > 0) {
      setCurrent(queueRef.current.shift());
    } else {
      setCurrent(null);
    }
  }, []);

  return (
    <AlertContext.Provider value={{ alert }}>
      {children}
      <AlertModal
        open={!!current}
        onClose={handleClose}
        title={current?.title || ""}
        description={current?.description}
        tone={current?.tone}
        actionLabel={current?.actionLabel}
      />
    </AlertContext.Provider>
  );
}

export function useAlert() {
  const ctx = useContext(AlertContext);
  if (!ctx) {
    throw new Error(
      "useAlert() must be used inside <AlertProvider>. Wrap the app root in main.jsx."
    );
  }
  return ctx;
}
