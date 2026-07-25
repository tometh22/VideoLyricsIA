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
 * Contrato post-fix: los segments EN PANTALLA son la fuente de verdad al
 * aprobar (onApprove manda `cleaned`, no el backup). Un conflicto del
 * backup se auto-resuelve una vez (re-lee la revisión fresca, resolveConflict)
 * y approve procede en UN click. Si aún así falla, se avisa con un toast y
 * NO se aprueba en silencio.
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

describe("Aprobar y generar — auto-resuelve stale-revision", () => {
  it("un 409 stale-revision se auto-resuelve y approve procede en un click", async () => {
    // 1ª llamada (autosave normal) → 409; 2ª (resolveConflict) → ok.
    const onPersistSegments = vi.fn().mockImplementation((_id, _segs, opts) =>
      opts?.resolveConflict
        ? Promise.resolve({ ok: true, revision: 7 })
        : Promise.resolve({ ok: false, reason: "stale-revision" }),
    );
    const onApprove = vi.fn();
    render(<LyricsEditor {...baseProps({ onPersistSegments, onApprove })} />);

    fireEvent.click(screen.getByRole("button", { name: /Aprobar y generar/i }));

    await waitFor(() => expect(onApprove).toHaveBeenCalledTimes(1));
    // El retry usó el path de conflicto (re-lectura fresca de revisión).
    expect(onPersistSegments).toHaveBeenCalledTimes(2);
    expect(onPersistSegments.mock.calls[1][2]).toMatchObject({ resolveConflict: true });
    // approve viaja con la revisión resuelta, no la stale.
    expect(onApprove.mock.calls[0][1]).toMatchObject({ baseRevision: 7 });
    expect(toastSpy).not.toHaveBeenCalled();
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
