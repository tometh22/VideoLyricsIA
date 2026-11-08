import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import LyricsTimeline from "./LyricsTimeline";

afterEach(() => { cleanup(); vi.restoreAllMocks(); });
const rows = [
  { _id: 1, segment_id: "one", start: 1, end: 2, text: "first" },
  { _id: 2, segment_id: "two", start: 4, end: 5, text: "second" },
];
function setup() {
  const props = { duration: 20, currentTime: 0, onDragStart: vi.fn(), onTimingChange: vi.fn(), onTimingChangeBatch: vi.fn() };
  const view = render(<LyricsTimeline {...props} segments={rows} />);
  return { props, rerender: segments => view.rerender(<LyricsTimeline {...props} segments={segments} />) };
}
const begin = (target, delta = 48) => {
  target.hasPointerCapture = vi.fn(() => true);
  target.releasePointerCapture = vi.fn();
  fireEvent.pointerDown(target, { clientX: 100, pointerId: 1, button: 0 });
  fireEvent.pointerMove(target, { clientX: 100 + delta, pointerId: 1 });
};
const release = target => fireEvent.pointerUp(target, { clientX: 148, pointerId: 1 });
const noCommit = props => {
  expect(props.onDragStart).not.toHaveBeenCalled();
  expect(props.onTimingChange).not.toHaveBeenCalled();
  expect(props.onTimingChangeBatch).not.toHaveBeenCalled();
};

describe("timeline remote-update contracts audit 5", () => {
  it.each(["timeline-segment-body", "timeline-edge-end"])("cancels %s when source timing changes during the gesture", testId => {
    const { props, rerender } = setup();
    const target = screen.getAllByTestId(testId)[0];
    begin(target);
    rerender(rows.map(row => row._id === 1 ? { ...row, start: 10, end: 11 } : row));
    release(target);
    noCommit(props);
    expect(target.releasePointerCapture).toHaveBeenCalledWith(1);
  });

  it("cancels a group move if one selected member receives new timing", () => {
    const { props, rerender } = setup();
    for (const segment of screen.getAllByTestId("timeline-segment")) fireEvent.keyDown(segment, { key: "Enter" });
    const target = screen.getAllByTestId("timeline-segment-body")[0];
    begin(target);
    rerender(rows.map(row => row._id === 2 ? { ...row, start: 8, end: 9 } : row));
    release(target);
    noCommit(props);
  });

  it("cancels a group move when a selected identity disappears", () => {
    const { props, rerender } = setup();
    for (const segment of screen.getAllByTestId("timeline-segment")) fireEvent.keyDown(segment, { key: "Enter" });
    const target = screen.getAllByTestId("timeline-segment-body")[0];
    begin(target);
    rerender(rows.slice(0, 1));
    release(target);
    noCommit(props);
  });

  it("cancels when a durable identity is replaced without changing its local id", () => {
    const { props, rerender } = setup();
    const target = screen.getAllByTestId("timeline-segment-body")[0];
    begin(target);
    rerender(rows.map(row => row._id === 1 ? { ...row, segment_id: "replacement" } : row));
    release(target);
    noCommit(props);
  });

  it("cancels when a neighbour changes the collision bounds", () => {
    const { props, rerender } = setup();
    const target = screen.getAllByTestId("timeline-segment-body")[0];
    begin(target);
    rerender(rows.map(row => row._id === 2 ? { ...row, start: 2.5, end: 3.5 } : row));
    release(target);
    noCommit(props);
  });

  it("keeps an active move through unrelated text and metadata updates", () => {
    const { props, rerender } = setup();
    const target = screen.getAllByTestId("timeline-segment-body")[0];
    begin(target);
    rerender(rows.map(row => row._id === 2 ? { ...row, text: "remote text", review: true } : row));
    release(target);
    expect(props.onTimingChange).toHaveBeenCalledWith(1, 2, 3, expect.objectContaining({ operation: "move" }));
  });
});
