/**
 * Themed alert dialog — replaces the native `window.alert()` calls
 * that bombed the dark UI with their OS-default white boxes.
 *
 * Visually mirrors the existing styled inline errors used elsewhere
 * (EnableProResModal, LoginPage, etc.) — same rounded surface, same
 * red ring for errors, same brand button.
 *
 * Don't render this directly. Use the `useAlert` hook from
 * `AlertProvider.jsx` so a single instance owns the modal state and
 * stacks new calls into a queue (mimics `window.alert()` ergonomics).
 *
 * Props:
 *   open          — boolean, controls visibility
 *   onClose       — fn(), called on overlay click / Cerrar / Escape
 *   title         — required, short headline
 *   description   — optional, longer explanation (preserves newlines)
 *   tone          — "error" (default) | "warning" | "success" | "info"
 *   actionLabel   — optional CTA text (default: "Cerrar")
 */
import { useEffect } from "react";

const TONE_STYLES = {
  error: {
    ring: "ring-red-500/30",
    iconBg: "bg-red-500/15 ring-red-500/40",
    iconColor: "text-red-300",
    titleColor: "text-red-300",
  },
  warning: {
    ring: "ring-amber-500/30",
    iconBg: "bg-amber-500/15 ring-amber-500/40",
    iconColor: "text-amber-300",
    titleColor: "text-amber-300",
  },
  success: {
    ring: "ring-emerald-500/30",
    iconBg: "bg-emerald-500/15 ring-emerald-500/40",
    iconColor: "text-emerald-300",
    titleColor: "text-emerald-300",
  },
  info: {
    ring: "ring-brand/30",
    iconBg: "bg-brand/15 ring-brand/40",
    iconColor: "text-brand-light",
    titleColor: "text-white",
  },
};

function Icon({ tone, className }) {
  // Single-path SVGs sized 16px. Lucide-style strokes to match the rest
  // of the app's iconography.
  if (tone === "success") {
    return (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2"
           strokeLinecap="round" strokeLinejoin="round" className={className}>
        <path d="M20 6L9 17l-5-5" />
      </svg>
    );
  }
  if (tone === "warning") {
    return (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2"
           strokeLinecap="round" strokeLinejoin="round" className={className}>
        <path d="M12 9v4M12 17h.01M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z" />
      </svg>
    );
  }
  if (tone === "info") {
    return (
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2"
           strokeLinecap="round" strokeLinejoin="round" className={className}>
        <circle cx="12" cy="12" r="10" />
        <path d="M12 16v-4M12 8h.01" />
      </svg>
    );
  }
  // default error
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2"
         strokeLinecap="round" strokeLinejoin="round" className={className}>
      <circle cx="12" cy="12" r="10" />
      <path d="M15 9l-6 6M9 9l6 6" />
    </svg>
  );
}

export default function AlertModal({
  open,
  onClose,
  title,
  description,
  tone = "error",
  actionLabel = "Cerrar",
}) {
  // Escape-to-close. Matches Cmd+. / Esc on the modal patterns elsewhere.
  useEffect(() => {
    if (!open) return undefined;
    const onKey = (e) => {
      if (e.key === "Escape") onClose?.();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  const styles = TONE_STYLES[tone] || TONE_STYLES.error;

  return (
    <div
      className="fixed inset-0 z-[100] flex items-center justify-center bg-black/60 backdrop-blur-sm p-4"
      onClick={(e) => { if (e.target === e.currentTarget) onClose?.(); }}
      role="alertdialog"
      aria-modal="true"
      aria-labelledby="alert-modal-title"
    >
      <div className={
        "w-full max-w-md bg-surface-2 rounded-card ring-1 p-5 space-y-4 " +
        styles.ring
      }>
        <div className="flex items-start gap-3">
          <div className={
            "shrink-0 w-9 h-9 rounded-full ring-1 flex items-center justify-center " +
            styles.iconBg
          }>
            <Icon tone={tone} className={"w-4 h-4 " + styles.iconColor} />
          </div>
          <div className="flex-1 min-w-0">
            <h3
              id="alert-modal-title"
              className={"text-sm font-semibold leading-snug " + styles.titleColor}
            >
              {title}
            </h3>
            {description && (
              <p className="text-[13px] text-ink-secondary mt-1.5 leading-relaxed whitespace-pre-wrap break-words">
                {description}
              </p>
            )}
          </div>
        </div>
        <div className="flex justify-end">
          <button
            type="button"
            onClick={onClose}
            className="btn-primary text-xs h-9 px-4"
            autoFocus
          >
            {actionLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
