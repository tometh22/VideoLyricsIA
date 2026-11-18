import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import LyricsTimeline from "./LyricsTimeline";

afterEach(() => { cleanup(); vi.restoreAllMocks(); });
const callbacks = () => ({ onSeek: vi.fn(), onTimingChange: vi.fn(), onTimingChangeBatch: vi.fn(), onDragStart: vi.fn() });

describe("timeline interaction regression contracts from audit 3", () => {
  it("aligns waveform peaks to audio time even when the viewport is wider than the song", () => {
    const fillRect = vi.fn();
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({ setTransform: vi.fn(), clearRect: vi.fn(), fillRect });
    render(<LyricsTimeline segments={[{ _id: 1, start: 5, end: 6, text: "at the peak" }]}
      duration={10} currentTime={5} waveform={{ duration: 10, peaks: [0, 1] }} />);
    const zoom = Number(screen.getByTestId("timeline-lane").dataset.pxPerSec);
    const lastPass = fillRect.mock.calls.slice(-2);
    expect(lastPass[1][0]).toBeCloseTo(5 * zoom, 5);
  });

  it("keeps the waveform aligned when lyrics extend beyond the audio metadata", () => {
    const fillRect = vi.fn();
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({ setTransform: vi.fn(), clearRect: vi.fn(), fillRect });
    render(<LyricsTimeline segments={[{ _id: 1, start: 29, end: 30, text: "legacy outlier" }]}
      duration={10} currentTime={5} waveform={{ duration: 10, peaks: [0, 1] }} />);
    const zoom = Number(screen.getByTestId("timeline-lane").dataset.pxPerSec);
    expect(fillRect.mock.calls.slice(-2)[1][0]).toBeCloseTo(5 * zoom, 5);
  });

  it("retains the full waveform extent before audio metadata loads", () => {
    const fillRect = vi.fn();
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({ setTransform: vi.fn(), clearRect: vi.fn(), fillRect });
    const { container } = render(<LyricsTimeline segments={[{ _id: 1, start: 0, end: 2, text: "intro" }]}
      duration={0} currentTime={0} waveform={{ duration: 100, peaks: [0, 1] }} />);
    const zoom = Number(screen.getByTestId("timeline-lane").dataset.pxPerSec);
    expect(fillRect.mock.calls.slice(-2)[1][0]).toBeCloseTo(50 * zoom, 5);
    expect(parseFloat(container.querySelector("canvas").style.width)).toBeGreaterThanOrEqual(100 * zoom);
  });

  it("reveals and selects an explicitly focused line without moving the outer page", () => {
    const rows = [{ _id: 0, start: 0, end: 2, text: "first" }, { _id: 1, start: 120, end: 122, text: "target" }];
    const props = { segments: rows, duration: 180, currentTime: 0, ...callbacks() };
    const { rerender } = render(<LyricsTimeline {...props} focusedSegId={null} />);
    const scroll = screen.getByTestId("timeline-scroll");
    Object.defineProperty(scroll, "clientWidth", { configurable: true, value: 960 });
    scroll.scrollTo = vi.fn(({ left }) => { scroll.scrollLeft = left; });
    const outerScroll = vi.spyOn(HTMLElement.prototype, "scrollIntoView");
    rerender(<LyricsTimeline {...props} focusedSegId={1} />);
    expect(screen.getAllByTestId("timeline-segment")[1]).toHaveAttribute("aria-pressed", "true");
    expect(scroll.scrollLeft).toBeGreaterThan(0);
    expect(outerScroll).not.toHaveBeenCalled();
  });

  it("keeps a multiselection when focus enters one of its members", () => {
    const rows = [{ _id: 0, start: 0, end: 2, text: "first" }, { _id: 1, start: 5, end: 7, text: "second" }];
    const props = { segments: rows, duration: 10, currentTime: 0 };
    const { rerender } = render(<LyricsTimeline {...props} focusedSegId={null} />);
    for (const segment of screen.getAllByTestId("timeline-segment")) fireEvent.keyDown(segment, { key: "Enter" });
    rerender(<LyricsTimeline {...props} focusedSegId={1} />);
    expect(screen.getByTestId("timeline-primary-actions")).toHaveAttribute("data-selected-count", "2");
  });

  it("does not reselect a cleared line when zoom changes", () => {
    render(<LyricsTimeline segments={[{ _id: 1, start: 1, end: 2, text: "line" }]}
      duration={10} currentTime={1} focusedSegId={1} />);
    fireEvent.click(screen.getByRole("button", { name: "Limpiar selección" }));
    fireEvent.click(screen.getByRole("button", { name: "Más acciones" }));
    fireEvent.click(screen.getByRole("button", { name: "Acercar" }));
    expect(screen.getByTestId("timeline-primary-actions")).toHaveAttribute("data-selected-count", "0");
  });

  it("resuming temporarily suspended follow keeps following enabled", () => {
    render(<LyricsTimeline segments={[{ _id: 1, start: 1, end: 2, text: "line" }]}
      duration={10} currentTime={1} isPlaying />);
    fireEvent.wheel(screen.getByTestId("timeline-scroll"));
    fireEvent.click(screen.getByRole("button", { name: "Más acciones" }));
    fireEvent.click(screen.getByRole("menuitem", { name: /Reanudar seguimiento/ }));
    expect(screen.getByRole("menuitem", { name: /Seguir reproducción/ })).toHaveAttribute("aria-pressed", "true");
  });

  it("moving a short valid lyric preserves its original duration", () => {
    const props = callbacks();
    render(<LyricsTimeline segments={[{ _id: 1, start: 1, end: 1.1, text: "¡Eh!" }]}
      duration={10} currentTime={0} {...props} />);
    const body = screen.getByTestId("timeline-segment-body");
    fireEvent.pointerDown(body, { clientX: 100, pointerId: 1, button: 0 });
    fireEvent.pointerMove(body, { clientX: 148, pointerId: 1 });
    fireEvent.pointerUp(body, { clientX: 148, pointerId: 1 });
    expect(props.onTimingChange).toHaveBeenCalledTimes(1);
    const [, start, end] = props.onTimingChange.mock.calls[0];
    expect(end - start).toBeCloseTo(0.1, 5);
  });
});
