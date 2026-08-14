/**
 * Regresión del "Aprobar y generar" que no anda (reporte UMG 2026-07-24:
 * "tuve que clickearlo muchas veces").
 *
 * El botón nunca está deshabilitado. handleApprove hace un flush del
 * autosave (backup del servidor); si ese POST devuelve 409 stale-revision
 * (la revisión del backup quedó adelante de la nuestra), el código ANTES
 * hacía `return` en silencio → el operador clickeaba una y otra vez sin
 * feedback hasta que el polling re-sincronizaba la revisión.
 *
 * Contrato actual: la revisión del servidor sigue protegida por CAS, pero el
 * editor reancla y reintenta automáticamente los 409 esperables. No se
 * muestra un popup de colaboración ni se bloquea la aprobación por un race
 * de autosave.
 */
import { render, screen, cleanup, fireEvent, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import LyricsEditor from "./LyricsEditor";

vi.mock("../i18n", () => ({
  useI18n: () => ({ t: (_key, fallback) => fallback }),
}));
vi.mock("./OnboardingTour", () => ({ EditorTour: () => null }));

const toastSpy = vi.fn();
vi.mock("./ToastProvider", () => ({
  useToast: () => ({ toast: toastSpy, dismiss: () => {} }),
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
    transcribeJobId: "job-conflict-test",
    submitLabel: "Aprobar y generar",
    ...overrides,
  };
}

afterEach(() => {
  cleanup();
  toastSpy.mockClear();
});

describe("Aprobar y generar — conflicto seguro", () => {
  it("un 409 se reintenta con la revisión actual y luego aprueba", async () => {
    const onPersistSegments = vi
      .fn()
      .mockResolvedValueOnce({
        ok: false,
        reason: "stale-revision",
        currentRevision: 1,
      })
      .mockResolvedValue({ ok: true, revision: 2 });
    const onApprove = vi.fn();
    render(<LyricsEditor {...baseProps({ onPersistSegments, onApprove })} />);

    fireEvent.click(screen.getByRole("button", { name: /Aprobar y generar/i }));

    await waitFor(() => expect(onApprove).toHaveBeenCalledTimes(1));
    expect(onPersistSegments).toHaveBeenCalledTimes(2);
    expect(onPersistSegments.mock.calls[1][2]).toMatchObject({ baseRevision: 1 });
    expect(screen.queryByText("Conflicto: cambios no guardados")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Aprobar y generar/i })).not.toBeDisabled();
  });

  it("si el 409 persiste, avisa con toast informativo sin mostrar conflicto", async () => {
    const onPersistSegments = vi
      .fn()
      .mockResolvedValue({ ok: false, reason: "stale-revision", currentRevision: 1 });
    const onApprove = vi.fn();
    render(<LyricsEditor {...baseProps({ onPersistSegments, onApprove })} />);

    fireEvent.click(screen.getByRole("button", { name: /Aprobar y generar/i }));

    await waitFor(() =>
      expect(toastSpy).toHaveBeenCalledWith(
        expect.objectContaining({ tone: "info" }),
      ),
    );
    expect(onApprove).not.toHaveBeenCalled();
    expect(screen.queryByText("Conflicto: cambios no guardados")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Aprobar y generar/i })).not.toBeDisabled();
  });
});
