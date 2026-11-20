import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import NativeLyricPreview from "./NativeLyricPreview";
import { createSubtitleRenderer } from "../lib/nativeSubtitleRenderer";

vi.mock("../i18n", () => ({ useI18n: () => ({ t: () => undefined }) }));
vi.mock("../lib/nativeSubtitleRenderer", () => ({ createSubtitleRenderer: vi.fn() }));
let instance;
beforeEach(() => {
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
  instance = { ready: Promise.resolve(), manualRender: vi.fn().mockResolvedValue(), destroy: vi.fn().mockResolvedValue(),
    renderer: { addFonts: vi.fn().mockResolvedValue(), setTrack: vi.fn().mockResolvedValue() } };
  createSubtitleRenderer.mockReturnValue(instance);
});
afterEach(() => { cleanup(); vi.clearAllMocks(); vi.unstubAllGlobals(); });
const track = (ass, font = "test.ttf") => ({ ass, fonts: [font], width: 1920, height: 1080 });
const reply = data => ({ ok: true, json: async () => data });
const props = request => ({ request, segments: [{ start: 0, end: 2, text: "Original" }], settings: {}, jobId: "job", currentTime: 1 });

it("keeps one worker, reuses fonts, updates text and releases the worker on unmount", async () => {
  let content = "original ASS";
  const request = vi.fn(async url => url.endsWith("text-preview") ? reply(track(content))
    : { ok: true, arrayBuffer: async () => new ArrayBuffer(4) });
  const view = render(<NativeLyricPreview {...props(request)} />);
  await waitFor(() => expect(createSubtitleRenderer).toHaveBeenCalled());
  expect(createSubtitleRenderer.mock.calls[0][0]).toHaveProperty("width", 1920);
  content = "new ASS";
  view.rerender(<NativeLyricPreview {...props(request)} segments={[{ start: 0, end: 2, text: "Nueva" }]} />);
  await waitFor(() => expect(instance.renderer.setTrack).toHaveBeenCalledWith("new ASS"));
  expect(createSubtitleRenderer).toHaveBeenCalledTimes(1);
  expect(instance.renderer.addFonts).not.toHaveBeenCalled();
  expect(request.mock.calls.filter(([url]) => url.includes("/fonts/"))).toHaveLength(1);
  view.unmount();
  await waitFor(() => expect(instance.destroy).toHaveBeenCalledTimes(1));
});

it("discards a slow obsolete response after the operator edits again", async () => {
  let resolveOld;
  const old = new Promise(resolve => { resolveOld = resolve; });
  let calls = 0;
  const request = vi.fn(async url => {
    if (!url.endsWith("text-preview")) return { ok: true, arrayBuffer: async () => new ArrayBuffer(4) };
    return ++calls === 1 ? old : reply(track("latest ASS", "race.ttf"));
  });
  const view = render(<NativeLyricPreview {...props(request)} />);
  await waitFor(() => expect(calls).toBe(1));
  view.rerender(<NativeLyricPreview {...props(request)} segments={[{ start: 0, end: 2, text: "Última edición" }]} />);
  await waitFor(() => expect(createSubtitleRenderer).toHaveBeenCalled());
  await act(async () => { resolveOld(reply(track("obsolete ASS", "race.ttf"))); });
  expect(createSubtitleRenderer.mock.calls[0][1]).toBe("latest ASS");
  expect(instance.renderer.setTrack).not.toHaveBeenCalledWith("obsolete ASS");
});

it("shows an explicit retry when preview fails instead of a differently wrapped approximation", async () => {
  const request = vi.fn().mockResolvedValue({ ok: false });
  render(<NativeLyricPreview {...props(request)} />);
  await screen.findByText("No se pudo mostrar la vista previa del texto.");
  request.mockImplementation(async url => url.endsWith("text-preview") ? reply(track("recovered ASS", "retry.ttf"))
    : { ok: true, arrayBuffer: async () => new ArrayBuffer(4) });
  fireEvent.click(screen.getByRole("button", { name: "Reintentar" }));
  await waitFor(() => expect(createSubtitleRenderer).toHaveBeenCalled());
});
