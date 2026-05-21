// Component tests for LyricsTimeline — the visual per-line timings editor.
// Covers the key contract: a click focuses+seeks (no edit), a drag commits
// a new timing via onTimingChange (which the parent stamps `locked`), and
// Reset is wired. Full pixel-accurate drag math isn't asserted (jsdom
// getBoundingClientRect is zeroed); we assert the callbacks fire with the
// right segment identity and direction.
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
    onFocus: vi.fn(),
    onReset: vi.fn(),
    ...overrides,
  };
  render(<LyricsTimeline {...props} />);
  return props;
}

it("renders a block per segment with its text", () => {
  setup();
  expect(screen.getByText("primera línea")).toBeInTheDocument();
  expect(screen.getByText("segunda línea")).toBeInTheDocument();
  expect(screen.getByText("tercera línea")).toBeInTheDocument();
});

it("Reset button calls onReset", () => {
  const props = setup();
  fireEvent.click(screen.getByText("Resetear timings"));
  expect(props.onReset).toHaveBeenCalledTimes(1);
});

it("a click (no movement) focuses + seeks to the clicked point, no edit", () => {
  const props = setup();
  const block = screen.getByText("segunda línea").closest("div[title]");
  // Vertical: clientY → time. jsdom rect.top is 0, so time = clientY/pxPerSec.
  fireEvent.pointerDown(block, { clientY: 100, pointerId: 1 });
  fireEvent.pointerUp(block, { clientY: 100, pointerId: 1 });
  expect(props.onFocus).toHaveBeenCalledWith(1);
  expect(props.onSeek).toHaveBeenCalledTimes(1);
  expect(props.onSeek.mock.calls[0][0]).toBeCloseTo(100 / 16, 2); // ZOOM_DEFAULT=16
  expect(props.onTimingChange).not.toHaveBeenCalled();
});

it("dragging commits a new timing via onTimingChange + pushes one undo snapshot", () => {
  const props = setup();
  const block = screen.getByText("segunda línea").closest("div[title]");
  // Vertical drag: drag DOWN (later in time) past the click slop.
  fireEvent.pointerDown(block, { clientY: 100, pointerId: 1 });
  fireEvent.pointerMove(block, { clientY: 160, pointerId: 1 }); // +60px = +1.5s @40px/s
  fireEvent.pointerUp(block, { clientY: 160, pointerId: 1 });
  expect(props.onDragStart).toHaveBeenCalledTimes(1);
  expect(props.onTimingChange).toHaveBeenCalledTimes(1);
  const [id, newStart, newEnd] = props.onTimingChange.mock.calls[0];
  expect(id).toBe(1);
  expect(newStart).toBeGreaterThan(10); // moved later
  expect(newEnd).toBeGreaterThan(11);
  expect(props.onFocus).not.toHaveBeenCalled();
});

it("zoom + makes blocks taller (vertical px/s geometry)", () => {
  setup();
  const block = () => screen.getByText("segunda línea").closest("div[title]");
  const hBefore = parseFloat(block().style.height);
  fireEvent.click(screen.getByLabelText("Acercar"));
  const hAfter = parseFloat(block().style.height);
  expect(hAfter).toBeGreaterThan(hBefore);
});

it("shows the save-status chip", () => {
  cleanup();
  setup({ saveStatus: "saving" });
  expect(screen.getByText("Guardando…")).toBeInTheDocument();
  cleanup();
  setup({ saveStatus: "saved" });
  expect(screen.getByText("Guardado")).toBeInTheDocument();
});

it("renders a waveform canvas when a waveform prop is provided", () => {
  cleanup();
  const { container } = render(
    <LyricsTimeline
      segments={SEGS}
      duration={60}
      currentTime={5}
      activeId={null}
      focusedSegId={null}
      highlightedIds={new Set()}
      waveform={{ peaks: [0.1, 0.9, 0.4, 0.2], duration: 60 }}
      onSeek={vi.fn()}
      onDragStart={vi.fn()}
      onTimingChange={vi.fn()}
      onFocus={vi.fn()}
      onReset={vi.fn()}
    />
  );
  expect(container.querySelector("canvas")).toBeInTheDocument();
});

it("renders without a canvas (graceful) when no waveform is provided", () => {
  cleanup();
  const { container } = render(
    <LyricsTimeline
      segments={SEGS}
      duration={60}
      currentTime={5}
      activeId={null}
      focusedSegId={null}
      highlightedIds={new Set()}
      onSeek={vi.fn()}
      onDragStart={vi.fn()}
      onTimingChange={vi.fn()}
      onFocus={vi.fn()}
      onReset={vi.fn()}
    />
  );
  // No waveform → no canvas, but the timeline (blocks) still renders.
  expect(container.querySelector("canvas")).not.toBeInTheDocument();
  expect(screen.getByText("primera línea")).toBeInTheDocument();
});

it("locked / dragged block does not crash without setPointerCapture (jsdom)", () => {
  // jsdom elements lack setPointerCapture; the component must tolerate it.
  const props = setup({ segments: [{ _id: 0, start: 0, end: 2, text: "x", locked: true }] });
  const block = screen.getByText("x").closest("div[title]");
  expect(() => {
    fireEvent.pointerDown(block, { clientY: 50, pointerId: 1 });
    fireEvent.pointerUp(block, { clientY: 50, pointerId: 1 });
  }).not.toThrow();
  expect(props.onFocus).toHaveBeenCalledWith(0);
});
