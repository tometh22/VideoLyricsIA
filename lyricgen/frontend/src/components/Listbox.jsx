import { useEffect, useRef, useState } from "react";

/**
 * Drop-in replacement for native <select> that matches the dark / glass
 * design system. Trigger button + popover, options rendered as <div>s so
 * inline styles (font-family for the typography picker, icons, etc.)
 * actually take effect across all browsers — native <option> elements
 * ignore most CSS.
 *
 * Props:
 *   value     : current selected option's `code` (string).
 *   onChange  : (newCode: string) => void.
 *   options   : Array<{ code, label, css?, weight?, hint? }>.
 *               `css`/`weight` apply inline `font-family`/`font-weight` to
 *               the option label so the typography picker shows each face
 *               in its own typography.
 *   className : extra classes for the trigger wrapper.
 *   ariaLabel : accessible label for screen readers.
 *   disabled  : optional, dims the trigger.
 *
 * Keyboard:
 *   - Trigger focus + Enter/Space: open popover, focus current option.
 *   - Arrow Up/Down: move highlight.
 *   - Enter: select highlighted, close.
 *   - Escape: close without selecting.
 *
 * Closes on click-outside.
 */
export default function Listbox({
  value,
  onChange,
  options,
  className = "",
  ariaLabel,
  disabled = false,
}) {
  const [open, setOpen] = useState(false);
  const [highlight, setHighlight] = useState(0);
  const triggerRef = useRef(null);
  const popoverRef = useRef(null);

  const selected = options.find((o) => o.code === value) || options[0];

  // Click outside closes the popover. Also, opening the popover initialises
  // the highlight to the currently selected option so keyboard users land
  // on a sensible row.
  useEffect(() => {
    if (!open) return;
    const idx = options.findIndex((o) => o.code === value);
    setHighlight(idx >= 0 ? idx : 0);
    const onDoc = (e) => {
      if (
        popoverRef.current && !popoverRef.current.contains(e.target) &&
        triggerRef.current && !triggerRef.current.contains(e.target)
      ) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open, options, value]);

  const onKeyDown = (e) => {
    if (disabled) return;
    if (!open) {
      if (e.key === "Enter" || e.key === " " || e.key === "ArrowDown") {
        e.preventDefault();
        setOpen(true);
      }
      return;
    }
    if (e.key === "Escape") {
      e.preventDefault();
      setOpen(false);
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      setHighlight((h) => (h + 1) % options.length);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setHighlight((h) => (h - 1 + options.length) % options.length);
    } else if (e.key === "Enter") {
      e.preventDefault();
      const opt = options[highlight];
      if (opt) {
        onChange(opt.code);
        setOpen(false);
      }
    }
  };

  return (
    <div className={`relative ${className}`}>
      <button
        ref={triggerRef}
        type="button"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={ariaLabel}
        disabled={disabled}
        onClick={() => !disabled && setOpen((o) => !o)}
        onKeyDown={onKeyDown}
        className={`w-full flex items-center justify-between gap-2 px-3 py-1.5
          rounded-md bg-surface-1 border text-[12px] text-white text-left
          transition-colors
          ${open ? "border-brand/50" : "border-white/[0.06] hover:border-white/[0.12]"}
          ${disabled ? "opacity-50 cursor-not-allowed" : "cursor-pointer"}
        `}
        style={{
          fontFamily: selected?.css || undefined,
          fontWeight: selected?.weight || undefined,
        }}
      >
        <span className="truncate">{selected?.label ?? ""}</span>
        <svg
          className={`w-3.5 h-3.5 text-gray-400 shrink-0 transition-transform ${open ? "rotate-180" : ""}`}
          fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24"
        >
          <polyline points="6 9 12 15 18 9" />
        </svg>
      </button>

      {open && (
        <div
          ref={popoverRef}
          role="listbox"
          aria-label={ariaLabel}
          className="absolute z-50 mt-1 left-0 right-0 max-h-72 overflow-y-auto
                     glass rounded-xl border border-white/[0.08] shadow-xl py-1"
        >
          {options.map((opt, idx) => {
            const active = opt.code === value;
            const highlighted = idx === highlight;
            return (
              <button
                key={opt.code || `opt-${idx}`}
                type="button"
                role="option"
                aria-selected={active}
                onMouseEnter={() => setHighlight(idx)}
                onClick={() => {
                  onChange(opt.code);
                  setOpen(false);
                }}
                className={`w-full flex items-center gap-2 px-3 py-2 text-[12px] text-left
                  transition-colors
                  ${highlighted ? "bg-white/[0.06]" : ""}
                  ${active ? "text-white" : "text-gray-200 hover:text-white"}
                `}
                style={{
                  fontFamily: opt.css || undefined,
                  fontWeight: opt.weight || undefined,
                }}
              >
                <span className="flex-1 truncate">{opt.label}</span>
                {active && (
                  <svg
                    className="w-3.5 h-3.5 text-brand shrink-0"
                    fill="none" stroke="currentColor" strokeWidth="2.5" viewBox="0 0 24 24"
                  >
                    <polyline points="20 6 9 17 4 12" />
                  </svg>
                )}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
