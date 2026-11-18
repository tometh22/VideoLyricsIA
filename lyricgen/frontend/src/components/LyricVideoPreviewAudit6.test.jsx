import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import LyricVideoPreview from "./LyricVideoPreview";

afterEach(cleanup);
const segments = [
  { _id: "a", start: 0, end: 5, text: "Primera frase" },
  { _id: "b", start: 5, end: 10, text: "Segunda frase" },
];

it.each(["move", "resize", "rotate"])("cancels %s rather than applying the previous phrase layout to the next playing phrase", (mode) => {
  const props = { segments, currentTime: 4.8, isPlaying: true, textCase: "original", onLayoutChange: vi.fn(), onSelect: vi.fn(), onDragStart: vi.fn() };
  const { rerender } = render(<LyricVideoPreview {...props} />);
  const handle = () => mode === "move"
    ? screen.getByText(/frase/).closest("div[class*='cursor-move']")
    : screen.getByTitle(mode === "resize" ? "Escalar" : "Rotar");
  fireEvent.pointerDown(handle(), { pointerId: 1, button: 0, clientX: 100, clientY: 100 });
  fireEvent.pointerMove(handle(), { pointerId: 1, clientX: 160, clientY: 130 });
  rerender(<LyricVideoPreview {...props} currentTime={5.2} />);
  expect(screen.getByText("Segunda frase")).toBeInTheDocument();
  fireEvent.pointerUp(handle(), { pointerId: 1, clientX: 160, clientY: 130 });
  expect(props.onLayoutChange).not.toHaveBeenCalled();
  expect(props.onSelect).not.toHaveBeenCalled();
});

it("preserves a valid gesture when only text and unrelated metadata change", () => {
  const props = { segments, currentTime: 2, textCase: "original", onLayoutChange: vi.fn(), onDragStart: vi.fn() };
  const { rerender } = render(<LyricVideoPreview {...props} />);
  const target = screen.getByText("Primera frase").closest("div[class*='cursor-move']");
  fireEvent.pointerDown(target, { pointerId: 1, button: 0, clientX: 100, clientY: 100 });
  fireEvent.pointerMove(target, { pointerId: 1, clientX: 160, clientY: 130 });
  rerender(<LyricVideoPreview {...props} segments={segments.map(s => s._id === "a" ? { ...s, text: "Letra corregida", review: true } : s)} />);
  fireEvent.pointerUp(target, { pointerId: 1, clientX: 160, clientY: 130 });
  expect(props.onLayoutChange).toHaveBeenCalledTimes(1);
  expect(props.onLayoutChange.mock.calls[0][0]).toBe("a");
});

it.each(["escape", "pointercancel", "lostcapture"])("cancels preview layout on %s and releases capture", (mode) => {
  const props = { segments, currentTime: 2, textCase: "original", onLayoutChange: vi.fn(), onDragStart: vi.fn() };
  render(<LyricVideoPreview {...props} />);
  const target = screen.getByText("Primera frase").closest("div[class*='cursor-move']");
  target.setPointerCapture = vi.fn();
  target.hasPointerCapture = vi.fn(() => true);
  target.releasePointerCapture = vi.fn();
  fireEvent.pointerDown(target, { pointerId: 1, button: 0, clientX: 100, clientY: 100 });
  fireEvent.pointerMove(target, { pointerId: 1, clientX: 160, clientY: 130 });
  if (mode === "escape") fireEvent.keyDown(window, { key: "Escape" });
  else if (mode === "pointercancel") fireEvent.pointerCancel(target, { pointerId: 1 });
  else fireEvent.lostPointerCapture(target, { pointerId: 1 });
  fireEvent.pointerUp(target, { pointerId: 1, clientX: 160, clientY: 130 });
  expect(target.releasePointerCapture).toHaveBeenCalledWith(1);
  expect(props.onLayoutChange).not.toHaveBeenCalled();
});
