import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import LyricsTimeline from "./LyricsTimeline";

afterEach(() => { cleanup(); vi.restoreAllMocks(); });
const callbacks = () => ({ onTimingChange: vi.fn(), onTimingChangeBatch: vi.fn(), onDragStart: vi.fn(), onSeek: vi.fn() });
const drag = (body, from, to) => {
  fireEvent.pointerDown(body, { clientX: from, pointerId: 1, button: 0 });
  fireEvent.pointerMove(body, { clientX: to, pointerId: 1 });
  fireEvent.pointerUp(body, { clientX: to, pointerId: 1 });
};

describe("timeline interaction regression contracts from audit 4", () => {
  it("never uses the minimum visual width as the duration of a subsecond audio clip", () => {
    const props = callbacks();
    render(<LyricsTimeline segments={[{ _id: 1, start: 0.1, end: 0.2, text: "¡Eh!" }]}
      duration={0.5} currentTime={0} {...props} />);
    drag(screen.getByTestId("timeline-segment-body"), 100, 148);
    expect(props.onTimingChange).toHaveBeenCalledTimes(1);
    const [, start, end] = props.onTimingChange.mock.calls[0];
    expect(start).toBeGreaterThanOrEqual(0);
    expect(end).toBeLessThanOrEqual(0.5);
    expect(end - start).toBeCloseTo(0.1, 6);
  });

  it("honors an explicitly focused identity when its document hydrates later", () => {
    const props = { duration: 180, currentTime: 0, focusedSegId: 1, ...callbacks() };
    const { rerender } = render(<LyricsTimeline {...props} segments={[]} />);
    const scroll = screen.getByTestId("timeline-scroll");
    Object.defineProperty(scroll, "clientWidth", { configurable: true, value: 960 });
    scroll.scrollTo = vi.fn(({ left }) => { scroll.scrollLeft = left; });
    rerender(<LyricsTimeline {...props} segments={[{ _id: 1, start: 120, end: 122, text: "late document" }]} />);
    expect(screen.getByTestId("timeline-segment")).toHaveAttribute("aria-pressed", "true");
    expect(scroll.scrollLeft).toBeGreaterThan(0);
  });

  it("Escape cancels a previewed drag before pointer release can persist it", () => {
    const props = callbacks();
    render(<><button>Outside timeline</button><LyricsTimeline segments={[{ _id: 1, start: 1, end: 2, text: "cancel me" }]}
      duration={10} currentTime={0} {...props} /></>);
    const outside = screen.getByRole("button", { name: "Outside timeline" });
    outside.focus();
    const body = screen.getByTestId("timeline-segment-body");
    body.hasPointerCapture = vi.fn(() => true);
    body.releasePointerCapture = vi.fn();
    fireEvent.pointerDown(body, { clientX: 100, pointerId: 1, button: 0 });
    fireEvent.pointerMove(body, { clientX: 148, pointerId: 1 });
    fireEvent.keyDown(outside, { key: "Escape", code: "Escape" });
    expect(body.releasePointerCapture).toHaveBeenCalledWith(1);
    fireEvent.pointerUp(body, { clientX: 148, pointerId: 1 });
    expect(props.onDragStart).not.toHaveBeenCalled();
    expect(props.onTimingChange).not.toHaveBeenCalled();
    expect(props.onTimingChangeBatch).not.toHaveBeenCalled();
  });
  it("Escape cancels marquee selection without undo history or timing changes", () => {
    const props = { ...callbacks(), onSelectionCreated: vi.fn() };
    render(<LyricsTimeline segments={[{ _id: 1, start: 1, end: 2, text: "keep selection" }]}
      duration={10} currentTime={0} focusedSegId={1} {...props} />);
    const lane = screen.getByTestId("timeline-lane");
    lane.hasPointerCapture = vi.fn(() => true);
    lane.releasePointerCapture = vi.fn();
    fireEvent.pointerDown(lane, { clientX: 10, clientY: 0, pointerId: 7, button: 0 });
    fireEvent.pointerMove(lane, { clientX: 148, clientY: 45, pointerId: 7 });
    fireEvent.keyDown(window, { key: "Escape", code: "Escape" });
    fireEvent.pointerUp(lane, { clientX: 148, clientY: 45, pointerId: 7 });
    expect(lane.releasePointerCapture).toHaveBeenCalledWith(7);
    expect(props.onSelectionCreated).not.toHaveBeenCalled();
    expect(props.onDragStart).not.toHaveBeenCalled();
    expect(props.onTimingChange).not.toHaveBeenCalled();
    expect(props.onTimingChangeBatch).not.toHaveBeenCalled();
    expect(screen.getByTestId("timeline-primary-actions")).toHaveAttribute("data-selected-count", "1");
  });

  it("clearing hydrated focus stays cleared through text updates", () => {
    const props = { duration: 180, currentTime: 0, focusedSegId: 1 };
    const { rerender } = render(<LyricsTimeline {...props} segments={[]} />);
    rerender(<LyricsTimeline {...props} segments={[{ _id: 1, start: 120, end: 122, text: "hydrated" }]} />);
    fireEvent.click(screen.getByRole("button", { name: "Limpiar selección" }));
    rerender(<LyricsTimeline {...props} segments={[{ _id: 1, start: 120, end: 122, text: "updated text" }]} />);
    expect(screen.getByTestId("timeline-primary-actions")).toHaveAttribute("data-selected-count", "0");
  });
  it.each(["drag", "nudge"])("%s never reverses an outward request for an existing outlier", (method) => {
    const props = callbacks();
    render(<LyricsTimeline segments={[{ _id: 1, start: 1.1, end: 1.2, text: "legacy outlier" }]}
      duration={1} currentTime={0} focusedSegId={1} {...props} />);
    if (method === "drag") drag(screen.getByTestId("timeline-segment-body"), 100, 148);
    else fireEvent.click(screen.getByRole("button", { name: "+100 ms" }));
    expect(props.onDragStart).not.toHaveBeenCalled();
    expect(props.onTimingChange).not.toHaveBeenCalled();
    expect(props.onTimingChangeBatch).not.toHaveBeenCalled();
  });

  it.each(["drag", "nudge"])("%s still recovers an outlier when the requested direction is inward", (method) => {
    const props = callbacks();
    render(<LyricsTimeline segments={[{ _id: 1, start: 1.1, end: 1.2, text: "legacy outlier" }]}
      duration={1} currentTime={0} focusedSegId={1} {...props} />);
    let start, end;
    if (method === "drag") {
      drag(screen.getByTestId("timeline-segment-body"), 100, 52);
      expect(props.onTimingChange).toHaveBeenCalledTimes(1);
      [, start, end] = props.onTimingChange.mock.calls[0];
    } else {
      fireEvent.click(screen.getByRole("button", { name: "−100 ms" }));
      expect(props.onTimingChangeBatch).toHaveBeenCalledTimes(1);
      ({ start, end } = props.onTimingChangeBatch.mock.calls[0][0][0]);
    }
    expect(start).toBeLessThan(1.1);
    expect(start).toBeGreaterThanOrEqual(0);
    expect(end).toBeLessThanOrEqual(1);
    expect(end - start).toBeCloseTo(0.1, 6);
  });

});
