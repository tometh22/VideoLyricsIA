import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import LyricsEditor from "./LyricsEditor";
import { segmentsStore } from "../state/segmentsStore";
import { captureHandledError } from "../observability";

vi.mock("../i18n", () => ({ useI18n: () => ({ t: () => undefined }) }));
vi.mock("../observability", () => ({ captureHandledError: vi.fn() }));
vi.mock("./OnboardingTour", () => ({ EditorTour: () => null }));
vi.mock("./ToastProvider", () => ({ useToast: () => ({ toast: vi.fn(), dismiss: vi.fn() }) }));

const lines = [
  { _id: 0, segment_id: "early", start: 1, end: 3, text: "Primera frase" },
  { _id: 1, segment_id: "later", start: 10, end: 14, text: "Segunda frase", review: true },
];
const reply = (body, status = 200) => new Response(JSON.stringify(body), { status });
let key;

function mount({ audioUrl = "https://example.test/synthetic.wav", rejectTelemetry = false, deferDocument = false } = {}) {
  key = `context-playback-${Math.random()}`;
  const request = vi.fn(async (path, options = {}) => {
    if (deferDocument && path === `/editor/${key}` && !options.method) return new Promise(() => {});
    if (path === `/editor/${key}` && !options.method) return reply({
      job_id: key, revision: 4, segments: lines, original_segments: lines,
      lock: { active: false },
    });
    if (path.endsWith("/lock/heartbeat")) return reply({ acquired: true, user: { id: 42 }, expires_at: "2099-01-01T00:00:00Z" });
    if (path === "/analytics/events") return reply(rejectTelemetry ? { accepted: 0, rejected: 1 } : { accepted: 1, rejected: 0 });
    return reply({});
  });
  const view = render(<LyricsEditor segments={lines} filename="synthetic.wav"
    transcribeJobId={key} editorRequest={request} audioUrl={audioUrl}
    user={{ id: 42, tenant_id: "test", features: { editor_v2: true } }}
    onApprove={vi.fn()} onBack={vi.fn()} onPersistSegments={vi.fn()} />);
  return { ...view, request };
}
function events(request, name) {
  return request.mock.calls.filter(([path]) => path === "/analytics/events")
    .flatMap(([, options]) => JSON.parse(options.body).events).filter((e) => e.name === name);
}

beforeEach(() => { localStorage.clear(); vi.clearAllMocks(); });
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it("plays two seconds before the chosen line, repeats, and records actual cursor origin without editing", async () => {
  const { container, request } = mount();
  await waitFor(() => expect(screen.getByDisplayValue("Segunda frase")).toBeEnabled());
  const audio = container.querySelector("audio");
  const play = vi.spyOn(audio, "play").mockResolvedValue();
  const before = JSON.stringify(segmentsStore.get(key));
  audio.currentTime = 22;
  fireEvent.click(screen.getByRole("button", { name: "Escuchar 2 s antes de la línea 2" }));
  expect(audio.currentTime).toBe(8);
  expect(play).toHaveBeenCalled();
  await waitFor(() => expect(events(request, "editor_line_context_played")).toHaveLength(1));
  expect(events(request, "editor_line_context_played")[0].properties).toMatchObject({
    line_id: "1", line_index: 1, segment_id: "later", revision: 4, line_context: "target", from_position_ms: 22000,
    position_ms: 8000, line_start_ms: 10000, requested_lead_in_ms: 2000,
    effective_lead_in_ms: 2000, review_marker: "review", unsaved_changes: false,
  });
  audio.currentTime = 12.25;
  fireEvent.click(screen.getByRole("button", { name: "Escuchar 2 s antes de la línea 2" }));
  expect(audio.currentTime).toBe(8);
  await waitFor(() => expect(events(request, "editor_line_context_played")).toHaveLength(2));
  expect(events(request, "editor_line_context_played")[1].properties.from_position_ms).toBe(12250);
  expect(events(request, "editor_seek")).toHaveLength(0); // no double-counting the new action
  expect(JSON.stringify(segmentsStore.get(key))).toBe(before);
  expect(request.mock.calls.some(([, o]) => o?.method === "PATCH")).toBe(false);
});

it("clamps early lines at zero and preserves the normal timestamp action", async () => {
  const { container, request } = mount();
  await screen.findByDisplayValue("Primera frase");
  fireEvent.click(screen.getByRole("button", { name: "Escuchar 2 s antes de la línea 1" }));
  expect(container.querySelector("audio").currentTime).toBe(0);
  await waitFor(() => expect(events(request, "editor_line_context_played")).toHaveLength(1));
  expect(events(request, "editor_line_context_played")[0].properties).toMatchObject({
    line_id: "0", line_index: 0, segment_id: "early", effective_lead_in_ms: 1000, review_marker: "none",
  });
  fireEvent.click(within(screen.getByTestId("lyric-row-2")).getByRole("button", { name: /^Reproducir desde/ }));
  expect(container.querySelector("audio").currentTime).toBe(10);
  expect(events(request, "editor_seek")[0].properties).toMatchObject({
    line_id: "1", line_index: 1, segment_id: "later", line_context: "target", position_ms: 10000,
  });
});

it("disables the action without audio", async () => {
  mount({ audioUrl: null });
  expect(await screen.findByRole("button", { name: "Escuchar 2 s antes de la línea 1" })).toBeDisabled();
});

it("does not count a browser playback rejection as a successful listen", async () => {
  const { container, request } = mount();
  await screen.findByDisplayValue("Primera frase");
  vi.spyOn(container.querySelector("audio"), "play").mockRejectedValue(new Error("NotAllowedError"));
  fireEvent.click(screen.getByRole("button", { name: "Escuchar 2 s antes de la línea 2" }));
  await waitFor(() => expect(container.querySelector("audio").currentTime).toBe(8));
  expect(events(request, "editor_line_context_played")).toHaveLength(0);
});

it("reports an HTTP-200 telemetry rejection once without preventing playback", async () => {
  const { container } = mount({ rejectTelemetry: true });
  await screen.findByDisplayValue("Primera frase");
  fireEvent.click(screen.getByRole("button", { name: "Escuchar 2 s antes de la línea 2" }));
  await waitFor(() => expect(captureHandledError).toHaveBeenCalledTimes(1));
  fireEvent.click(screen.getByRole("button", { name: "Escuchar 2 s antes de la línea 1" }));
  expect(container.querySelector("audio").currentTime).toBe(0);
  expect(captureHandledError).toHaveBeenCalledTimes(1);
});


it("does not invent revision zero while the durable document is still loading", async () => {
  const { request } = mount({ deferDocument: true });
  fireEvent.click(await screen.findByRole("button", { name: "Escuchar 2 s antes de la línea 1" }));
  await waitFor(() => expect(events(request, "editor_line_context_played")).toHaveLength(1));
  expect(events(request, "editor_line_context_played")[0].properties).not.toHaveProperty("revision");
});
