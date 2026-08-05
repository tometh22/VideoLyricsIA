import { render, screen, cleanup, fireEvent } from "@testing-library/react";
import { afterEach, describe, it, expect, vi } from "vitest";
import LyricsTimeline from "./LyricsTimeline";

afterEach(cleanup);

const SEGS = [
  { _id: 0, start: 0, end: 2, text: "primera línea" },
  { _id: 1, start: 10, end: 11, text: "segunda línea" },
  { _id: 2, start: 40, end: 41, text: "tercera línea" },
];

function setup(overrides = {}) {
  const props = {
    segments: SEGS,
    duration: 60,
    currentTime: 5,
    activeId: null,
    focusedSegId: null,
    highlightedIds: new Set(),
    onSeek: vi.fn(),
    onDragStart: vi.fn(),
    onTimingChange: vi.fn(),
    onTimingChangeBatch: vi.fn(),
    onTextChange: vi.fn(),
    onFocus: vi.fn(),
    onReset: vi.fn(),
    ...overrides,
  };
  render(<LyricsTimeline {...props} />);
  return props;
}

describe("LyricsTimeline", () => {
  it("renders one horizontal block per segment", () => {
    setup();
    expect(screen.getByText("primera línea")).toBeInTheDocument();
    expect(screen.getByText("segunda línea")).toBeInTheDocument();
    expect(screen.getByText("tercera línea")).toBeInTheDocument();
  });

  it("keeps blocks visible when API timings arrive as strings", () => {
    setup({
      segments: [
        { _id: "a", start: "13.2", end: "15.8", text: "línea desde API" },
      ],
    });
    const block = screen.getAllByTestId("timeline-segment")[0];
    const zoom = Number(screen.getByTestId("timeline-lane").dataset.pxPerSec);
    expect(block).toBeInTheDocument();
    expect(parseFloat(block.style.left)).toBeCloseTo(13.2 * zoom, 5);
    expect(parseFloat(block.style.width)).toBeCloseTo(Math.max(56, 2.6 * zoom), 5);
  });

  it("seeks when clicking anywhere on the empty timeline", () => {
    const props = setup();
    const lane = screen.getByTestId("timeline-lane");
    fireEvent.pointerDown(lane, { clientX: 300, clientY: 10, pointerId: 1, button: 0 });
    fireEvent.pointerUp(lane, { clientX: 300, clientY: 10, pointerId: 1, button: 0 });
    expect(props.onSeek).toHaveBeenCalledTimes(1);
    const zoom = Number(lane.dataset.pxPerSec);
    expect(props.onSeek.mock.calls[0][0]).toBeCloseTo(300 / zoom, 2);
  });

  it("fits the timeline to the available width on first render", () => {
    setup();
    const lane = screen.getByTestId("timeline-lane");
    const surface = lane.closest("div[style*='width']");
    expect(Number(lane.dataset.pxPerSec)).toBeLessThan(90);
    expect(parseFloat(surface.style.width)).toBeLessThanOrEqual(960);
  });

  it("clicking a line focuses it and seeks without editing its timing", () => {
    const props = setup();
    const block = screen.getByText("segunda línea").closest("div[title]");
    fireEvent.pointerDown(block, { clientX: 1000, pointerId: 1, button: 0 });
    fireEvent.pointerUp(block, { clientX: 1000, pointerId: 1, button: 0 });
    expect(props.onFocus).toHaveBeenCalledWith(1);
    expect(props.onSeek).toHaveBeenCalledTimes(1);
    expect(props.onTimingChange).not.toHaveBeenCalled();
  });

  it("dragging a line commits a horizontal timing change and one undo snapshot", () => {
    const props = setup();
    const block = screen.getByText("segunda línea").closest("div[title]");
    fireEvent.pointerDown(block, { clientX: 900, pointerId: 1, button: 0 });
    fireEvent.pointerMove(block, { clientX: 1080, pointerId: 1 });
    fireEvent.pointerUp(block, { clientX: 1080, pointerId: 1 });
    expect(props.onDragStart).toHaveBeenCalledTimes(1);
    expect(props.onTimingChange).toHaveBeenCalledTimes(1);
    const [id, newStart, newEnd] = props.onTimingChange.mock.calls[0];
    expect(id).toBe(1);
    expect(newStart).toBeGreaterThan(10);
    expect(newEnd).toBeGreaterThan(11);
  });

  it("Cmd/Ctrl-click toggles lines and dragging the group commits one batch", () => {
    const props = setup();
    const first = screen.getByText("primera línea").closest("div[title]");
    const second = screen.getByText("segunda línea").closest("div[title]");

    fireEvent.pointerDown(first, { clientX: 100, pointerId: 1, button: 0, metaKey: true });
    fireEvent.pointerUp(first, { clientX: 100, pointerId: 1, button: 0, metaKey: true });
    fireEvent.pointerDown(second, { clientX: 950, pointerId: 2, button: 0, ctrlKey: true });
    fireEvent.pointerUp(second, { clientX: 950, pointerId: 2, button: 0, ctrlKey: true });

    expect(screen.getByText("2 seleccionadas")).toBeInTheDocument();
    fireEvent.pointerDown(first, { clientX: 100, pointerId: 3, button: 0 });
    fireEvent.pointerMove(first, { clientX: 190, pointerId: 3 });
    fireEvent.pointerUp(first, { clientX: 190, pointerId: 3 });

    expect(props.onTimingChangeBatch).toHaveBeenCalledTimes(1);
    expect(props.onTimingChangeBatch.mock.calls[0][0]).toHaveLength(2);
    expect(props.onTimingChange).not.toHaveBeenCalled();
  });

  it("selects multiple lines by painting a marquee with the mouse", () => {
    setup();
    const lane = screen.getByTestId("timeline-lane");
    fireEvent.pointerDown(lane, { clientX: 0, clientY: 0, pointerId: 1, button: 0 });
    fireEvent.pointerMove(lane, { clientX: 1100, clientY: 80, pointerId: 1 });
    fireEvent.pointerUp(lane, { clientX: 1100, clientY: 80, pointerId: 1 });
    expect(screen.getByText("2 seleccionadas")).toBeInTheDocument();
  });

  it("can start painting from a line with Shift without enabling selection mode", () => {
    setup();
    const block = screen.getByText("segunda línea").closest("div[title]");
    fireEvent.pointerDown(block, { clientX: 900, clientY: 55, pointerId: 1, button: 0, shiftKey: true });
    fireEvent.pointerMove(block, { clientX: 0, clientY: 0, pointerId: 1 });
    fireEvent.pointerUp(block, { clientX: 0, clientY: 0, pointerId: 1 });
    expect(screen.getByText("2 seleccionadas")).toBeInTheDocument();
  });

  it("shows selection instructions and distinct move/resize cursors", () => {
    setup();
    const help = screen.getByTestId("timeline-selection-help");
    const block = screen.getAllByTestId("timeline-segment")[0];
    const edge = block.querySelector('[data-testid="timeline-edge-end"]');
    expect(help).toHaveTextContent("Shift+arrastrar");
    expect(block.className).toContain("cursor-grab");
    expect(edge.className).toContain("cursor-ew-resize");
  });

  it("resizes timing from either horizontal edge", () => {
    const props = setup();
    const block = screen.getAllByTestId("timeline-segment")[1];
    const edge = block.querySelector('[data-testid="timeline-edge-end"]');
    fireEvent.pointerDown(edge, { clientX: 1000, pointerId: 1, button: 0 });
    fireEvent.pointerMove(edge, { clientX: 1080, pointerId: 1 });
    fireEvent.pointerUp(edge, { clientX: 1080, pointerId: 1 });
    expect(props.onTimingChange).toHaveBeenCalledWith(1, 10, expect.any(Number));
    expect(props.onTimingChange.mock.calls[0][2]).toBeGreaterThan(11);
  });

  it("edits line text inline", () => {
    const props = setup();
    fireEvent.doubleClick(screen.getByText("segunda línea"));
    const input = screen.getByDisplayValue("segunda línea");
    fireEvent.change(input, { target: { value: "segunda línea corregida" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(props.onTextChange).toHaveBeenCalledWith(1, "segunda línea corregida");
  });

  it("renders waveform only when waveform data exists", () => {
    const { container, rerender } = render(<LyricsTimeline segments={SEGS} duration={60} currentTime={5} onSeek={vi.fn()} onReset={vi.fn()} />);
    expect(container.querySelector("canvas")).not.toBeInTheDocument();
    rerender(<LyricsTimeline segments={SEGS} duration={60} currentTime={5} waveform={{ peaks: [0.1, 0.9] }} onSeek={vi.fn()} onReset={vi.fn()} />);
    expect(container.querySelector("canvas")).toBeInTheDocument();
  });

  it("shows save status, restore action and zoom controls", () => {
    const props = setup({ saveStatus: "saving" });
    expect(screen.getByText("Guardando…")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /más acciones/i }));
    fireEvent.click(screen.getByText("Restaurar"));
    expect(props.onReset).toHaveBeenCalledTimes(1);
    expect(screen.getByLabelText("Alejar")).toBeInTheDocument();
    expect(screen.getByLabelText("Acercar")).toBeInTheDocument();
  });

  it("auto-follows the playhead once and does not restart a pending smooth scroll", () => {
    const props = {
      segments: SEGS,
      duration: 60,
      currentTime: 5,
      isPlaying: true,
      onSeek: vi.fn(),
      onReset: vi.fn(),
    };
    const { rerender } = render(<LyricsTimeline {...props} />);
    const scroll = screen.getByTestId("timeline-scroll");
    Object.defineProperty(scroll, "clientWidth", { configurable: true, value: 80 });
    scroll.scrollTo = vi.fn();
    rerender(<LyricsTimeline {...props} currentTime={5.1} />);
    rerender(<LyricsTimeline {...props} currentTime={5.2} />);
    expect(scroll.scrollTo).toHaveBeenCalledTimes(1);
  });
});
