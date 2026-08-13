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

// REGRESSION GUARD 2026-05-24: between two back-to-back lines the preview
// used to drop to `null` for the gap frame, ripping the placeholder in and
// out. With sticky-last-line the previous line holds for up to TAIL_HOLD_S
// seconds — placeholder stays away during normal verse pacing.
it("holds the previous line during a sub-second gap (no placeholder flicker)", () => {
  // segments: 0-5 "primera línea", 5.5-10 "segunda línea" — 0.5 s gap.
  const segs = [
    { _id: 0, start: 0, end: 5, text: "primera línea" },
    { _id: 1, start: 5.5, end: 10, text: "segunda línea" },
  ];
  setup({ segments: segs, currentTime: 5.2 });   // inside the gap
  // previous line is still on screen, NOT the placeholder
  expect(screen.getByText("primera línea")).toBeInTheDocument();
  expect(screen.queryByText(/Reproducí/)).not.toBeInTheDocument();
});

it("drops to the placeholder after the tail-hold window (real instrumental)", () => {
  // Only one line at 0-2. At t=4 we're 2 s past the end → past TAIL_HOLD_S.
  const segs = [{ _id: 0, start: 0, end: 2, text: "única" }];
  setup({ segments: segs, currentTime: 4 });
  expect(screen.queryByText("única")).not.toBeInTheDocument();
  expect(screen.getByText(/Reproducí|Play|Toque/)).toBeInTheDocument();
});

it("stacks multiple overlapping lines (chorus repeat case)", () => {
  // INCIDENT 2026-05-25: chorus with repeated lines like
  // "Legalícenla / Oh-oh-oh" at same start time used to show only the
  // last one. Now both are rendered, joined by a newline, so the
  // operator sees the full overlap.
  const segs = [
    { _id: 0, start: 5,   end: 7, text: "Legalícenla" },
    { _id: 1, start: 5.1, end: 7, text: "Oh-oh-oh" },
  ];
  setup({ segments: segs, currentTime: 6 });
  // Both texts are rendered in the same node (joined by \n via whitespace:pre-line)
  const el = screen.getByText(/Legalícenla[\s\S]*Oh-oh-oh/);
  expect(el).toBeInTheDocument();
});

it("does not show any line before the first segment starts (intro)", () => {
  // currentTime before any segment → lastStarted is null → placeholder.
  const segs = [
    { _id: 0, start: 2, end: 5, text: "primera línea" },
    { _id: 1, start: 5, end: 10, text: "segunda línea" },
  ];
  setup({ segments: segs, currentTime: 0.5 });   // genuine intro
  expect(screen.queryByText("primera línea")).not.toBeInTheDocument();
  expect(screen.queryByText("segunda línea")).not.toBeInTheDocument();
  expect(screen.getByText(/Reproducí|Play|Toque/)).toBeInTheDocument();
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

it("sizes the line by length tier like the render: long line → smaller font than short", () => {
  const shortLine = "corta";                                   // ≤50 → 85px tier
  const longLine = "x".repeat(95);                              // >80 → 55px tier
  setup({ segments: [{ _id: 0, start: 0, end: 5, text: shortLine }], currentTime: 2 });
  const shortFs = parseFloat(screen.getByText(shortLine).style.fontSize); // cqw
  cleanup();
  setup({ segments: [{ _id: 0, start: 0, end: 5, text: longLine }], currentTime: 2 });
  const longFs = parseFloat(screen.getByText(longLine).style.fontSize);
  expect(longFs).toBeLessThan(shortFs);
  expect(shortFs).toBeCloseTo((85 / 1920) * 100, 2);
  expect(longFs).toBeCloseTo((55 / 1920) * 100, 2);
});

it("fontScale multiplies the tier size and is clamped to the backend max (1.5)", () => {
  const seg = [{ _id: 0, start: 0, end: 5, text: "corta" }]; // ≤50 → 85px tier
  setup({ segments: seg, currentTime: 2, fontScale: 1 });
  const base = parseFloat(screen.getByText("corta").style.fontSize);
  cleanup();
  setup({ segments: seg, currentTime: 2, fontScale: 1.5 });
  const at15 = parseFloat(screen.getByText("corta").style.fontSize);
  cleanup();
  setup({ segments: seg, currentTime: 2, fontScale: 2.0 }); // over the clamp
  const at20 = parseFloat(screen.getByText("corta").style.fontSize);
  expect(base).toBeCloseTo((85 / 1920) * 100, 3);
  // 85 × 1.5 = 127.5 → the render rounds to an INTEGER px with Python
  // banker's rounding (128), and since the parity refactor the preview
  // shows that exact px — not the continuous 127.5 the old formula used.
  expect(at15).toBeCloseTo((128 / 1920) * 100, 3);
  expect(at20).toBeCloseTo(at15, 3); // clamped to 1.5, not 2.0
});

it("editable=false hides the layout handles but still renders the line", () => {
  const { container } = setup({ editable: false });
  expect(screen.getByText("segunda línea")).toBeInTheDocument();
  // No resize/rotate handles, no readout chip.
  expect(container.querySelector("[title='Escalar']")).not.toBeInTheDocument();
  expect(container.querySelector("[title='Rotar']")).not.toBeInTheDocument();
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
  expect(container.querySelector("video")).toHaveAttribute("loop");
});
