/**
 * Regresión 83f95d0e2679: varios clicks en "Aprobar y generar" quedaban
 * esperando el flush de autosave. El primer callback arrancaba el edit y
 * navegaba al progreso; los callbacks tardíos volvían a POSTear y mostraban
 * "ya se está re-renderizando" encima del proceso exitoso.
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
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

afterEach(cleanup);

describe("Aprobar y generar — single flight", () => {
  it("coalesce clicks durante autosave y durante el POST en una sola aprobación", async () => {
    let resolveSave;
    let resolveApprove;
    const onPersistSegments = vi.fn(() => new Promise((resolve) => {
      resolveSave = resolve;
    }));
    const onApprove = vi.fn(() => new Promise((resolve) => {
      resolveApprove = resolve;
    }));

    render(
      <LyricsEditor
        segments={[{ start: 1, end: 2, text: "línea" }]}
        filename="song.mp3"
        audioFile={null}
        referenceLyrics=""
        onApprove={onApprove}
        onBack={vi.fn()}
        transcribeJobId="single-flight-job"
        onPersistSegments={onPersistSegments}
        submitLabel="Aprobar y generar"
      />,
    );

    const button = screen.getByRole("button", { name: /Aprobar y generar/i });
    fireEvent.click(button);
    fireEvent.click(button);
    fireEvent.click(button);

    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("aria-busy", "true");
    expect(onPersistSegments).toHaveBeenCalledTimes(1);

    await act(async () => {
      resolveSave({ ok: true, revision: 4 });
    });
    await waitFor(() => expect(onApprove).toHaveBeenCalledTimes(1));

    // El autosave ya terminó, pero el POST padre sigue pendiente. El CTA debe
    // continuar bloqueado durante toda la promesa, no sólo durante el flush.
    fireEvent.click(button);
    expect(onApprove).toHaveBeenCalledTimes(1);
    expect(button).toBeDisabled();

    await act(async () => {
      resolveApprove();
    });
    await waitFor(() => expect(button).not.toBeDisabled());
    expect(onApprove).toHaveBeenCalledTimes(1);
  });
});
