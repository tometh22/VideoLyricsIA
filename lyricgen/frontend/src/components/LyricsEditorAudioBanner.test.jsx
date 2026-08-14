/**
 * Regresión del banner de audio del editor (reporte 2026-07-25): al editar
 * un job, "Audio no disponible para reproducir" aparecía ~30s mientras el
 * padre todavía traía la URL del audio (fetch con reintentos), y recién
 * después se podía escuchar. Falso alarma: el audio SÍ existía, estaba
 * cargando.
 *
 * Contrato post-fix: mientras audioLoading=true (fetch en vuelo) el editor
 * muestra "Cargando audio…", NO "Audio no disponible". Recién con
 * audioLoading=false y sin audioUrl (reintentos agotados / job sin input)
 * aparece "Audio no disponible".
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
    segments: [{ start: 1.0, end: 2.0, text: "alpha line" }],
    filename: "song.mp3",
    audioFile: null,
    referenceLyrics: "",
    onApprove: vi.fn(),
    onBack: vi.fn(),
    transcribeJobId: "job-audio-banner",
    ...overrides,
  };
}

afterEach(() => cleanup());

describe("LyricsEditor — banner de audio (cargando vs no disponible)", () => {
  it("audioLoading=true sin audioUrl → muestra 'Cargando audio…', NO 'Audio no disponible'", () => {
    render(<LyricsEditor {...baseProps({ audioUrl: null, audioLoading: true })} />);
    expect(screen.getByText("Cargando audio…")).toBeInTheDocument();
    expect(screen.queryByText(/Audio no disponible/)).not.toBeInTheDocument();
  });

  it("audioLoading=false sin audioUrl → recién ahí muestra 'Audio no disponible'", () => {
    render(<LyricsEditor {...baseProps({ audioUrl: null, audioLoading: false })} />);
    expect(screen.getByText(/Audio no disponible/)).toBeInTheDocument();
    expect(screen.queryByText("Cargando audio…")).not.toBeInTheDocument();
  });

  it("un 503 temporal no se presenta como audio inexistente y permite reintentar", () => {
    const onRetryAudio = vi.fn();
    render(<LyricsEditor {...baseProps({
      audioUrl: null,
      audioLoading: false,
      audioUnavailableReason: "temporary",
      onRetryAudio,
    })} />);
    expect(screen.getByText(/Audio temporalmente no disponible/)).toBeInTheDocument();
    screen.getAllByRole("button", { name: "Reintentar audio" })[0].click();
    expect(onRetryAudio).toHaveBeenCalledTimes(1);
  });

  it("con audioUrl → no muestra ninguno de los dos banners (hay reproductor)", () => {
    render(<LyricsEditor {...baseProps({ audioUrl: "https://r2.example/audio.wav", audioLoading: false })} />);
    expect(screen.queryByText("Cargando audio…")).not.toBeInTheDocument();
    expect(screen.queryByText(/Audio no disponible/)).not.toBeInTheDocument();
  });
});
