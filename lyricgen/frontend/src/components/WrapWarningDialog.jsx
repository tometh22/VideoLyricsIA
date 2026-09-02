import { useEffect, useRef } from "react";
import { createPortal } from "react-dom";

export default function WrapWarningDialog({ warning, onApproveAnyway, onAutoSplit, onReview }) {
  const dialogRef = useRef(null);
  const onReviewRef = useRef(onReview);

  useEffect(() => {
    onReviewRef.current = onReview;
  }, [onReview]);

  useEffect(() => {
    if (!warning) return undefined;
    const previous = document.activeElement;
    dialogRef.current?.focus();
    const onKeyDown = (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onReviewRef.current?.();
        return;
      }
      if (event.key !== "Tab") return;
      const controls = [...dialogRef.current.querySelectorAll("button:not([disabled])")];
      if (!controls.length) return;
      const first = controls[0];
      const last = controls[controls.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      previous?.focus?.();
    };
  }, [warning]);

  if (!warning || typeof document === "undefined") return null;
  const count = warning.ids?.length || 0;
  return createPortal(
    <div className="fixed inset-0 z-[100] grid place-items-center bg-black/75 p-4 backdrop-blur-sm" role="presentation">
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="wrap-warning-title"
        aria-describedby="wrap-warning-description"
        tabIndex={-1}
        className="w-full max-w-lg rounded-3xl bg-surface-1 p-6 shadow-2xl ring-1 ring-white/15 outline-none"
      >
        <div className="flex items-start gap-3">
          <div className="grid h-10 w-10 shrink-0 place-items-center rounded-2xl bg-amber-400/10 text-amber-200 ring-1 ring-amber-300/20" aria-hidden="true">
            <svg className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="1.8" viewBox="0 0 24 24">
              <path d="M12 8v5m0 3h.01" strokeLinecap="round" />
              <path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z" strokeLinejoin="round" />
            </svg>
          </div>
          <div>
            <p className="text-[10px] font-semibold uppercase tracking-[0.18em] text-amber-300">Antes de aprobar</p>
            <h2 id="wrap-warning-title" className="mt-1 text-xl font-semibold tracking-tight text-white">
              {count === 1 ? "Una línea puede ocupar 3 renglones" : `${count} líneas pueden ocupar 3 renglones`}
            </h2>
          </div>
        </div>
        <p id="wrap-warning-description" className="mt-4 text-sm leading-relaxed text-ink-secondary">
          Al agrandar la letra, algunas frases pueden dividirse en más renglones dentro del video. Tus correcciones están guardadas: podés aprobar igualmente o revisarlas antes.
        </p>
        <div className="mt-6 grid gap-2 sm:grid-cols-2">
          <button type="button" onClick={onReview} className="rounded-xl bg-white/[0.055] px-4 py-3 text-sm font-medium text-white ring-1 ring-white/10 transition-colors hover:bg-white/10">
            Seguir revisando
          </button>
          <button type="button" onClick={onAutoSplit} className="rounded-xl bg-white/[0.055] px-4 py-3 text-sm font-medium text-white ring-1 ring-white/10 transition-colors hover:bg-white/10">
            Dividir automáticamente
          </button>
        </div>
        <button type="button" onClick={onApproveAnyway} className="mt-3 w-full rounded-xl bg-gradient-to-r from-brand to-brand-light px-4 py-3 text-sm font-semibold text-white shadow-lg shadow-brand/20 transition-transform hover:-translate-y-0.5">
          Aprobar igualmente
        </button>
      </div>
    </div>,
    document.body,
  );
}
