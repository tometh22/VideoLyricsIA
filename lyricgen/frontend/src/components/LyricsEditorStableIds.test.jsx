/**
 * Identidad estable de filas a través de reseeds (PR D del plan del editor).
 *
 * Antes: cualquier reseed del prop `segments` reasignaba `_id` POR ÍNDICE →
 * todas las keys nuevas → React re-montaba TODAS las filas (el amplificador
 * del reseed-storm/freeze en canciones largas, P0 UMG). Ahora el reseed
 * preserva el _id de las filas cuyo contenido no cambió, así un cambio
 * externo de UNA línea re-monta UNA fila.
 *
 * Lo observamos por identidad de nodos DOM: update → mismo nodo; remount →
 * nodo nuevo.
 */
import { render, screen, cleanup } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import LyricsEditor from "./LyricsEditor";

vi.mock("../i18n", () => ({
  useI18n: () => ({ t: (_key, fallback) => fallback }),
}));
vi.mock("./OnboardingTour", () => ({ EditorTour: () => null }));
vi.mock("./ToastProvider", () => ({
  useToast: () => ({ toast: () => {}, dismiss: () => {} }),
  ToastProvider: ({ children }) => children,
}));

function baseProps(overrides = {}) {
  return {
    segments: [
      { start: 1.0, end: 2.0, text: "alpha" },
      { start: 3.0, end: 4.0, text: "beta" },
      { start: 5.0, end: 6.0, text: "gamma" },
    ],
    filename: "song.mp3",
    audioFile: null,
    referenceLyrics: "",
    onApprove: vi.fn(),
    onBack: vi.fn(),
    transcribeJobId: "job-stable-ids",
    ...overrides,
  };
}

afterEach(() => cleanup());

describe("identidad estable de segmentos en reseed", () => {
  it("un cambio externo de UNA línea no re-monta las filas intactas", () => {
    const props = baseProps();
    const { rerender } = render(<LyricsEditor {...props} />);

    const alphaBefore = screen.getByDisplayValue("alpha");
    const gammaBefore = screen.getByDisplayValue("gamma");

    // Cambio externo genuino (nueva referencia, UNA línea distinta) —
    // pasa los guards de eco y dispara el reseed.
    rerender(
      <LyricsEditor
        {...props}
        segments={[
          { start: 1.0, end: 2.0, text: "alpha" },
          { start: 3.0, end: 4.0, text: "beta EDITADA" },
          { start: 5.0, end: 6.0, text: "gamma" },
        ]}
      />,
    );

    expect(screen.getByDisplayValue("beta EDITADA")).toBeTruthy();
    // Filas intactas: MISMO nodo DOM (update, no remount).
    expect(screen.getByDisplayValue("alpha")).toBe(alphaBefore);
    expect(screen.getByDisplayValue("gamma")).toBe(gammaBefore);
  });

  it("un eco reordenado con los mismos valores no re-monta ninguna fila", () => {
    const props = baseProps();
    const { rerender } = render(<LyricsEditor {...props} />);
    const alphaBefore = screen.getByDisplayValue("alpha");
    const betaBefore = screen.getByDisplayValue("beta");

    // Mismos valores, otra referencia y otro ORDEN (el writeback del
    // backend re-sortea por start): el guard #724 lo absorbe sin reseed.
    rerender(
      <LyricsEditor
        {...props}
        segments={[
          { start: 3.0, end: 4.0, text: "beta" },
          { start: 1.0, end: 2.0, text: "alpha" },
          { start: 5.0, end: 6.0, text: "gamma" },
        ]}
      />,
    );

    expect(screen.getByDisplayValue("alpha")).toBe(alphaBefore);
    expect(screen.getByDisplayValue("beta")).toBe(betaBefore);
  });
});
