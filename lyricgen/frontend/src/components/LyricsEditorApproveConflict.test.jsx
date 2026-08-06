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
 * Contrato Editor 2.0: un conflicto nunca se auto-resuelve ni pisa la
 * revisión del equipo. La aprobación queda bloqueada y el borrador local
 * permanece disponible hasta una decisión explícita.
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
  it("un 409 bloquea aprobación y nunca intenta overwrite automático", async () => {
    const onPersistSegments = vi.fn().mockResolvedValue({ ok: false, reason: "stale-revision" });
    const onApprove = vi.fn();
    render(<LyricsEditor {...baseProps({ onPersistSegments, onApprove })} />);

    fireEvent.click(screen.getByRole("button", { name: /Aprobar y generar/i }));

    await waitFor(() => expect(screen.getByText("Conflicto: cambios no guardados")).toBeInTheDocument());
    expect(onApprove).not.toHaveBeenCalled();
    expect(onPersistSegments).toHaveBeenCalledTimes(1);
    expect(onPersistSegments.mock.calls[0][2]).not.toMatchObject({ resolveConflict: true });
    expect(screen.getByRole("button", { name: /Aprobar y generar/i })).toBeDisabled();
  });

  it("si el conflicto persiste, avisa con toast y NO aprueba en silencio", async () => {
    const onPersistSegments = vi
      .fn()
      .mockResolvedValue({ ok: false, reason: "stale-revision" });
    const onApprove = vi.fn();
    render(<LyricsEditor {...baseProps({ onPersistSegments, onApprove })} />);

    fireEvent.click(screen.getByRole("button", { name: /Aprobar y generar/i }));

    await waitFor(() =>
      expect(toastSpy).toHaveBeenCalledWith(
        expect.objectContaining({ tone: "error" }),
      ),
    );
    expect(onApprove).not.toHaveBeenCalled();
  });
});
