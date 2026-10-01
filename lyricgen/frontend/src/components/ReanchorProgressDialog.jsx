import { useLayoutEffect, useRef } from "react";
import { createPortal } from "react-dom";
import { useI18n } from "../i18n";

export default function ReanchorProgressDialog({ waiting = false, returnFocusRef }) {
  const { t } = useI18n();
  const overlayRef = useRef(null);
  const dialogRef = useRef(null);

  useLayoutEffect(() => {
    const previousFocus = document.activeElement;
    const previousOverflow = document.body.style.overflow;
    // The paste dialog and app navigation live outside the editor subtree.
    // Make every background surface inert, preserving any existing locks.
    const background = [...document.body.children]
      .filter((element) => element !== overlayRef.current)
      .map((element) => [element, element.getAttribute("inert")]);
    background.forEach(([element]) => element.setAttribute("inert", ""));
    document.body.style.overflow = "hidden";
    dialogRef.current?.focus();
    const keepFocus = (event) => {
      if (!overlayRef.current?.contains(event.target)) dialogRef.current?.focus();
    };
    const blockEditorKeys = (event) => {
      // Window shortcuts (play, undo, timing edits) also need to be blocked.
      event.stopImmediatePropagation();
      if (!event.metaKey && !event.ctrlKey) event.preventDefault();
    };
    window.addEventListener("keydown", blockEditorKeys, true);
    document.addEventListener("focusin", keepFocus, true);
    return () => {
      window.removeEventListener("keydown", blockEditorKeys, true);
      document.removeEventListener("focusin", keepFocus, true);
      background.forEach(([element, inert]) => {
        if (inert === null) element.removeAttribute("inert");
        else element.setAttribute("inert", inert);
      });
      document.body.style.overflow = previousOverflow;
      const target = previousFocus?.isConnected && previousFocus !== document.body
        ? previousFocus : returnFocusRef?.current;
      if (!target?.closest("[inert]")) target?.focus();
    };
  }, [returnFocusRef]);

  return createPortal(
    <div ref={overlayRef} data-testid="reanchor-progress-overlay"
      className="fixed inset-0 z-[200] flex items-center justify-center bg-black/80 p-5 backdrop-blur-sm">
      <section ref={dialogRef} role="dialog" aria-modal="true" tabIndex={-1}
        aria-labelledby="reanchor-progress-title" aria-describedby="reanchor-progress-description"
        className="w-full max-w-md rounded-3xl bg-surface-1 p-8 text-center shadow-2xl ring-1 ring-white/15 outline-none">
        <div aria-hidden="true" className="mx-auto h-10 w-10 animate-spin rounded-full border-4 border-white/15 border-t-brand motion-reduce:animate-none" />
        <h2 id="reanchor-progress-title" className="mt-5 text-lg font-semibold text-white">
          {t("editor.reanchor_running") || "Re-sincronizando…"}
        </h2>
        <p id="reanchor-progress-description" role="status" aria-live="polite" aria-atomic="true"
          className="mt-3 text-sm leading-6 text-ink-secondary">
          {waiting
            ? (t("editor.reanchor_progress_waiting") || "La respuesta está demorando. Estamos comprobando si la re-sincronización terminó; esperá un momento más.")
            : (t("editor.reanchor_progress_hint") || "Estamos ajustando la letra al audio. Puede tardar unos minutos. Esperá a que termine para seguir editando.")}
        </p>
      </section>
    </div>, document.body,
  );
}
