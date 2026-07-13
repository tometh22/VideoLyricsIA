import { useEffect, useRef } from "react";

const FOCUSABLE = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled]):not([type='hidden'])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "[tabindex]:not([tabindex='-1'])",
].join(",");

/** Shared keyboard/focus behavior for modal dialogs. */
export default function useDialogA11y({
  open = true,
  onClose,
  closeOnEscape = true,
  initialFocusRef,
} = {}) {
  const dialogRef = useRef(null);
  const optionsRef = useRef({ onClose, closeOnEscape, initialFocusRef });
  optionsRef.current = { onClose, closeOnEscape, initialFocusRef };

  useEffect(() => {
    if (!open) return undefined;
    const opener = document.activeElement;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";

    const focusTimer = window.setTimeout(() => {
      const dialog = dialogRef.current;
      const preferred = optionsRef.current.initialFocusRef?.current;
      const target = preferred || dialog?.querySelector(FOCUSABLE) || dialog;
      target?.focus?.();
    }, 0);

    const onKeyDown = (event) => {
      const dialog = dialogRef.current;
      if (!dialog) return;
      if (event.key === "Escape" && optionsRef.current.closeOnEscape) {
        event.preventDefault();
        optionsRef.current.onClose?.();
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = [...dialog.querySelectorAll(FOCUSABLE)].filter(
        (node) => node.getAttribute("aria-hidden") !== "true" && !node.closest("[hidden]"),
      );
      if (focusable.length === 0) {
        event.preventDefault();
        dialog.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };

    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.clearTimeout(focusTimer);
      window.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previousOverflow;
      if (opener instanceof HTMLElement && document.contains(opener)) opener.focus();
    };
  }, [open]);

  return dialogRef;
}
