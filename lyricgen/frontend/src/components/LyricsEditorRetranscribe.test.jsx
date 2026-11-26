/**
 * Staging job 7d45ea03941b "Gran Ídolo De Asia": WhisperX auto-detected
 * English and translated half a Spanish song. The editor told the reviewer to
 * "go back and reprocess", a path campaign jobs do not have. These cover the
 * in-editor re-transcription: language selector, confirmation, polling until
 * the new version is installed, reload, failure, and the menu entry that works
 * when no language alert is shown.
 */
import { render, screen, cleanup, fireEvent, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import LyricsEditor from "./LyricsEditor";
import { suggestedRetranscribeLanguage } from "../lib/languageRetranscription";

vi.mock("../i18n", () => ({
  useI18n: () => ({ t: (_key, fallback) => fallback }),
}));
vi.mock("./OnboardingTour", () => ({ EditorTour: () => null }));
vi.mock("./ToastProvider", () => ({
  useToast: () => ({ toast: vi.fn(), dismiss: () => {} }),
  ToastProvider: ({ children }) => children,
}));

function props(overrides = {}) {
  return {
    segments: [{ start: 1.0, end: 2.0, text: "great idol of asia" }],
    filename: "song.mp3",
    audioFile: null,
    referenceLyrics: "",
    onApprove: vi.fn(),
    onBack: vi.fn(),
    transcribeJobId: "job-retranscribe",
    segmentsRevision: 3,
    submitLabel: "Aprobar letra y timing",
    onPersistSegments: vi.fn().mockResolvedValue({ ok: true, revision: 3 }),
    ...overrides,
  };
}

afterEach(() => cleanup());

describe("LyricsEditor — volver a transcribir en otro idioma", () => {
  it("confirma, pollea hasta que la letra nueva está lista y recarga el editor", async () => {
    const onRetranscribeLanguage = vi.fn().mockResolvedValue({ ok: true, status: "queued" });
    const onPollRetranscription = vi.fn()
      .mockResolvedValueOnce({ ok: true, status: "idle" })          // on mount
      .mockResolvedValueOnce({ ok: true, status: "running", language: "es", progress: 40 })
      .mockResolvedValueOnce({ ok: true, status: "applied", language: "es", result_revision: 4 });
    const onReloadServer = vi.fn();
    render(<LyricsEditor {...props({
      languageConflict: true, onRetranscribeLanguage, onPollRetranscription, onReloadServer,
    })} />);

    const inline = await screen.findByTestId("retranscribe-inline");
    expect(within(inline).getByTestId("retranscribe-open").textContent)
      .toBe("Volver a transcribir en Español");
    fireEvent.change(within(inline).getByTestId("retranscribe-language-select"),
      { target: { value: "pt" } });
    expect(within(inline).getByTestId("retranscribe-open").textContent)
      .toBe("Volver a transcribir en Portugués");
    fireEvent.change(within(inline).getByTestId("retranscribe-language-select"),
      { target: { value: "es" } });
    fireEvent.click(within(inline).getByTestId("retranscribe-open"));

    const dialog = await screen.findByTestId("retranscribe-dialog");
    expect(dialog.textContent).toContain(
      "La versión actual queda guardada en el historial. Genly vuelve a escuchar la canción en Español y la letra nueva vuelve a revisión.",
    );
    expect(onRetranscribeLanguage).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByTestId("retranscribe-confirm"));

    await waitFor(() => expect(onRetranscribeLanguage)
      .toHaveBeenCalledWith({ language: "es", baseRevision: 3 }));
    await waitFor(() => expect(onReloadServer).toHaveBeenCalledTimes(1), { timeout: 10000 });
    expect(onPollRetranscription).toHaveBeenCalledTimes(3);
  }, 15000);

  it("muestra el error y no recarga si la nueva transcripción falla", async () => {
    const onPollRetranscription = vi.fn()
      .mockResolvedValueOnce({ ok: true, status: "idle" })
      .mockResolvedValueOnce({ ok: true, status: "failed", error: "No pudimos completar la transcripción." });
    const onReloadServer = vi.fn();
    render(<LyricsEditor {...props({
      languageUncertain: true,
      onRetranscribeLanguage: vi.fn().mockResolvedValue({ ok: true, status: "queued" }),
      onPollRetranscription, onReloadServer,
    })} />);
    fireEvent.click(await screen.findByTestId("retranscribe-open"));
    fireEvent.click(await screen.findByTestId("retranscribe-confirm"));
    const alert = await screen.findByRole("alert", {}, { timeout: 10000 });
    expect(alert.textContent).toContain("No pudimos completar la transcripción.");
    expect(onReloadServer).not.toHaveBeenCalled();
  }, 15000);

  it("no inicia nada si el servidor rechaza una versión vieja", async () => {
    const onRetranscribeLanguage = vi.fn().mockResolvedValue({
      ok: false, reason: "stale_revision",
      message: "La versión guardada cambió. Revisá la letra actual y volvé a intentar.",
    });
    const onPollRetranscription = vi.fn().mockResolvedValue({ ok: true, status: "idle" });
    render(<LyricsEditor {...props({
      languageConflict: true, onRetranscribeLanguage, onPollRetranscription,
    })} />);
    fireEvent.click(await screen.findByTestId("retranscribe-open"));
    fireEvent.click(await screen.findByTestId("retranscribe-confirm"));
    expect((await screen.findByRole("alert")).textContent).toContain("La versión guardada cambió");
    expect(onPollRetranscription).toHaveBeenCalledTimes(1); // only the mount check
  });

  it("se ofrece desde Herramientas aunque no haya alerta de idioma", async () => {
    render(<LyricsEditor {...props({
      onRetranscribeLanguage: vi.fn(),
      onPollRetranscription: vi.fn().mockResolvedValue({ ok: true, status: "idle" }),
    })} />);
    expect(screen.queryByTestId("retranscribe-inline")).toBeNull();
    fireEvent.click(screen.getByTestId("editor-overflow-btn"));
    fireEvent.click(await screen.findByTestId("retranscribe-menu-item"));
    expect(await screen.findByTestId("retranscribe-dialog")).toBeTruthy();
  });

  it("retoma el progreso si la canción ya se está transcribiendo", async () => {
    render(<LyricsEditor {...props({
      onRetranscribeLanguage: vi.fn(),
      onPollRetranscription: vi.fn().mockResolvedValue({ ok: true, status: "running", language: "en" }),
    })} />);
    const dialog = await screen.findByTestId("retranscribe-dialog");
    await waitFor(() => expect(dialog.textContent).toContain("escuchando la canción en Inglés"));
  });

  it("sin el callback no muestra la acción (wizard / edición post-render)", () => {
    render(<LyricsEditor {...props({ languageConflict: true })} />);
    expect(screen.queryByTestId("retranscribe-inline")).toBeNull();
    expect(screen.getByText(/Volvé atrás, elegí el idioma/)).toBeTruthy();
  });

  it("sugiere el idioma que escuchó la sonda, si viene", () => {
    expect(suggestedRetranscribeLanguage({ main_language: "pt" })).toBe("pt");
    expect(suggestedRetranscribeLanguage(null, { main_language: "EN" })).toBe("en");
    expect(suggestedRetranscribeLanguage({ main_language: "nn" })).toBe("es");
    expect(suggestedRetranscribeLanguage(undefined, undefined)).toBe("es");
  });
});
