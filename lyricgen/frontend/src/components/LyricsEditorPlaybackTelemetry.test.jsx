import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import LyricsEditor from "./LyricsEditor";
import { segmentsStore } from "../state/segmentsStore";

vi.mock("../i18n", () => ({
  useI18n: () => ({ t: (_key, fallback) => fallback }),
}));
vi.mock("./OnboardingTour", () => ({ EditorTour: () => null }));
vi.mock("./ToastProvider", () => ({
  useToast: () => ({ toast: vi.fn(), dismiss: vi.fn() }),
  ToastProvider: ({ children }) => children,
}));

const JOB = "listen-job";
const USER = { id: 7, username: "operator", tenant_id: "listen-team", features: {} };
const SEGMENTS = [
  { _id: "line-1", start: 0, end: 2, text: "hoy te vi pasar" },
  { _id: "line-2", start: 2, end: 4, text: "por la vereda" },
];

function renderEditor(editorRequest) {
  return render(<LyricsEditor
    segments={SEGMENTS}
    filename="song.wav"
    user={USER}
    transcribeJobId={JOB}
    storeKey={`${JOB}-${Math.random()}`}
    editorRequest={editorRequest}
    audioUrl="https://example.test/song.mp3"
    disableAutosave
    onApprove={vi.fn()}
    onBack={vi.fn()}
  />);
}

// jsdom no reproduce audio: se simula el estado del elemento y sus eventos.
function mediaState(audio) {
  const state = { paused: true, currentTime: 0, duration: 180, playbackRate: 1 };
  for (const key of Object.keys(state)) {
    Object.defineProperty(audio, key, {
      configurable: true,
      get: () => state[key],
      set: (value) => { state[key] = value; },
    });
  }
  return state;
}

function analyticsEvents(editorRequest, name) {
  return editorRequest.mock.calls
    .filter(([path]) => path === "/analytics/events")
    .map(([, options]) => ({ options, event: JSON.parse(options.body).events[0] }))
    .filter(({ event }) => !name || event.name === name);
}

afterEach(() => {
  cleanup();
  localStorage.clear();
  segmentsStore._clearAll();
});

describe("editor_audio_played", () => {
  it("manda los tramos que sonaron al salir del editor, sólo números y con hora del evento", async () => {
    const editorRequest = vi.fn(async () => ({ ok: true, status: 200, clone: () => ({ json: async () => ({}) }) }));
    const { container, unmount } = renderEditor(editorRequest);
    await screen.findByDisplayValue("hoy te vi pasar");
    const audio = container.querySelector("audio");
    const media = mediaState(audio);

    media.paused = false;
    fireEvent.play(audio);
    for (const t of [0.5, 1, 1.5, 2]) {
      media.currentTime = t;
      fireEvent.timeUpdate(audio);
    }
    // Un salto mientras suena abre otro tramo.
    media.currentTime = 30;
    fireEvent.seeking(audio);
    media.currentTime = 31;
    fireEvent.timeUpdate(audio);
    media.paused = true;
    fireEvent.pause(audio);
    expect(analyticsEvents(editorRequest, "editor_audio_played")).toHaveLength(0);

    unmount();
    const [played] = analyticsEvents(editorRequest, "editor_audio_played");
    expect(played.options.keepalive).toBe(true);
    expect(played.event.job_id).toBe(JOB);
    expect(Number.isNaN(Date.parse(played.event.occurred_at))).toBe(false);
    expect(played.event.properties).toMatchObject({
      ranges: [[0, 2000], [30_000, 31_000]],
      played_ms: 3000,
      playback_rate: 1,
      audio_duration_ms: 180_000,
      flush_reason: "unmount",
    });
    expect(played.event.properties.session_id).toBeTruthy();
    expect(JSON.stringify(played.event)).not.toContain("hoy te vi pasar");
  });

  it("al ocultar la pestaña envía con keepalive y no repite lo ya enviado", async () => {
    const editorRequest = vi.fn(async () => ({ ok: true, status: 200, clone: () => ({ json: async () => ({}) }) }));
    const { container, unmount } = renderEditor(editorRequest);
    await screen.findByDisplayValue("hoy te vi pasar");
    const audio = container.querySelector("audio");
    const media = mediaState(audio);
    media.currentTime = 10;
    media.paused = false;
    fireEvent.play(audio);
    media.currentTime = 11;
    fireEvent.timeUpdate(audio);

    window.dispatchEvent(new Event("pagehide"));
    const [hidden] = analyticsEvents(editorRequest, "editor_audio_played");
    expect(hidden.options.keepalive).toBe(true);
    expect(hidden.event.properties).toMatchObject({ ranges: [[10_000, 11_000]], flush_reason: "hidden" });

    media.paused = true;
    fireEvent.pause(audio);
    unmount();
    // Nada nuevo sonó después del envío: no hay un segundo evento.
    expect(analyticsEvents(editorRequest, "editor_audio_played")).toHaveLength(1);
  });

  it("todo evento del editor lleva occurred_at", async () => {
    const editorRequest = vi.fn(async () => ({ ok: true, status: 200, clone: () => ({ json: async () => ({}) }) }));
    renderEditor(editorRequest);
    await screen.findByDisplayValue("hoy te vi pasar");
    const events = analyticsEvents(editorRequest);
    expect(events.length).toBeGreaterThan(0);
    for (const { event } of events) {
      expect(Number.isNaN(Date.parse(event.occurred_at))).toBe(false);
    }
  });
});
