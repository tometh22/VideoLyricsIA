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
    onTimingChangeBatch: vi.fn(),
    onTextChange: vi.fn(),
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

it("offers fit/detail presets and an accessible song minimap", () => {
  setup();
  expect(screen.getByText("32 px/s")).toBeInTheDocument();
  fireEvent.click(screen.getByText("Ajustar"));
  expect(screen.getByText("8 px/s")).toBeInTheDocument();
  fireEvent.click(screen.getByText("Detalle"));
  expect(screen.getByText("32 px/s")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /Mini-mapa de la canción/i })).toBeInTheDocument();
});

it("a click (no movement) focuses + seeks to the clicked point, no edit", () => {
  const props = setup();
  const block = screen.getByText("segunda línea").closest("div[title]");
  // Vertical: clientY → time. jsdom rect.top is 0, so time = clientY/pxPerSec.
  fireEvent.pointerDown(block, { clientY: 100, pointerId: 1 });
  fireEvent.pointerUp(block, { clientY: 100, pointerId: 1 });
  expect(props.onFocus).toHaveBeenCalledWith(1);
  expect(props.onSeek).toHaveBeenCalledTimes(1);
  expect(props.onSeek.mock.calls[0][0]).toBeCloseTo(100 / 32, 2); // ZOOM_DEFAULT=32
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

it("double-click on a line's text edits it inline and commits via onTextChange (no view switch)", () => {
  const props = setup();
  const span = screen.getByText("segunda línea");
  fireEvent.doubleClick(span);
  const input = screen.getByDisplayValue("segunda línea");
  fireEvent.change(input, { target: { value: "segunda línea corregida" } });
  fireEvent.keyDown(input, { key: "Enter" });
  expect(props.onTextChange).toHaveBeenCalledWith(1, "segunda línea corregida");
});

it("Escape cancels an inline text edit without committing", () => {
  const props = setup();
  fireEvent.doubleClick(screen.getByText("segunda línea"));
  const input = screen.getByDisplayValue("segunda línea");
  fireEvent.change(input, { target: { value: "descartar" } });
  fireEvent.keyDown(input, { key: "Escape" });
  expect(props.onTextChange).not.toHaveBeenCalled();
  expect(screen.getByText("segunda línea")).toBeInTheDocument();
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

it("selects multiple lines with Shift-click and commits one group move", () => {
  const props = setup();
  const first = screen.getByText("primera línea").closest("div[title]");
  const second = screen.getByText("segunda línea").closest("div[title]");

  fireEvent.pointerDown(first, { clientY: 20, pointerId: 1, shiftKey: true });
  fireEvent.pointerUp(first, { clientY: 20, pointerId: 1, shiftKey: true });
  fireEvent.pointerDown(second, { clientY: 320, pointerId: 2, shiftKey: true });
  fireEvent.pointerUp(second, { clientY: 320, pointerId: 2, shiftKey: true });

  expect(screen.getByText("2 seleccionadas")).toBeInTheDocument();
  fireEvent.pointerDown(first, { clientY: 20, pointerId: 3 });
  fireEvent.pointerMove(first, { clientY: 52, pointerId: 3 });
  fireEvent.pointerUp(first, { clientY: 52, pointerId: 3 });

  expect(props.onDragStart).toHaveBeenCalledTimes(1);
  expect(props.onTimingChangeBatch).toHaveBeenCalledTimes(1);
  const [changes] = props.onTimingChangeBatch.mock.calls[0];
  expect(changes).toHaveLength(2);
  expect(changes[0].start).toBeGreaterThan(0);
  expect(changes[1].start).toBeGreaterThan(10);
  expect(props.onTimingChange).not.toHaveBeenCalled();
});

it("paints a selection over the timeline when selection mode is enabled", () => {
  setup();
  fireEvent.click(screen.getByRole("button", { name: "Seleccionar" }));
  const lane = screen.getByTestId("timeline-lane");
  fireEvent.pointerDown(lane, { clientY: 0, pointerId: 1, button: 0 });
  fireEvent.pointerMove(lane, { clientY: 370, pointerId: 1 });
  fireEvent.pointerUp(lane, { clientY: 370, pointerId: 1 });
  expect(screen.getByText("2 seleccionadas")).toBeInTheDocument();
});

it("does not restart smooth auto-follow while the previous scroll is pending", () => {
  const props = {
    segments: SEGS,
    duration: 60,
    currentTime: 5,
    isPlaying: true,
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
  };
  const { rerender } = render(<LyricsTimeline {...props} />);
  const scroll = screen.getByTestId("timeline-lane").parentElement;
  Object.defineProperty(scroll, "clientHeight", { configurable: true, value: 80 });
  Object.defineProperty(scroll, "scrollTop", { configurable: true, writable: true, value: 0 });
  scroll.scrollTo = vi.fn();
  rerender(<LyricsTimeline {...props} currentTime={5.1} />);
  rerender(<LyricsTimeline {...props} currentTime={5.2} />);
  expect(scroll.scrollTo).toHaveBeenCalledTimes(1);
});

it("allows auto-follow again after the smooth-scroll fallback expires", () => {
  vi.useFakeTimers();
  try {
    const props = {
      segments: SEGS,
      duration: 60,
      currentTime: 5,
      isPlaying: true,
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
    };
    const { rerender } = render(<LyricsTimeline {...props} />);
    const scroll = screen.getByTestId("timeline-lane").parentElement;
    Object.defineProperty(scroll, "clientHeight", { configurable: true, value: 80 });
    Object.defineProperty(scroll, "scrollTop", { configurable: true, writable: true, value: 0 });
    scroll.scrollTo = vi.fn();
    rerender(<LyricsTimeline {...props} currentTime={5.1} />);
    expect(scroll.scrollTo).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(750);
    rerender(<LyricsTimeline {...props} currentTime={5.2} />);
    expect(scroll.scrollTo).toHaveBeenCalledTimes(2);
  } finally {
    vi.useRealTimers();
  }
});

// ---------------------------------------------------------------------------
// Multi-lane drag — operator report 2026-05-26
//
// WhisperX devuelve a veces 2+ líneas que se solapan en el tiempo (un coro
// con overdubs, o frases superpuestas). El Gantt las renderiza en lanes
// paralelas. Cuando el operador arrastra el bottom edge de UNA de ellas
// para extender el timing a cubrir toda la voz, el `neighbours()` pre-fix
// usaba el orden global por `.start` y devolvía como `next` al segmento de
// la lane PARALELA — la clamp `hi = next.start - gapS` bloqueaba la
// extensión a unos pocos milisegundos, indistinguible visualmente de un
// "snap back" inmediato.
//
// Fix: `neighbours()` filtra por mismo lane antes de buscar prev/next. Las
// líneas en lanes paralelas dejan de bloquear la extensión.
// ---------------------------------------------------------------------------

// Multi-lane fixtures: A en lane 0 (0-2s), B en lane 1 overlap (1-5s),
// C en lane 0 lejos (20-21s). Por el algoritmo de lane assignment:
//   sort by start → [A, B, C]
//   A → lane 0 (laneEnds=[2])
//   B starts at 1, laneEnds[0]=2 > 1 → lane 1 (laneEnds=[2, 5])
//   C starts at 20, laneEnds[0]=2 ≤ 20 → lane 0 (laneEnds=[21, 5])
const MULTI_LANE_SEGS = [
  { _id: 0, start: 0,  end: 2,  text: "A lane 0" },
  { _id: 1, start: 1,  end: 5,  text: "B lane 1 (overlap)" },
  { _id: 2, start: 20, end: 21, text: "C lane 0 (far)" },
];

it("end-edge drag can extend past an overlapping segment in another lane", () => {
  // PRE-FIX: this asserted-greater-than would fail because the clamp,
  // using B (lane 1) as the next neighbour, would force end ≈ 0.95 —
  // a value LESS than origEnd (2). The commit would actually SHRINK the
  // segment, which the operator perceives as "snap back to original".
  //
  // POST-FIX: lane-aware neighbours skips B (different lane) and uses
  // C (same lane, far away). The end extends freely up to C - gapS.
  const props = setup({ segments: MULTI_LANE_SEGS });
  const aBlock = screen.getByText("A lane 0").closest("div[title]");
  const bottomEdge = aBlock.querySelector('[title="Arrastrá: cuándo SALE la línea"]');
  expect(bottomEdge).not.toBeNull();
  // Drag the bottom edge DOWN by 80px = +2.5s @ pxPerSec=32.
  fireEvent.pointerDown(bottomEdge, { clientY: 20, pointerId: 1 });
  fireEvent.pointerMove(bottomEdge, { clientY: 100, pointerId: 1 });
  fireEvent.pointerUp(bottomEdge, { clientY: 100, pointerId: 1 });
  expect(props.onTimingChange).toHaveBeenCalledTimes(1);
  const [id, newStart, newEnd] = props.onTimingChange.mock.calls[0];
  expect(id).toBe(0);
  expect(newStart).toBeCloseTo(0, 3); // start unchanged
  // Bug repro: pre-fix newEnd ≈ 0.95 (< origEnd 2). Post-fix newEnd ≈ 7.
  expect(newEnd).toBeGreaterThan(2);
  expect(newEnd).toBeLessThan(20); // still clamped by C in same lane
});

it("start-edge drag can move earlier past an overlapping segment in another lane", () => {
  // Mismo bug en la otra punta. C en lane 0 quiere moverse hacia el
  // segundo 5 — pero el sorted `prev` es B (lane 1) que ends en 5, por
  // lo que `lo = B.end + gapS = 5.05` parecía ser el piso, cuando en
  // realidad la lane 0 está libre entre 2 (A.end) y 20 (C.start).
  const props = setup({ segments: MULTI_LANE_SEGS });
  const cBlock = screen.getByText("C lane 0 (far)").closest("div[title]");
  const topEdge = cBlock.querySelector('[title="Arrastrá: cuándo ENTRA la línea"]');
  expect(topEdge).not.toBeNull();
  // C en y=20*32=640. Arrastrar UP 480 px = -15s @ pxPerSec=32.
  // Target start: 20 - 15 = 5.
  fireEvent.pointerDown(topEdge, { clientY: 640, pointerId: 1 });
  fireEvent.pointerMove(topEdge, { clientY: 160, pointerId: 1 });
  fireEvent.pointerUp(topEdge, { clientY: 160, pointerId: 1 });
  expect(props.onTimingChange).toHaveBeenCalledTimes(1);
  const [id, newStart, newEnd] = props.onTimingChange.mock.calls[0];
  expect(id).toBe(2);
  // Bug repro: pre-fix newStart ≈ 5.05 (clamped by B.end). Post-fix
  // newStart should reach 5 (free space in lane 0 from A.end=2 onwards).
  expect(newStart).toBeLessThan(5.05);
  expect(newStart).toBeGreaterThan(2); // still clamped by A in same lane
  expect(newEnd).toBeCloseTo(21, 3); // end unchanged for start-mode drag
});

it("regression: single-lane back-to-back lines still clamp at next.start - gapS", () => {
  // Cuando NO hay overlap, el comportamiento es idéntico al pre-fix: el
  // operador no puede empujar un end hacia donde arranca la próxima línea
  // del MISMO lane. Esto evita que dos líneas secuenciales se solapen
  // accidentalmente en el render. Aseguramos que el fix no rompió esta
  // semántica.
  const SINGLE_LANE = [
    { _id: 0, start: 0, end: 2, text: "primera" },
    { _id: 1, start: 5, end: 6, text: "segunda" },
  ];
  const props = setup({ segments: SINGLE_LANE });
  const firstBlock = screen.getByText("primera").closest("div[title]");
  const bottomEdge = firstBlock.querySelector('[title="Arrastrá: cuándo SALE la línea"]');
  fireEvent.pointerDown(bottomEdge, { clientY: 20, pointerId: 1 });
  // Drag DOWN 200 px = +12.5 s. Sin clamp llegaría a 14.5 s; clampeado por
  // segunda.start - gapS = 5 - 0.05 = 4.95 s.
  fireEvent.pointerMove(bottomEdge, { clientY: 220, pointerId: 1 });
  fireEvent.pointerUp(bottomEdge, { clientY: 220, pointerId: 1 });
  expect(props.onTimingChange).toHaveBeenCalledTimes(1);
  const [, , newEnd] = props.onTimingChange.mock.calls[0];
  expect(newEnd).toBeGreaterThan(2);
  expect(newEnd).toBeCloseTo(4.95, 1); // still clamped at next.start - gapS
});
