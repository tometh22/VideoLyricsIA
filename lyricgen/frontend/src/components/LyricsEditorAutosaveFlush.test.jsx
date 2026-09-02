/**
 * Regresión del flush-on-unmount del autosave (auditoría 2026-06-10,
 * "hay partes que no se graban").
 *
 * Pre-fix: el cleanup del debounce de 3s CANCELABA el guardado pendiente
 * al desmontar — salir del step 6 del wizard (el workaround de la
 * operadora contra el "se mueve") tiraba los últimos <3s de anclas/drags,
 * y el remount sembraba desde datos viejos. Post-fix: el unmount dispara
 * un persist fire-and-forget con el estado vigente.
 */
import { render, screen, cleanup, fireEvent } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import LyricsEditor from "./LyricsEditor";

vi.mock("../i18n", () => ({
  useI18n: () => ({ t: (_key, fallback) => fallback }),
}));
vi.mock("./OnboardingTour", () => ({
  EditorTour: () => null,
}));
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
    transcribeJobId: "job-flush-test",
    ...overrides,
  };
}

afterEach(() => cleanup());

describe("autosave flush-on-unmount", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("editar y desmontar ANTES del debounce de 3s persiste igual", () => {
    const onPersistSegments = vi.fn().mockResolvedValue({ ok: true });
    const { unmount } = render(
      <LyricsEditor {...baseProps({ onPersistSegments })} />,
    );

    const input = screen.getByDisplayValue("alpha line");
    fireEvent.change(input, { target: { value: "alpha line editada" } });

    // El workaround de la operadora: salir del paso a los ~1s del último
    // toque — mucho antes de que el debounce dispare.
    vi.advanceTimersByTime(1000);
    unmount();

    expect(onPersistSegments).toHaveBeenCalled();
    const calls = onPersistSegments.mock.calls;
    const [jobId, cleaned] = calls[calls.length - 1];
    expect(jobId).toBe("job-flush-test");
    expect(cleaned.some((s) => s.text === "alpha line editada")).toBe(true);
  });

  it("si el debounce YA disparó, el unmount no duplica el persist", async () => {
    const onPersistSegments = vi.fn().mockResolvedValue({ ok: true });
    const { unmount } = render(
      <LyricsEditor {...baseProps({ onPersistSegments })} />,
    );

    const input = screen.getByDisplayValue("alpha line");
    fireEvent.change(input, { target: { value: "alpha line editada" } });

    await vi.advanceTimersByTimeAsync(3500); // debounce dispara y resuelve
    const callsAfterDebounce = onPersistSegments.mock.calls.length;
    expect(callsAfterDebounce).toBeGreaterThan(0);

    unmount();
    expect(onPersistSegments.mock.calls.length).toBe(callsAfterDebounce);
  });

  it("sin jobId el flush no dispara (mismo guard que el autosave)", () => {
    const onPersistSegments = vi.fn().mockResolvedValue({ ok: true });
    const { unmount } = render(
      <LyricsEditor {...baseProps({ onPersistSegments, transcribeJobId: null })} />,
    );
    const input = screen.getByDisplayValue("alpha line");
    fireEvent.change(input, { target: { value: "x" } });
    vi.advanceTimersByTime(1000);
    unmount();
    expect(onPersistSegments).not.toHaveBeenCalled();
  });

  it("un ancla de Modo Sync marca dirty y se persiste al salir", () => {
    const onPersistSegments = vi.fn().mockResolvedValue({ ok: true });
    const { container, unmount } = render(
      <LyricsEditor {...baseProps({
        onPersistSegments,
        audioFile: new Blob(["audio"], { type: "audio/mpeg" }),
      })} />,
    );
    const audio = container.querySelector("audio");
    Object.defineProperty(audio, "currentTime", { configurable: true, writable: true, value: 1.6 });
    fireEvent.timeUpdate(audio);

    fireEvent.click(container.querySelector('[data-testid^="sync-dot-"]'));
    fireEvent.keyDown(window, { code: "Space" });
    vi.advanceTimersByTime(500);
    unmount();

    expect(onPersistSegments).toHaveBeenCalledTimes(1);
    const saved = onPersistSegments.mock.calls[0][1];
    expect(saved[0].start).not.toBeCloseTo(1, 2);
  });
});
