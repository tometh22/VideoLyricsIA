/**
 * Identidad estable de filas a través de reseeds (PR D del plan del editor,
 * adaptado en PR E al segmentsStore).
 *
 * Antes: cualquier reseed del prop `segments` reasignaba `_id` POR ÍNDICE →
 * todas las keys nuevas → React re-montaba TODAS las filas (el amplificador
 * del reseed-storm/freeze en canciones largas, P0 UMG). Con PR E el prop es
 * solo el seed inicial: el reemplazo externo post-mount va por
 * segmentsStore.replace(jobId, segs), que preserva el _id de las filas cuyo
 * contenido no cambió (reseedPreservingIds) — un cambio externo de UNA
 * línea re-monta UNA fila; un eco puro no re-monta ninguna.
 *
 * Lo observamos por identidad de nodos DOM: update → mismo nodo; remount →
 * nodo nuevo.
 */
import { render, screen, cleanup, act } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import LyricsEditor from "./LyricsEditor";
import { segmentsStore } from "../state/segmentsStore";

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

afterEach(() => {
  cleanup();
  // El store es a nivel módulo — soltar la entrada del jobId del test.
  segmentsStore._clearAll();
});

describe("identidad estable de segmentos en reseed", () => {
  it("un cambio externo de UNA línea no re-monta las filas intactas", () => {
    const props = baseProps();
    render(<LyricsEditor {...props} />);

    const alphaBefore = screen.getByDisplayValue("alpha");
    const gammaBefore = screen.getByDisplayValue("gamma");

    // Cambio externo genuino (UNA línea distinta). PR E: ya no viaja por
    // el prop — va por el canal oficial segmentsStore.replace, que
    // preserva _id de las filas sin cambios.
    act(() => {
      segmentsStore.replace("job-stable-ids", [
        { start: 1.0, end: 2.0, text: "alpha" },
        { start: 3.0, end: 4.0, text: "beta EDITADA" },
        { start: 5.0, end: 6.0, text: "gamma" },
      ]);
    });

    expect(screen.getByDisplayValue("beta EDITADA")).toBeTruthy();
    // Filas intactas: MISMO nodo DOM (update, no remount).
    expect(screen.getByDisplayValue("alpha")).toBe(alphaBefore);
    expect(screen.getByDisplayValue("gamma")).toBe(gammaBefore);
  });

  it("un eco reordenado con los mismos valores no re-monta ninguna fila", () => {
    const props = baseProps();
    render(<LyricsEditor {...props} />);
    const alphaBefore = screen.getByDisplayValue("alpha");
    const betaBefore = screen.getByDisplayValue("beta");

    // Mismos valores en otro ORDEN (el writeback del backend re-sortea
    // por start). PR E: replace() preserva TODOS los _id de contenido
    // idéntico → ninguna fila re-monta.
    act(() => {
      segmentsStore.replace("job-stable-ids", [
        { start: 3.0, end: 4.0, text: "beta" },
        { start: 1.0, end: 2.0, text: "alpha" },
        { start: 5.0, end: 6.0, text: "gamma" },
      ]);
    });

    expect(screen.getByDisplayValue("alpha")).toBe(alphaBefore);
    expect(screen.getByDisplayValue("beta")).toBe(betaBefore);
  });
});
