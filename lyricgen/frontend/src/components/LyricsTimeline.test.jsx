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

it("a click (no movement) focuses + seeks, does NOT edit timing", () => {
  const props = setup();
  const block = screen.getByText("segunda línea").closest("div[title]");
  fireEvent.pointerDown(block, { clientX: 100, pointerId: 1 });
  fireEvent.pointerUp(block, { clientX: 100, pointerId: 1 });
  expect(props.onFocus).toHaveBeenCalledWith(1);
  expect(props.onSeek).toHaveBeenCalledWith(10); // segment start
  expect(props.onTimingChange).not.toHaveBeenCalled();
});

it("dragging commits a new timing via onTimingChange + pushes one undo snapshot", () => {
  const props = setup();
  const block = screen.getByText("segunda línea").closest("div[title]");
  // Drag the block body well past the click slop (delta >> 4px).
  fireEvent.pointerDown(block, { clientX: 100, pointerId: 1 });
  fireEvent.pointerMove(block, { clientX: 160, pointerId: 1 }); // +60px = +2s @30px/s
  fireEvent.pointerUp(block, { clientX: 160, pointerId: 1 });
  expect(props.onDragStart).toHaveBeenCalledTimes(1);
  expect(props.onTimingChange).toHaveBeenCalledTimes(1);
  const [id, newStart, newEnd] = props.onTimingChange.mock.calls[0];
  expect(id).toBe(1);
  expect(newStart).toBeGreaterThan(10); // moved later
  expect(newEnd).toBeGreaterThan(11);
  // click handlers must NOT also fire on a real drag
  expect(props.onFocus).not.toHaveBeenCalled();
});

it("locked / dragged block does not crash without setPointerCapture (jsdom)", () => {
  // jsdom elements lack setPointerCapture; the component must tolerate it.
  const props = setup({ segments: [{ _id: 0, start: 0, end: 2, text: "x", locked: true }] });
  const block = screen.getByText("x").closest("div[title]");
  expect(() => {
    fireEvent.pointerDown(block, { clientX: 50, pointerId: 1 });
    fireEvent.pointerUp(block, { clientX: 50, pointerId: 1 });
  }).not.toThrow();
  expect(props.onFocus).toHaveBeenCalledWith(0);
});
