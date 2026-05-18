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
