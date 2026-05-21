// Tests for the editable live preview. jsdom returns zeroed rects, so we
// assert callback identity + shape (which line, did it commit a layout) rather
// than pixel-accurate pos/scale/rot math.
import { render, screen, cleanup, fireEvent } from "@testing-library/react";
import { afterEach, it, expect, vi } from "vitest";
import LyricVideoPreview from "./LyricVideoPreview";

afterEach(cleanup);

const SEGS = [
  { _id: 0, start: 0, end: 5, text: "primera línea" },
  { _id: 1, start: 5, end: 10, text: "segunda línea" },
];

function setup(overrides = {}) {
  const props = {
    segments: SEGS,
    currentTime: 6,           // inside seg _id:1
    backgroundUrl: null,
    backgroundStyle: "calido",
    textCase: "original",     // keep text literal so queries match (default is "upper")
    onSelect: vi.fn(),
    onLayoutChange: vi.fn(),
    onDragStart: vi.fn(),
    ...overrides,
  };
  const utils = render(<LyricVideoPreview {...props} />);
  return { props, ...utils };
}

it("renders the line active at currentTime", () => {
  setup();
  expect(screen.getByText("segunda línea")).toBeInTheDocument();
  expect(screen.queryByText("primera línea")).not.toBeInTheDocument();
});

it("a click (no movement) selects the line, does not commit a layout", () => {
  const { props } = setup();
  const text = screen.getByText("segunda línea").closest("div[class*='cursor-move']");
  fireEvent.pointerDown(text, { clientX: 100, clientY: 100, pointerId: 1 });
  fireEvent.pointerUp(text, { clientX: 100, clientY: 100, pointerId: 1 });
  expect(props.onSelect).toHaveBeenCalledWith(1);
  expect(props.onLayoutChange).not.toHaveBeenCalled();
});

it("dragging the body commits a layout change for that line + one undo snapshot", () => {
  const { props } = setup();
  const text = screen.getByText("segunda línea").closest("div[class*='cursor-move']");
  fireEvent.pointerDown(text, { clientX: 100, clientY: 100, pointerId: 1 });
  fireEvent.pointerMove(text, { clientX: 160, clientY: 130, pointerId: 1 });
  fireEvent.pointerUp(text, { clientX: 160, clientY: 130, pointerId: 1 });
  expect(props.onDragStart).toHaveBeenCalledTimes(1);
  expect(props.onLayoutChange).toHaveBeenCalledTimes(1);
  const [id, layout] = props.onLayoutChange.mock.calls[0];
  expect(id).toBe(1);
  expect(layout).toHaveProperty("pos");
  expect(layout).toHaveProperty("scale");
  expect(layout).toHaveProperty("rot");
  expect(props.onSelect).not.toHaveBeenCalled();
});

it("uses an existing layout override (rot) on the active line", () => {
  setup({
    segments: [{ _id: 0, start: 0, end: 5, text: "torcida", rot: -8, scale: 1.2 }],
    currentTime: 2,
  });
  const wrap = screen.getByText("torcida").closest("div[class*='cursor-move']");
  expect(wrap.style.transform).toContain("rotate(-8deg)");
});

it("with fade, the active line ramps opacity at its start; full opacity mid-line", () => {
  // seg _id:1 is 5..10. fade dur = 0.15s. Just after start → partial opacity.
  setup({ transition: "fade", currentTime: 5.05 });
  const fadingIn = parseFloat(screen.getByText("segunda línea").style.opacity);
  expect(fadingIn).toBeGreaterThan(0);
  expect(fadingIn).toBeLessThan(1);
  cleanup();
  // Mid-line → full opacity.
  setup({ transition: "fade", currentTime: 7 });
  expect(parseFloat(screen.getByText("segunda línea").style.opacity)).toBe(1);
});

it("with cut, the active line is always full opacity (no fade)", () => {
  setup({ transition: "cut", currentTime: 5.02 });
  expect(parseFloat(screen.getByText("segunda línea").style.opacity)).toBe(1);
});

it("renders a <video> when backgroundUrl is set, gradient otherwise", () => {
  const { container, rerender } = setup({ backgroundUrl: null });
  expect(container.querySelector("video")).not.toBeInTheDocument();
  rerender(
    <LyricVideoPreview
      segments={SEGS} currentTime={6} backgroundUrl="https://stub/bg.mp4"
      onSelect={vi.fn()} onLayoutChange={vi.fn()} onDragStart={vi.fn()}
    />
  );
  expect(container.querySelector("video")).toBeInTheDocument();
});
