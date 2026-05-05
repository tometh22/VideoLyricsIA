import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

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
 *   options   : Array<{ code, label, css?, weight?, hint?, disabled?: boolean }>.
 *               `css`/`weight` apply inline `font-family`/`font-weight` to
 *               the option label so the typography picker shows each face
 *               in its own typography. `disabled: true` renders the option
 *               dimmed and skips it in keyboard navigation / click.
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
  // Popover position is computed from the trigger's getBoundingClientRect
  // and rendered into document.body via a portal — that's the only way to
  // escape parent containers with overflow-y-auto / overflow-hidden, which
  // is exactly what the file-rows wrapper does (max-h-96 overflow-y-auto)
  // and what was clipping the dropdown at the row's bottom edge.
  const [popoverPos, setPopoverPos] = useState({ top: 0, left: 0, width: 0 });
  const triggerRef = useRef(null);
  const popoverRef = useRef(null);

  const selected = options.find((o) => o.code === value) || options[0];

  // Helper: index of the next enabled option from `from` in `dir` (1 or -1).
  // Used by keyboard nav to skip disabled options.
  const _nextEnabled = (from, dir) => {
    const n = options.length;
    if (n === 0) return 0;
    let i = from;
    for (let step = 0; step < n; step++) {
      i = (i + dir + n) % n;
      if (!options[i]?.disabled) return i;
    }
    return from;
  };

  // Recompute popover screen position from the trigger's bounding rect.
  // Called on open and on scroll/resize so the popover follows the trigger
  // when the page or any parent scrolls.
  const updatePosition = () => {
    const t = triggerRef.current;
    if (!t) return;
    const r = t.getBoundingClientRect();
    setPopoverPos({
      top: r.bottom + 4,         // 4 px gap below the trigger
      left: r.left,
      width: r.width,
    });
  };

  // Click outside closes the popover. Also, opening the popover initialises
  // the highlight to the currently selected option (or the first enabled
  // one if the current is disabled) so keyboard users land on a sensible row.
  useEffect(() => {
    if (!open) return;
    let idx = options.findIndex((o) => o.code === value);
    if (idx < 0 || options[idx]?.disabled) {
      idx = options.findIndex((o) => !o.disabled);
      if (idx < 0) idx = 0;
    }
    setHighlight(idx);
    updatePosition();
    const onDoc = (e) => {
      if (
        popoverRef.current && !popoverRef.current.contains(e.target) &&
        triggerRef.current && !triggerRef.current.contains(e.target)
      ) {
        setOpen(false);
      }
    };
    const onScrollOrResize = () => updatePosition();
    document.addEventListener("mousedown", onDoc);
    // Use capture so we hear scrolls in any ancestor (e.g. the file-rows
    // overflow-y-auto wrapper) — `scroll` doesn't bubble.
    window.addEventListener("scroll", onScrollOrResize, true);
    window.addEventListener("resize", onScrollOrResize);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      window.removeEventListener("scroll", onScrollOrResize, true);
      window.removeEventListener("resize", onScrollOrResize);
    };
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
      setHighlight((h) => _nextEnabled(h, 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setHighlight((h) => _nextEnabled(h, -1));
    } else if (e.key === "Enter") {
      e.preventDefault();
      const opt = options[highlight];
      if (opt && !opt.disabled) {
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

      {open && createPortal(
        <div
          ref={popoverRef}
          role="listbox"
          aria-label={ariaLabel}
          style={{
            position: "fixed",
            top: popoverPos.top,
            left: popoverPos.left,
            width: popoverPos.width,
            zIndex: 1000,
          }}
          className="max-h-72 overflow-y-auto glass rounded-xl
                     border border-white/[0.08] shadow-xl py-1"
        >
          {options.map((opt, idx) => {
            const active = opt.code === value;
            const highlighted = idx === highlight;
            const optDisabled = !!opt.disabled;
            return (
              <button
                key={opt.code || `opt-${idx}`}
                type="button"
                role="option"
                aria-selected={active}
                aria-disabled={optDisabled}
                disabled={optDisabled}
                onMouseEnter={() => !optDisabled && setHighlight(idx)}
                onClick={() => {
                  if (optDisabled) return;
                  onChange(opt.code);
                  setOpen(false);
                }}
                className={`w-full flex items-center gap-2 px-3 py-2 text-[12px] text-left
                  transition-colors
                  ${highlighted && !optDisabled ? "bg-white/[0.06]" : ""}
                  ${optDisabled
                    ? "text-gray-600 cursor-not-allowed italic"
                    : active
                      ? "text-white"
                      : "text-gray-200 hover:text-white"
                  }
                `}
                style={{
                  fontFamily: opt.css || undefined,
                  fontWeight: opt.weight || undefined,
                }}
                title={opt.hint || undefined}
              >
                <span className="flex-1 truncate">{opt.label}</span>
                {active && !optDisabled && (
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
        </div>,
        document.body
      )}
    </div>
  );
}
