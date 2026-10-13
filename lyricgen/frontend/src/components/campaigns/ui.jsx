import { createPortal } from "react-dom";
import useDialogA11y from "../../hooks/useDialogA11y";
import { stageMeta, toneOf } from "../../lib/campaignPipeline";

// Small, consistent building blocks for the campaign workspace. One primary
// (filled violet) action per surface; everything else is secondary or ghost.

const BUTTON = {
  primary: "bg-brand text-white shadow-[0_8px_24px_rgba(117,87,255,.28)] hover:bg-[#6a4cf5] disabled:shadow-none",
  soft: "bg-brand/15 text-violet-100 ring-1 ring-brand/30 hover:bg-brand/25",
  secondary: "bg-white/[0.06] text-white ring-1 ring-white/10 hover:bg-white/[0.1]",
  ghost: "text-ink-secondary hover:bg-white/[0.06] hover:text-white",
  danger: "bg-red-500/15 text-red-100 ring-1 ring-red-400/25 hover:bg-red-500/25",
  success: "bg-emerald-500/15 text-emerald-100 ring-1 ring-emerald-400/25 hover:bg-emerald-500/25",
};
const SIZE = { sm: "h-8 px-3 text-xs", md: "h-10 px-4 text-sm", lg: "h-11 px-5 text-sm" };

export function Button({ variant = "secondary", size = "md", className = "", children, ...props }) {
  return <button type="button" {...props}
    className={`inline-flex shrink-0 items-center justify-center gap-2 whitespace-nowrap rounded-button font-semibold transition duration-brand ease-brand focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand disabled:cursor-not-allowed disabled:opacity-40 ${BUTTON[variant]} ${SIZE[size]} ${className}`}>
    {children}
  </button>;
}

export function StageBadge({ stage, label, className = "" }) {
  const tone = toneOf(stage);
  return <span className={`inline-flex items-center gap-1.5 whitespace-nowrap rounded-full px-2.5 py-1 text-[11px] font-medium ring-1 ${tone.chip} ${className}`}>
    <span aria-hidden="true" className={`h-1.5 w-1.5 rounded-full ${tone.dot}`} />
    {label || stageMeta(stage).title}
  </span>;
}

const CHIP = {
  neutral: "bg-white/[0.05] text-ink-secondary ring-white/10",
  info: "bg-sky-400/10 text-sky-200 ring-sky-300/20",
  warning: "bg-amber-400/10 text-amber-200 ring-amber-300/25",
  danger: "bg-red-400/10 text-red-200 ring-red-300/25",
  success: "bg-emerald-400/10 text-emerald-200 ring-emerald-300/25",
  brand: "bg-brand/15 text-violet-200 ring-brand/30",
};

export function Chip({ tone = "neutral", className = "", title, children }) {
  return <span title={title} className={`inline-flex items-center gap-1 whitespace-nowrap rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 ${CHIP[tone]} ${className}`}>{children}</span>;
}

export function Kbd({ children, className = "" }) {
  return <kbd aria-hidden="true" className={`${className || "inline-flex"} min-w-[1.4rem] items-center justify-center rounded-md border border-white/15 bg-white/[0.06] px-1.5 py-0.5 font-mono text-[10px] text-ink-secondary`}>{children}</kbd>;
}

export function Skeleton({ className = "" }) {
  return <div aria-hidden="true" className={`animate-pulse rounded-lg bg-white/[0.06] ${className}`} />;
}

export function EmptyState({ icon = "♪", title, description, action }) {
  return <div className="flex flex-col items-center justify-center gap-3 px-6 py-14 text-center">
    <div aria-hidden="true" className="grid h-12 w-12 place-items-center rounded-2xl bg-white/[0.05] text-xl text-ink-secondary ring-1 ring-white/10">{icon}</div>
    <p className="text-sm font-semibold text-white">{title}</p>
    {description && <p className="max-w-md text-sm text-ink-secondary">{description}</p>}
    {action}
  </div>;
}

/** Explanations live behind an (i) instead of paragraphs in the flow. */
export function InfoTip({ label = "Más información", children, align = "left" }) {
  return <details className="group relative inline-block align-middle">
    <summary aria-label={label} className="grid h-5 w-5 cursor-pointer list-none place-items-center rounded-full text-[11px] font-bold text-ink-secondary ring-1 ring-white/15 hover:text-white [&::-webkit-details-marker]:hidden">i</summary>
    <div role="note" className={`absolute top-7 z-30 w-72 rounded-xl bg-surface-3 p-3 text-xs leading-relaxed text-ink-secondary shadow-depth-lg ring-1 ring-white/10 ${align === "right" ? "right-0" : "left-0"}`}>{children}</div>
  </details>;
}

export function Banner({ tone = "info", children, action, role }) {
  const styles = {
    info: "bg-brand/10 text-violet-100 ring-brand/25",
    success: "bg-emerald-500/10 text-emerald-100 ring-emerald-400/25",
    warning: "bg-amber-500/10 text-amber-100 ring-amber-400/25",
    danger: "bg-red-500/10 text-red-100 ring-red-400/25",
  };
  return <div role={role || (tone === "danger" ? "alert" : "status")} className={`flex flex-wrap items-center justify-between gap-3 rounded-xl px-4 py-3 text-sm ring-1 ${styles[tone]}`}>
    <div className="min-w-0 flex-1">{children}</div>{action}
  </div>;
}

export function Modal({ label, busy = false, onClose, width = "max-w-lg", children }) {
  const dialogRef = useDialogA11y({ onClose, closeOnEscape: !busy });
  return createPortal(<div ref={dialogRef} tabIndex={-1} role="dialog" aria-modal="true" aria-label={label}
    className="fixed inset-0 z-[90] grid place-items-center overflow-y-auto bg-black/75 p-4 text-white backdrop-blur-sm"
    onMouseDown={(event) => { if (!busy && event.target === event.currentTarget) onClose?.(); }}>
    <div className={`max-h-[calc(100dvh-2rem)] w-full overflow-y-auto rounded-card bg-surface-2 p-5 shadow-depth-lg ring-1 ring-white/10 sm:p-6 ${width}`}>{children}</div>
  </div>, document.body);
}

export function Field({ label, hint, children, className = "" }) {
  return <label className={`block text-sm ${className}`}>
    <span className="mb-1.5 block text-xs font-medium text-ink-secondary">{label}</span>
    {children}
    {hint && <span className="mt-1 block text-xs text-ink-secondary/80">{hint}</span>}
  </label>;
}

export const inputClass = "w-full rounded-button bg-black/30 px-3 py-2.5 text-sm text-white ring-1 ring-white/10 outline-none placeholder:text-ink-secondary/60 focus:ring-2 focus:ring-brand/60 disabled:opacity-50";

export function ProgressBar({ value, max = 100, tone = "bg-brand", label }) {
  const percent = max > 0 ? Math.min(100, Math.max(0, (100 * value) / max)) : 0;
  return <div role="progressbar" aria-label={label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(percent)} className="h-1.5 overflow-hidden rounded-full bg-white/[0.07]">
    <div className={`h-full rounded-full transition-all duration-brand ${tone}`} style={{ width: `${percent}%` }} />
  </div>;
}
