// Global test setup for vitest + jsdom.
//
// Imports @testing-library/jest-dom for the `toBeInTheDocument` /
// `toHaveTextContent` style matchers. Without this, assertions on
// rendered DOM read as raw node comparisons and the failure messages
// are useless.
import "@testing-library/jest-dom/vitest";

// jsdom doesn't ship URL.createObjectURL — used by LyricsEditor for
// the audio blob preview. Tests never play audio so we stub to a noop
// returning a deterministic string the cleanup path can compare.
if (typeof URL.createObjectURL !== "function") {
  URL.createObjectURL = () => "blob:stub";
  URL.revokeObjectURL = () => {};
}

// jsdom doesn't implement HTMLMediaElement.play / pause; the audio
// element in the editor calls both. Stub to noop so the test isn't
// flooded with "not implemented" errors.
if (typeof window !== "undefined" && typeof HTMLMediaElement !== "undefined") {
  HTMLMediaElement.prototype.play = () => Promise.resolve();
  HTMLMediaElement.prototype.pause = () => {};
  HTMLMediaElement.prototype.load = () => {};
}

// Canvas is intentionally visual-only in unit tests (waveform + text
// measurement). jsdom logs a noisy "not implemented" error on every render;
// a deterministic 2D context keeps assertions focused on application code.
if (typeof window !== "undefined" && typeof HTMLCanvasElement !== "undefined") {
  HTMLCanvasElement.prototype.getContext = () => ({
    setTransform: () => {},
    clearRect: () => {},
    fillRect: () => {},
    measureText: (text) => ({ width: String(text || "").length * 8 }),
    font: "",
    fillStyle: "",
  });
}

// Responsive charts need a non-zero observed box. jsdom has no layout engine,
// so provide one stable viewport instead of letting Recharts warn with -1×-1.
if (typeof window !== "undefined" && typeof globalThis.ResizeObserver === "undefined") {
  globalThis.ResizeObserver = class ResizeObserver {
    constructor(callback) { this.callback = callback; }
    observe(target) {
      this.callback([{ target, contentRect: { width: 800, height: 400 } }]);
    }
    unobserve() {}
    disconnect() {}
  };
}

// scrollIntoView — jsdom doesn't implement it. The editor calls it
// from the sync-mode auto-scroll effect on every line change. Noop
// stub is enough for tests; no test should depend on layout.
if (typeof window !== "undefined" && typeof Element !== "undefined") {
  Element.prototype.scrollIntoView = Element.prototype.scrollIntoView || function () {};
}

// localStorage / sessionStorage — jsdom blocks both for opaque origins
// (about:blank), throwing SecurityError on first access. Several
// components touch them on mount (auth token, tenant detection, plan
// reads), so we replace with in-memory stubs. defineProperty with
// configurable:true overrides jsdom's getter that would otherwise
// throw on every access.
if (typeof window !== "undefined") {
  const _mk = () => {
    let store = {};
    return {
      getItem: (k) => (k in store ? store[k] : null),
      setItem: (k, v) => { store[k] = String(v); },
      removeItem: (k) => { delete store[k]; },
      clear: () => { store = {}; },
      key: (i) => Object.keys(store)[i] ?? null,
      get length() { return Object.keys(store).length; },
    };
  };
  Object.defineProperty(window, "localStorage", {
    value: _mk(), writable: true, configurable: true,
  });
  Object.defineProperty(window, "sessionStorage", {
    value: _mk(), writable: true, configurable: true,
  });
}

// matchMedia stub — react-joyride and a few responsive helpers check
// it on mount.
if (typeof window !== "undefined" && !window.matchMedia) {
  window.matchMedia = (query) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  });
}

// PR E (2026-07): el segmentsStore es un Map a NIVEL MÓDULO (sobrevive
// unmounts a propósito) — sin esta limpieza, la entrada seedeada por un
// test con transcribeJobId leakea al siguiente test que use el mismo id
// y el editor "hereda" segments de otro test. Limpiar SIEMPRE entre tests.
import { afterEach as _segStoreAfterEach } from "vitest";
import { segmentsStore as _segmentsStore } from "./state/segmentsStore";
_segStoreAfterEach(() => {
  _segmentsStore._clearAll();
});
