/**
 * Versión B, parte 2 — botón "Re-sincronizar con IA" del LyricsEditor.
 *
 * Contrato:
 *  - Gate: solo aparece con onReanchor + transcribeJobId + user.features
 *    .anchor_lyrics === true (flag ANCHOR_LYRICS_ENABLED del backend).
 *  - Click: flushea el estado local a /save-segments ANTES del reanchor
 *    (el backend re-ancla segments_json — sin el flush re-anclaría texto
 *    viejo), llama onReanchor(jobId) y reemplaza `edited` con los
 *    segments re-anclados que devuelve el endpoint.
 *  - Decline / error → toast de error y los timings quedan como estaban.
 */
import { render, screen, cleanup, fireEvent, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import LyricsEditor from "./LyricsEditor";

vi.mock("../i18n", () => ({
  useI18n: () => ({ t: () => "" }),
}));
vi.mock("./OnboardingTour", () => ({
  EditorTour: () => null,
}));

const toastSpy = vi.fn();
vi.mock("./ToastProvider", () => ({
  useToast: () => ({ toast: toastSpy, dismiss: () => {} }),
  ToastProvider: ({ children }) => children,
}));

const SEGMENTS = [
  { start: 0.0, end: 2.0, text: "linea uno" },
  { start: 2.0, end: 4.0, text: "linea dos" },
  { start: 4.0, end: 6.0, text: "linea tres" },
];

function baseProps(overrides = {}) {
  return {
    segments: SEGMENTS,
    filename: "song.mp3",
    audioFile: null,
    referenceLyrics: "",
    onApprove: vi.fn(),
    onBack: vi.fn(),
    transcribeJobId: "job-reanchor",
    user: { features: { anchor_lyrics: true } },
    disableAutosave: true,
    ...overrides,
  };
}

afterEach(() => {
  cleanup();
  toastSpy.mockReset();
});

describe("Re-sincronizar con IA", () => {
  it("NO se muestra sin features.anchor_lyrics", () => {
    render(
      <LyricsEditor {...baseProps({
        user: { features: {} },
        onReanchor: vi.fn(),
      })} />,
    );
    expect(screen.queryByTestId("reanchor-btn")).toBeNull();
  });

  it("NO se muestra sin onReanchor (padre sin soporte)", () => {
    render(<LyricsEditor {...baseProps()} />);
    expect(screen.queryByTestId("reanchor-btn")).toBeNull();
  });

  it("click → flush a save-segments, luego reanchor, y aplica los segments nuevos", async () => {
    const calls = [];
    const onPersistSegments = vi.fn(async (jobId, cleaned) => {
      calls.push(["persist", jobId, cleaned.map((s) => s.text)]);
      return { ok: true };
    });
    const onReanchor = vi.fn(async (jobId) => {
      calls.push(["reanchor", jobId]);
      return {
        ok: true,
        count: 3,
        review_count: 1,
        segments: [
          { start: 0.4, end: 2.1, text: "linea uno" },
          { start: 2.6, end: 4.2, text: "linea dos", review: true },
          { start: 4.8, end: 6.3, text: "linea tres" },
        ],
      };
    });
    render(
      <LyricsEditor {...baseProps({ onPersistSegments, onReanchor })} />,
    );

    fireEvent.click(screen.getByTestId("editor-overflow-btn"));

    fireEvent.click(screen.getByTestId("reanchor-btn"));

    await waitFor(() => expect(onReanchor).toHaveBeenCalledWith("job-reanchor"));
    // El flush al backend corre ANTES del reanchor, con el texto actual.
    expect(calls[0][0]).toBe("persist");
    expect(calls[0][1]).toBe("job-reanchor");
    expect(calls[0][2]).toEqual(["linea uno", "linea dos", "linea tres"]);
    expect(calls[1][0]).toBe("reanchor");
    // Toast de éxito con los contadores del endpoint.
    await waitFor(() => expect(toastSpy).toHaveBeenCalled());
    expect(toastSpy.mock.calls[0][0].tone).toBe("success");
    expect(toastSpy.mock.calls[0][0].message).toContain("3");
    expect(toastSpy.mock.calls[0][0].message).toContain("1");
  });

  it("decline del backend (ok:false) → toast de error, sin romper el editor", async () => {
    const onReanchor = vi.fn(async () => ({ ok: false, reason: "declined" }));
    render(
      <LyricsEditor {...baseProps({
        onPersistSegments: vi.fn(async () => ({ ok: true })),
        onReanchor,
      })} />,
    );

    fireEvent.click(screen.getByTestId("editor-overflow-btn"));

    fireEvent.click(screen.getByTestId("reanchor-btn"));

    await waitFor(() => expect(toastSpy).toHaveBeenCalled());
    expect(toastSpy.mock.calls[0][0].tone).toBe("error");
    // Los segments del editor siguen siendo los originales.
    expect(screen.getByDisplayValue("linea uno")).toBeTruthy();
  });

  it("excepción del callback → toast de error (nunca crashea)", async () => {
    const onReanchor = vi.fn(async () => { throw new Error("network"); });
    render(
      <LyricsEditor {...baseProps({
        onPersistSegments: vi.fn(async () => ({ ok: true })),
        onReanchor,
      })} />,
    );
    fireEvent.click(screen.getByTestId("editor-overflow-btn"));
    fireEvent.click(screen.getByTestId("reanchor-btn"));
    await waitFor(() => expect(toastSpy).toHaveBeenCalled());
    expect(toastSpy.mock.calls[0][0].tone).toBe("error");
  });
});
