// Smoke test for the vitest + jsdom + RTL setup. Lives alongside the
// setup file rather than under __tests__/ so a broken setup fails fast
// the next time anyone runs `npm test`. Asserts:
//
//   1. globals (describe/it/expect/vi) resolve without imports
//   2. jest-dom matchers are loaded (toBeInTheDocument)
//   3. jsdom provides a document
//   4. our HTMLMediaElement / URL stubs took effect
//
// If this file fails: don't add more tests; fix the setup first.
import { render, screen } from "@testing-library/react";

describe("frontend test infra", () => {
  it("renders a React component into jsdom", () => {
    render(<p>hola</p>);
    expect(screen.getByText("hola")).toBeInTheDocument();
  });

  it("provides URL.createObjectURL", () => {
    // jsdom v22+ ships a real implementation that returns blob:nodedata:.
    // Older versions need the stub in test-setup.js — either way the
    // call must be safe and return a string the editor can assign to
    // an <audio src>.
    expect(typeof URL.createObjectURL).toBe("function");
    const url = URL.createObjectURL(new Blob(["test"]));
    expect(typeof url).toBe("string");
    expect(url).toMatch(/^blob:/);
  });

  it("stubs HTMLMediaElement.play to resolve immediately", async () => {
    const audio = document.createElement("audio");
    await expect(audio.play()).resolves.toBeUndefined();
  });
});
