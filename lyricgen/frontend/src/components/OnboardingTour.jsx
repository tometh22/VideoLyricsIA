import { useEffect, useState, useRef, useLayoutEffect, useCallback } from "react";
import { useI18n } from "../i18n";

const PAD = 8;
const TOOLTIP_W = 320;
const TOOLTIP_GAP = 14;

function getRect(selector) {
  if (!selector) return null;
  try {
    const el = document.querySelector(selector);
    if (!el) return null;
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return null;
    return r;
  } catch {
    return null;
  }
}

function computeTooltipPos(rect, placement) {
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  if (!rect) {
    return { left: vw / 2 - TOOLTIP_W / 2, top: vh / 2 - 100, centered: true };
  }
  let left, top;
  const place = placement || (rect.right + TOOLTIP_GAP + TOOLTIP_W < vw ? "right" : "bottom");
  if (place === "right") {
    left = rect.right + TOOLTIP_GAP;
    top = rect.top + rect.height / 2 - 80;
  } else if (place === "left") {
    left = rect.left - TOOLTIP_GAP - TOOLTIP_W;
    top = rect.top + rect.height / 2 - 80;
  } else if (place === "top") {
    left = rect.left + rect.width / 2 - TOOLTIP_W / 2;
    top = rect.top - TOOLTIP_GAP - 180;
  } else {
    left = rect.left + rect.width / 2 - TOOLTIP_W / 2;
    top = rect.bottom + TOOLTIP_GAP;
  }
  // Clamp
  left = Math.max(12, Math.min(left, vw - TOOLTIP_W - 12));
  top = Math.max(12, Math.min(top, vh - 220));
  return { left, top, centered: false };
}

export default function OnboardingTour({ steps, onFinish, onSkip }) {
  const { t } = useI18n();
  const [idx, setIdx] = useState(0);
  const [rect, setRect] = useState(null);
  const [tip, setTip] = useState({ left: 0, top: 0, centered: true });
  const rafRef = useRef(null);

  const step = steps[idx];

  const measure = useCallback(() => {
    const r = step?.target ? getRect(step.target) : null;
    setRect(r);
    setTip(computeTooltipPos(r, step?.placement));
  }, [step]);

  useLayoutEffect(() => {
    measure();
  }, [measure]);

  useEffect(() => {
    // Re-measure on resize, scroll, or DOM mutations (sidebar toggle, etc.)
    const onChange = () => {
      if (rafRef.current) cancelAnimationFrame(rafRef.current);
      rafRef.current = requestAnimationFrame(measure);
    };
    window.addEventListener("resize", onChange);
    window.addEventListener("scroll", onChange, true);
    const mo = new MutationObserver(onChange);
    mo.observe(document.body, { childList: true, subtree: true, attributes: true });
    // Retry if the target wasn't mounted yet
    const retry = setInterval(() => {
      if (!rect && step?.target) measure();
    }, 250);
    return () => {
      window.removeEventListener("resize", onChange);
      window.removeEventListener("scroll", onChange, true);
      mo.disconnect();
      clearInterval(retry);
      if (rafRef.current) cancelAnimationFrame(rafRef.current);
    };
  }, [measure, rect, step]);

  useEffect(() => {
    // Scroll target into view if needed
    if (step?.target) {
      try {
        const el = document.querySelector(step.target);
        if (el && el.scrollIntoView) {
          el.scrollIntoView({ behavior: "smooth", block: "center" });
        }
      } catch {}
    }
  }, [idx, step]);

  useEffect(() => {
    const onKey = (e) => {
      if (e.key === "Escape") onSkip?.();
      else if (e.key === "ArrowRight" || e.key === "Enter") next();
      else if (e.key === "ArrowLeft") prev();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [idx]);

  const next = () => {
    if (idx >= steps.length - 1) onFinish?.();
    else setIdx((i) => i + 1);
  };
  const prev = () => setIdx((i) => Math.max(0, i - 1));

  if (!step) return null;

  const hasTarget = !!rect;
  const spotlightStyle = hasTarget
    ? {
        left: rect.left - PAD,
        top: rect.top - PAD,
        width: rect.width + PAD * 2,
        height: rect.height + PAD * 2,
      }
    : null;

  return (
    <div className="fixed inset-0 z-[9999] pointer-events-none" aria-live="polite">
      {/* Backdrop with cutout — clicks blocked over backdrop, allowed inside spotlight */}
      <div className="absolute inset-0 pointer-events-auto" onClick={(e) => e.stopPropagation()}>
        {hasTarget ? (
          <svg className="absolute inset-0 w-full h-full" style={{ pointerEvents: "auto" }}>
            <defs>
              <mask id="tour-mask">
                <rect width="100%" height="100%" fill="white" />
                <rect
                  x={spotlightStyle.left}
                  y={spotlightStyle.top}
                  width={spotlightStyle.width}
                  height={spotlightStyle.height}
                  rx="14"
                  ry="14"
                  fill="black"
                />
              </mask>
            </defs>
            <rect
              width="100%"
              height="100%"
              fill="rgba(5, 5, 12, 0.72)"
              mask="url(#tour-mask)"
              style={{ backdropFilter: "blur(2px)" }}
            />
          </svg>
        ) : (
          <div className="absolute inset-0 bg-surface/80 backdrop-blur-sm" />
        )}

        {/* Spotlight ring */}
        {hasTarget && (
          <div
            className="absolute rounded-2xl ring-2 ring-brand/70 shadow-[0_0_0_4px_rgba(124,92,252,0.18)] transition-all duration-300 pointer-events-none"
            style={spotlightStyle}
          />
        )}

        {/* Tooltip */}
        <div
          className="absolute glass-elevated rounded-2xl border border-white/[0.08] p-5 shadow-2xl animate-fade-in"
          style={{
            left: tip.left,
            top: tip.top,
            width: TOOLTIP_W,
            pointerEvents: "auto",
          }}
        >
          <div className="flex items-start justify-between gap-3 mb-3">
            <div className="flex items-center gap-2">
              <span className="text-[10px] font-bold text-brand uppercase tracking-widest">
                {t("tour.step")} {idx + 1} / {steps.length}
              </span>
            </div>
            <button
              onClick={onSkip}
              className="text-gray-500 hover:text-white transition-colors -mt-1 -mr-1 p-1"
              aria-label={t("tour.skip")}
            >
              <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
                <path d="M18 6L6 18M6 6l12 12" />
              </svg>
            </button>
          </div>

          <h3 className="text-lg font-bold text-white mb-2">{step.title}</h3>
          <p className="text-sm text-gray-300 leading-relaxed mb-5">{step.body}</p>

          {/* Progress dots */}
          <div className="flex items-center gap-1.5 mb-4">
            {steps.map((_, i) => (
              <span
                key={i}
                className={`h-1 rounded-full transition-all ${
                  i === idx ? "w-6 bg-brand" : i < idx ? "w-1.5 bg-brand/40" : "w-1.5 bg-white/10"
                }`}
              />
            ))}
          </div>

          <div className="flex items-center justify-between gap-2">
            <button
              onClick={onSkip}
              className="text-xs text-gray-500 hover:text-gray-300 transition-colors"
            >
              {t("tour.skip")}
            </button>
            <div className="flex gap-2">
              {idx > 0 && (
                <button
                  onClick={prev}
                  className="btn-secondary text-xs px-3 py-1.5"
                >
                  {t("tour.prev")}
                </button>
              )}
              <button
                onClick={next}
                className="btn-primary text-xs px-4 py-1.5"
              >
                {idx === steps.length - 1 ? t("tour.done") : t("tour.next")}
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
