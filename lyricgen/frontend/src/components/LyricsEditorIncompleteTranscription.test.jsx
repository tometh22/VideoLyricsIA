import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import LyricsEditor, { incompleteVocalWindows } from "./LyricsEditor";
import { segmentsStore } from "../state/segmentsStore";

vi.mock("../i18n", () => ({ useI18n: () => ({ t: () => undefined }) }));
vi.mock("./OnboardingTour", () => ({ EditorTour: () => null }));
vi.mock("./ToastProvider", () => ({
  useToast: () => ({ toast: vi.fn(), dismiss: () => {} }),
  ToastProvider: ({ children }) => children,
}));

// Production runs quality in `observe`: the warning must not depend on enforce.
const QUALITY = {
  policy_version: "lyrics-quality-v6",
  mode: "observe",
  decision: "review_required",
  evaluated_revision: 0,
  unsafe_windows: [
    { start: 38.1, end: 64.1, reason: "vocal_window_unrecognized", attempts: 5 },
  ],
  vocal_windows: {
    incomplete: [{ start: 38.1, end: 64.1, reason: "vocal_window_unrecognized" }],
    incomplete_seconds: 26,
  },
};

function props(overrides = {}) {
  return {
    segments: [{ start: 27.4, end: 37.7, text: "Y te explota en la cabeza" },
               { start: 64.1, end: 71.5, text: "Cosa poco importante" }],
    filename: "song.mp3", audioFile: null, audioUrl: "https://media.example.test/song.wav",
    referenceLyrics: "", transcriptionQuality: QUALITY, transcribeJobId: "job-incomplete",
    segmentsRevision: 0, onApprove: vi.fn(), onBack: vi.fn(), ...overrides,
  };
}

afterEach(() => { cleanup(); segmentsStore._clearAll(); });

describe("LyricsEditor — transcripción incompleta", () => {
  it("normaliza y deduplica tramos de ambas fuentes", () => {
    expect(incompleteVocalWindows(QUALITY)).toEqual([{ start: 38.1, end: 64.1 }]);
    expect(incompleteVocalWindows({ unsafe_windows: [{ start: 5, end: 3, reason: "vocal_window_unrecognized" }] })).toEqual([]);
    expect(incompleteVocalWindows(null)).toEqual([]);
  });

  it("muestra los segundos sin transcribir y lleva al tramo", () => {
    const { container } = render(<LyricsEditor {...props()} />);
    const alert = screen.getByTestId("incomplete-transcription");
    expect(alert).toHaveTextContent("26 s con voz quedaron sin transcribir");
    const button = within(alert).getByRole("button", { name: "0:38–1:04" });
    const audio = container.querySelector("audio");
    Object.defineProperty(audio, "duration", { configurable: true, value: 120 });
    Object.defineProperty(audio, "readyState", { configurable: true, value: 1 });
    fireEvent.loadedMetadata(audio);
    fireEvent.click(button);
    expect(audio.currentTime).toBeCloseTo(38.1, 1);
  });

  it("no muestra nada cuando todos los tramos se transcribieron", () => {
    render(<LyricsEditor {...props({ transcriptionQuality: { ...QUALITY, unsafe_windows: [], vocal_windows: { incomplete: [] } } })} />);
    expect(screen.queryByTestId("incomplete-transcription")).toBeNull();
  });
});
