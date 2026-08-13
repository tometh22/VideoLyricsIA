import { useEffect, useRef } from "react";

// UI freeze / render-storm detector (P0 UMG Chile 2026-06-16: "se queda
// pegado y empieza a titilar"). Cause-agnostic: instead of guessing WHICH
// loop hangs the main thread, we measure the symptom — the main thread
// saturating. A flicker/freeze is the rAF loop unable to keep ~60fps because
// React is re-rendering (or some loop is running) every frame.
//
// The 3 s debounced autosave can't record a 60 fps loop, so prod had zero
// trace. This emits ONE throttled "[ui-freeze]" console.warn (forwarded to
// Sentry by observability.js, 1/min/tag) carrying: effective fps, how long
// it was saturated, the last editor action (breadcrumb), and a caller-
// supplied context snapshot (segment count / overlaps / oscillating texts).
// The page URL already carries the job_id (/videos/.../edit-lyrics).
//
// Also wires a PerformanceObserver for `longtask` entries (>50 ms main-thread
// blocks) where supported (Chromium/Edge — the operators' browser), surfacing
// a burst as corroborating evidence.

// Module-level breadcrumb of the most recent editor mutation, set by the
// editor via recordEditorAction(); read into the freeze report so we know
// what the operator did right before the hang.
let _lastAction = { name: "", at: 0, info: null };
export function recordEditorAction(name, info = null) {
  _lastAction = { name, at: _nowMs(), info };
}

function _nowMs() {
  try {
    return typeof performance !== "undefined" && performance.now
      ? performance.now()
      : 0;
  } catch {
    return 0;
  }
}

/**
 * @param {object} opts
 * @param {boolean} opts.active     enable the detector (e.g. only on the editor screen)
 * @param {() => object} opts.getContext  returns a small JSON-able snapshot for the report
 */
export function useUiStormDetector({ active = true, getContext } = {}) {
  const getCtxRef = useRef(getContext);
  getCtxRef.current = getContext;

  useEffect(() => {
    if (!active) return undefined;
    if (typeof window === "undefined" || !window.requestAnimationFrame) return undefined;

    let raf = 0;
    let prev = _nowMs();
    let saturatedSince = 0;   // ms timestamp the main thread started lagging
    let slowFrames = 0;
    let lastReport = 0;

    const SLOW_FRAME_MS = 60;        // a frame >60ms ⇒ <~16fps ⇒ jank
    const SUSTAINED_MS = 1200;       // lagging this long ⇒ a real freeze, not a hiccup
    const REPORT_THROTTLE_MS = 30_000;

    const loop = () => {
      const now = _nowMs();
      const dt = now - prev;
      prev = now;

      if (dt > SLOW_FRAME_MS) {
        if (saturatedSince === 0) { saturatedSince = now; slowFrames = 0; }
        slowFrames += 1;
        const sustained = now - saturatedSince;
        if (sustained >= SUSTAINED_MS && now - lastReport > REPORT_THROTTLE_MS) {
          lastReport = now;
          const fps = slowFrames > 0 ? Math.round((slowFrames / sustained) * 1000) : 0;
          let ctx = null;
          try { ctx = getCtxRef.current ? getCtxRef.current() : null; } catch { /* best-effort */ }
          const sinceAction = _lastAction.at ? Math.round(now - _lastAction.at) : null;
          // eslint-disable-next-line no-console
          console.warn("[ui-freeze] main thread saturated", {
            sustainedMs: Math.round(sustained),
            approxFps: fps,
            lastAction: _lastAction.name || null,
            msSinceLastAction: sinceAction,
            lastActionInfo: _lastAction.info,
            context: ctx,
          });
        }
      } else {
        saturatedSince = 0;
        slowFrames = 0;
      }
      raf = window.requestAnimationFrame(loop);
    };
    raf = window.requestAnimationFrame(loop);

    // Long-task observer (Chromium/Edge). A burst of long tasks corroborates
    // a freeze; we keep a rolling count and let the rAF report read it.
    let observer = null;
    try {
      if (typeof PerformanceObserver !== "undefined") {
        let recentLong = 0;
        observer = new PerformanceObserver((list) => {
          const n = list.getEntries().filter((e) => e.duration > 50).length;
          if (n > 0) recentLong += n;
          // Surface a clear burst directly (independent of the rAF path).
          if (recentLong >= 8) {
            recentLong = 0;
            // eslint-disable-next-line no-console
            console.warn("[ui-longtask-burst] >=8 long tasks (>50ms) in a tick", {
              lastAction: _lastAction.name || null,
            });
          }
          // decay so isolated long tasks don't accumulate into a false burst
          setTimeout(() => { recentLong = Math.max(0, recentLong - n); }, 2000);
        });
        observer.observe({ entryTypes: ["longtask"] });
      }
    } catch { /* longtask unsupported — rAF detector still covers it */ }

    return () => {
      if (raf) window.cancelAnimationFrame(raf);
      if (observer) { try { observer.disconnect(); } catch { /* noop */ } }
    };
  }, [active]);
}
