import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import GuidedTimingReview from "./GuidedTimingReview";

vi.mock("../i18n", () => ({ useI18n: () => ({ t: (_key, fallback) => fallback }) }));
afterEach(() => { cleanup(); vi.restoreAllMocks(); });
const props = () => ({
  windows: [{ id: "same-region", start: 12, end: 14, reasons: ["timing"] }],
  segments: [{ _id: 1, start: 12, end: 14, text: "Una frase" }],
  duration: 100, currentTime: 0, audioAvailable: true, confirmedIds: new Set(),
  onPlayWindow: vi.fn().mockResolvedValue(true), onConfirm: vi.fn(), onStopPlayback: vi.fn(),
});

describe("guided review contracts audit 4", () => {
  it("requires listening again if a window keeps its identity but changes its audio range", async () => {
    const initial = props();
    const view = render(<GuidedTimingReview {...initial} />);
    await userEvent.click(screen.getByRole("button", { name: /Escuchar fragmento/ }));
    await userEvent.click(screen.getByRole("button", { name: "Sí, está bien" }));
    expect(screen.getByRole("button", { name: /Confirmar tramo/ })).toBeEnabled();
    view.rerender(<GuidedTimingReview {...initial} windows={[{ ...initial.windows[0], start: 40, end: 43 }]} />);
    const confirm = screen.getByRole("button", { name: /Confirmar tramo/ });
    expect(confirm).toBeDisabled();
    fireEvent.click(confirm);
    expect(initial.onConfirm).not.toHaveBeenCalled();
  });

  it("draws each peak at its real audio time inside a fractional focused range", () => {
    const fillRect = vi.fn();
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({ setTransform: vi.fn(), clearRect: vi.fn(), fillRect });
    const peaks = Array(100).fill(0);
    peaks[11] = 1;
    render(<GuidedTimingReview {...props()} waveform={{ duration: 100, peaks }} />);
    // Window12..14 +1.5s context => viewport10.5..15.5. The peak at11s
    // belongs 10% into that viewport, not at index1/6=16.7%.
    const canvas = screen.getByTestId("guided-waveform").querySelector("canvas");
    const fullHeightPeak = fillRect.mock.calls.find(call => call[3] === 66);
    expect(fullHeightPeak).toBeDefined();
    expect(fullHeightPeak[0]).toBeCloseTo(Number.parseFloat(canvas.style.width) * 0.1, 5);
  });
  it("uses the actual focused span for audio shorter than half a second", () => {
    const fillRect = vi.fn();
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({ setTransform: vi.fn(), clearRect: vi.fn(), fillRect });
    render(<GuidedTimingReview {...props()} duration={0.2}
      windows={[{ id: "short", start: 0, end: 0.2 }]}
      segments={[{ _id: 1, start: 0, end: 0.2, text: "Ah" }]}
      waveform={{ duration: 0.2, peaks: [0, 1] }} />);
    const canvas = screen.getByTestId("guided-waveform").querySelector("canvas");
    const peak = fillRect.mock.calls.find(call => call[3] === 66);
    expect(peak[0]).toBeCloseTo(Number.parseFloat(canvas.style.width) / 2, 5);
  });

});
