/**
 * Botón "Guardar" manual (pedido operadores UMG — Seba + Gaby, jul-2026).
 * El autoguardado debounced a veces falla/se pierde al navegar; el botón
 * da un guardado on-demand con feedback claro (Guardar → Guardando… →
 * Guardado / Reintentar). Reusa el mismo camino que el flush-on-drag.
 */
import { render, screen, cleanup, fireEvent, waitFor } from "@testing-library/react";
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
    segments: [{ start: 1.0, end: 2.0, text: "alpha line" }],
    filename: "song.mp3",
    audioFile: null,
    referenceLyrics: "",
    onApprove: vi.fn(),
    onBack: vi.fn(),
    transcribeJobId: "job-manual-save",
    ...overrides,
  };
}

afterEach(() => cleanup());

describe("botón Guardar manual", () => {
  it("guarda on-demand el estado vigente al hacer click y muestra Guardado", async () => {
    const onPersistSegments = vi.fn().mockResolvedValue({ ok: true });
    render(<LyricsEditor {...baseProps({ onPersistSegments })} />);

    const input = screen.getByDisplayValue("alpha line");
    fireEvent.change(input, { target: { value: "alpha editada" } });

    fireEvent.click(screen.getByRole("button", { name: "Guardar" }));

    expect(onPersistSegments).toHaveBeenCalled();
    const [jobId, cleaned] = onPersistSegments.mock.calls.at(-1);
    expect(jobId).toBe("job-manual-save");
    expect(cleaned.some((s) => s.text === "alpha editada")).toBe(true);

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Guardado" })).toBeTruthy(),
    );
  });

  it("no se renderiza sin onPersistSegments cableado", () => {
    render(<LyricsEditor {...baseProps({ onPersistSegments: undefined })} />);
    expect(screen.queryByRole("button", { name: "Guardar" })).toBeNull();
  });

  it("marca 'Reintentar' cuando el guardado falla", async () => {
    const onPersistSegments = vi.fn().mockResolvedValue({ ok: false });
    render(<LyricsEditor {...baseProps({ onPersistSegments })} />);

    fireEvent.click(screen.getByRole("button", { name: "Guardar" }));

    // En error hay dos afordancias de reintento (el banner rojo + el botón
    // de la barra) — con que aparezca al menos una alcanza.
    await waitFor(() =>
      expect(
        screen.getAllByRole("button", { name: "Reintentar" }).length,
      ).toBeGreaterThan(0),
    );
  });
});
