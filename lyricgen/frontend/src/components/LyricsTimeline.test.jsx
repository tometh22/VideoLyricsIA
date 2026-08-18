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
    onDeleteSelection: vi.fn(() => true),
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
    expect(screen.getAllByText("primera línea").length).toBeGreaterThan(0);
    expect(screen.getAllByText("segunda línea").length).toBeGreaterThan(0);
    expect(screen.getAllByText("tercera línea").length).toBeGreaterThan(0);
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
    expect(parseFloat(block.style.width)).toBeCloseTo(2.6 * zoom, 5);
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

  it("opens at an editing zoom instead of compressing the full song", () => {
    setup();
    const lane = screen.getByTestId("timeline-lane");
    expect(Number(lane.dataset.pxPerSec)).toBe(48);
    expect(screen.getByRole("button", { name: "Ver canción completa" })).toBeInTheDocument();
  });

  it("delegates vertical scrolling to the editor and keeps horizontal timeline scroll", () => {
    setup();
    const scroll = screen.getByTestId("timeline-scroll");
    expect(scroll).toHaveAttribute("data-scroll-owner", "horizontal-only");
    expect(scroll.className).toContain("overflow-x-auto");
    expect(scroll.className).toContain("overflow-y-hidden");
    expect(scroll.style.maxHeight).toBe("");
  });

  it("clicking a line focuses it and seeks without editing its timing", () => {
    const props = setup();
    const block = screen.getAllByTestId("timeline-segment")[1];
    const body = block.querySelector('[data-testid="timeline-segment-body"]');
    fireEvent.pointerDown(body, { clientX: 1000, pointerId: 1, button: 0 });
    fireEvent.pointerUp(body, { clientX: 1000, pointerId: 1, button: 0 });
    expect(props.onFocus).toHaveBeenCalledWith(1);
    expect(props.onSeek).toHaveBeenCalledTimes(1);
    expect(props.onTimingChange).not.toHaveBeenCalled();
  });

  it("dragging a line commits a horizontal timing change and one undo snapshot", () => {
    const props = setup();
    const block = screen.getAllByTestId("timeline-segment")[1];
    const body = block.querySelector('[data-testid="timeline-segment-body"]');
    fireEvent.pointerDown(body, { clientX: 900, pointerId: 1, button: 0 });
    fireEvent.pointerMove(body, { clientX: 1080, pointerId: 1 });
    fireEvent.pointerUp(body, { clientX: 1080, pointerId: 1 });
    expect(props.onDragStart).toHaveBeenCalledTimes(1);
    expect(props.onTimingChange).toHaveBeenCalledTimes(1);
    const [id, newStart, newEnd] = props.onTimingChange.mock.calls[0];
    expect(id).toBe(1);
    expect(newStart).toBeGreaterThan(10);
    expect(newEnd).toBeGreaterThan(11);
  });

  it("does not modify neighbouring lines when a packed line is dragged in safe mode", () => {
    const props = setup({
      segments: [
        { _id: "a", start: 0, end: 1, text: "primera" },
        { _id: "b", start: 1.05, end: 2, text: "segunda" },
        { _id: "c", start: 2.05, end: 3, text: "tercera" },
      ],
      duration: 10,
    });
    const block = screen.getAllByTestId("timeline-segment")[1];
    const body = block.querySelector('[data-testid="timeline-segment-body"]');
    fireEvent.pointerDown(body, { clientX: 100, pointerId: 1, button: 0 });
    fireEvent.pointerMove(body, { clientX: 124, pointerId: 1 });
    fireEvent.pointerUp(body, { clientX: 124, pointerId: 1 });

    expect(props.onTimingChange).not.toHaveBeenCalled();
    expect(props.onTimingChangeBatch).not.toHaveBeenCalled();
    expect(screen.getByTestId("timeline-limit-feedback")).toHaveTextContent("No hay espacio para mover sólo esta línea");
  });

  it("moves packed neighbours only after the operator explicitly enables chain mode", () => {
    const props = setup({
      segments: [
        { _id: "a", start: 0, end: 1, text: "primera" },
        { _id: "b", start: 1.05, end: 2, text: "segunda" },
        { _id: "c", start: 2.05, end: 3, text: "tercera" },
      ],
      duration: 10,
    });
    fireEvent.click(screen.getByRole("button", { name: "Más acciones" }));
    fireEvent.click(screen.getByRole("menuitemcheckbox", { name: /Mover en cadena/ }));

    const block = screen.getAllByTestId("timeline-segment")[1];
    const body = block.querySelector('[data-testid="timeline-segment-body"]');
    fireEvent.pointerDown(body, { clientX: 100, pointerId: 1, button: 0 });
    fireEvent.pointerMove(body, { clientX: 124, pointerId: 1 });
    fireEvent.pointerUp(body, { clientX: 124, pointerId: 1 });

    expect(props.onTimingChange).not.toHaveBeenCalled();
    expect(props.onTimingChangeBatch).toHaveBeenCalledWith([
      { id: "b", start: 1.55, end: 2.5 },
      { id: "c", start: 2.55, end: 3.5 },
    ], expect.objectContaining({ operation: "move" }));
  });

  it("Cmd/Ctrl-click toggles lines and dragging the group commits one batch", () => {
    const props = setup();
    const [first, second] = screen.getAllByTestId("timeline-segment");
    const firstBody = first.querySelector('[data-testid="timeline-segment-body"]');
    const secondBody = second.querySelector('[data-testid="timeline-segment-body"]');

    fireEvent.pointerDown(firstBody, { clientX: 100, pointerId: 1, button: 0, metaKey: true });
    fireEvent.pointerUp(firstBody, { clientX: 100, pointerId: 1, button: 0, metaKey: true });
    fireEvent.pointerDown(secondBody, { clientX: 950, pointerId: 2, button: 0, ctrlKey: true });
    fireEvent.pointerUp(secondBody, { clientX: 950, pointerId: 2, button: 0, ctrlKey: true });

    expect(screen.getByText("2 líneas")).toBeInTheDocument();
    fireEvent.pointerDown(firstBody, { clientX: 100, pointerId: 3, button: 0 });
    fireEvent.pointerMove(firstBody, { clientX: 190, pointerId: 3 });
    fireEvent.pointerUp(firstBody, { clientX: 190, pointerId: 3 });

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
    expect(screen.getByText("2 líneas")).toBeInTheDocument();
  });

  it("selects a contiguous range with Shift-click", () => {
    setup();
    const [first, second] = screen.getAllByTestId("timeline-segment");
    fireEvent.pointerDown(first.querySelector('[data-testid="timeline-segment-body"]'), { clientX: 100, pointerId: 1, button: 0, metaKey: true });
    fireEvent.pointerDown(second.querySelector('[data-testid="timeline-segment-body"]'), { clientX: 900, pointerId: 2, button: 0, shiftKey: true });
    expect(screen.getByText("2 líneas")).toBeInTheDocument();
  });

  it("deletes the selected lines from the contextual action", () => {
    const props = setup();
    const [first, second] = screen.getAllByTestId("timeline-segment");
    fireEvent.pointerDown(first.querySelector('[data-testid="timeline-segment-body"]'), { clientX: 100, pointerId: 1, button: 0, metaKey: true });
    fireEvent.pointerDown(second.querySelector('[data-testid="timeline-segment-body"]'), { clientX: 900, pointerId: 2, button: 0, ctrlKey: true });

    fireEvent.click(screen.getByRole("button", { name: "Eliminar 2 líneas" }));
    expect(props.onDeleteSelection).toHaveBeenCalledWith([0, 1]);
  });

  it("deletes one line directly from its visible row action without selecting it first", () => {
    const props = setup();
    const deleteButtons = screen.getAllByTestId("timeline-delete-line");

    expect(deleteButtons).toHaveLength(3);
    expect(deleteButtons[1]).toHaveAccessibleName("Eliminar línea 2");
    fireEvent.pointerDown(deleteButtons[1], { pointerId: 1, button: 0 });
    fireEvent.click(deleteButtons[1]);

    expect(props.onDeleteSelection).toHaveBeenCalledWith([1]);
    expect(screen.queryByText("1 línea")).not.toBeInTheDocument();
  });

  it("supports Delete from a focused timing block but ignores editable text", () => {
    const props = setup();
    const first = screen.getAllByTestId("timeline-segment")[0];
    fireEvent.pointerDown(first.querySelector('[data-testid="timeline-segment-body"]'), { clientX: 100, pointerId: 1, button: 0, metaKey: true });
    first.focus();
    fireEvent.keyDown(first, { key: "Delete" });
    expect(props.onDeleteSelection).toHaveBeenCalledWith([0]);

    props.onDeleteSelection.mockClear();
    const second = screen.getAllByTestId("timeline-segment")[1];
    fireEvent.doubleClick(second.querySelector("span[title*='Doble-click']"));
    fireEvent.keyDown(screen.getByDisplayValue("segunda línea"), { key: "Backspace" });
    expect(props.onDeleteSelection).not.toHaveBeenCalled();
  });

  it("keeps a usable move target on a very short line without overlapping its resize handles", () => {
    const props = setup({
      segments: [
        { _id: "short", start: 0.4, end: 0.7, text: "Oh" },
        { _id: "next", start: 1.2, end: 2, text: "Siguiente" },
      ],
      duration: 5,
    });
    const block = screen.getAllByTestId("timeline-segment")[0];
    const body = block.querySelector('[data-testid="timeline-segment-body"]');
    const startEdge = block.querySelector('[data-testid="timeline-edge-start"]');
    const endEdge = block.querySelector('[data-testid="timeline-edge-end"]');

    expect(parseFloat(block.style.width)).toBeCloseTo(0.3 * 48, 5);
    expect(body.style.width).toBe("28px");
    expect(parseFloat(startEdge.style.left)).toBeLessThan(-22);
    expect(parseFloat(endEdge.style.right)).toBeLessThan(-22);

    fireEvent.pointerDown(body, { clientX: 100, pointerId: 1, button: 0 });
    fireEvent.pointerMove(body, { clientX: 119, pointerId: 1 });
    fireEvent.pointerUp(body, { clientX: 119, pointerId: 1 });
    expect(props.onTimingChange).toHaveBeenCalledWith("short", expect.any(Number), expect.any(Number), expect.objectContaining({ operation: "move" }));
  });

  it("shows selection instructions and distinct move/resize cursors", () => {
    setup();
    const help = screen.getByTestId("timeline-selection-help");
    const block = screen.getAllByTestId("timeline-segment")[0];
    const edge = block.querySelector('[data-testid="timeline-edge-end"]');
    expect(help).toHaveTextContent("Papelera: elimina una línea");
    expect(help).toHaveTextContent("Arrastrá el fondo");
    expect(block.className).toContain("cursor-grab");
    expect(edge.className).toContain("cursor-ew-resize");
    expect(edge.style.width).toBe("22px");
  });

  it("distinguishes the playing row from purple selection", () => {
    setup({ activeId: 1, isPlaying: true });
    const rows = screen.getAllByTestId("timeline-label-row");
    expect(rows[1]).toHaveAttribute("aria-current", "true");
    expect(rows[1]).toHaveAttribute("data-active", "true");
    expect(rows[1].className).toContain("bg-cyan");
    expect(screen.getByText("Sonando")).toBeInTheDocument();
  });

  it("moves playheads with compositor transforms instead of layout left", () => {
    setup({ activeId: 1, currentTime: 2.5 });
    const main = screen.getByTestId("timeline-playhead");
    const active = screen.getByTestId("timeline-active-playhead");
    expect(main.style.left).toBe("");
    expect(active.style.left).toBe("");
    expect(main.style.transform).toBe("translate3d(120px, 0, 0)");
    expect(active.style.transform).toBe("translate3d(120px, 0, 0)");
  });

  it("does not scroll the outer editor when the active lyric changes", () => {
    const scrollIntoView = vi.fn();
    const original = HTMLElement.prototype.scrollIntoView;
    HTMLElement.prototype.scrollIntoView = scrollIntoView;
    const props = { segments: SEGS, duration: 60, currentTime: 5, isPlaying: true, activeId: 0 };
    const { rerender } = render(<LyricsTimeline {...props} />);
    rerender(<LyricsTimeline {...props} currentTime={10} activeId={1} />);
    expect(scrollIntoView).not.toHaveBeenCalled();
    HTMLElement.prototype.scrollIntoView = original;
  });

  it("resizes timing from either horizontal edge", () => {
    const props = setup();
    const block = screen.getAllByTestId("timeline-segment")[1];
    const edge = block.querySelector('[data-testid="timeline-edge-end"]');
    fireEvent.pointerDown(edge, { clientX: 1000, pointerId: 1, button: 0 });
    fireEvent.pointerMove(edge, { clientX: 1080, pointerId: 1 });
    fireEvent.pointerUp(edge, { clientX: 1080, pointerId: 1 });
    expect(props.onTimingChange).toHaveBeenCalledWith(1, 10, expect.any(Number), expect.objectContaining({ operation: "ripple_resize" }));
    expect(props.onTimingChange.mock.calls[0][2]).toBeGreaterThan(11);
  });

  it("uses Alt for fine edge adjustment", () => {
    const props = setup();
    const block = screen.getAllByTestId("timeline-segment")[1];
    const edge = block.querySelector('[data-testid="timeline-edge-end"]');
    fireEvent.pointerDown(edge, { clientX: 1000, pointerId: 1, button: 0 });
    fireEvent.pointerMove(edge, { clientX: 1048, pointerId: 1, altKey: true });
    fireEvent.pointerUp(edge, { clientX: 1048, pointerId: 1, altKey: true });
    const [, start, end] = props.onTimingChange.mock.calls[0];
    expect(start).toBe(10);
    expect(end).toBeCloseTo(11.1, 4);
  });

  it("ripple-trims a packed neighbour by default without shortening it", () => {
    const props = setup({
      segments: [
        { _id: "a", start: 0, end: 2, text: "línea actual" },
        { _id: "b", start: 2.05, end: 3.5, text: "línea siguiente" },
      ],
    });
    const block = screen.getAllByTestId("timeline-segment")[0];
    const edge = block.querySelector('[data-testid="timeline-edge-end"]');

    fireEvent.pointerDown(edge, { clientX: 100, pointerId: 1, button: 0 });
    fireEvent.pointerMove(edge, { clientX: 124, pointerId: 1 });
    fireEvent.pointerUp(edge, { clientX: 124, pointerId: 1 });
    expect(props.onTimingChange).not.toHaveBeenCalled();
    expect(props.onTimingChangeBatch).toHaveBeenCalledWith([
      { id: "a", start: 0, end: 2.5 },
      { id: "b", start: 2.55, end: 4 },
    ], expect.objectContaining({ operation: "ripple_resize" }));
  });

  it("keeps the right edge safe when the operator chooses Solo esta línea", () => {
    const props = setup({
      segments: [
        { _id: "a", start: 0, end: 2, text: "línea actual" },
        { _id: "b", start: 2.05, end: 3.5, text: "línea siguiente" },
      ],
    });
    fireEvent.click(screen.getAllByRole("button", { name: "Solo esta línea" })[0]);
    const block = screen.getAllByTestId("timeline-segment")[0];
    const edge = block.querySelector('[data-testid="timeline-edge-end"]');

    fireEvent.pointerDown(edge, { clientX: 100, pointerId: 1, button: 0 });
    fireEvent.pointerMove(edge, { clientX: 124, pointerId: 1 });
    fireEvent.pointerUp(edge, { clientX: 124, pointerId: 1 });

    expect(props.onTimingChange).not.toHaveBeenCalled();
    expect(props.onTimingChangeBatch).not.toHaveBeenCalled();
    expect(screen.getByTestId("timeline-limit-feedback")).toHaveTextContent("Para no modificarla, el ajuste se detuvo");
  });

  it("keeps the left edge protected even while right-edge ripple is the default", () => {
    const props = setup({
      segments: [
        { _id: "a", start: 0, end: 2, text: "línea anterior" },
        { _id: "b", start: 2.05, end: 4, text: "línea actual" },
      ],
    });
    const block = screen.getAllByTestId("timeline-segment")[1];
    const edge = block.querySelector('[data-testid="timeline-edge-start"]');

    fireEvent.pointerDown(edge, { clientX: 100, pointerId: 1, button: 0 });
    fireEvent.pointerMove(edge, { clientX: 76, pointerId: 1 });
    fireEvent.pointerUp(edge, { clientX: 76, pointerId: 1 });

    expect(props.onTimingChange).not.toHaveBeenCalled();
    expect(props.onTimingChangeBatch).not.toHaveBeenCalled();
    expect(screen.getByTestId("timeline-limit-feedback")).toHaveTextContent("Para no modificarla, el ajuste se detuvo");
  });

  it("cancels a visible ripple preview without committing a partial batch", () => {
    const props = setup({
      segments: [
        { _id: "a", start: 0, end: 2, text: "línea actual" },
        { _id: "b", start: 2.05, end: 3.5, text: "línea siguiente" },
      ],
    });
    const block = screen.getAllByTestId("timeline-segment")[0];
    const edge = block.querySelector('[data-testid="timeline-edge-end"]');

    fireEvent.pointerDown(edge, { clientX: 100, pointerId: 1, button: 0 });
    fireEvent.pointerMove(edge, { clientX: 124, pointerId: 1 });
    const previewNext = screen.getAllByTestId("timeline-segment")[1];
    expect(parseFloat(previewNext.style.left)).toBeCloseTo(2.55 * 48, 4);
    fireEvent.pointerCancel(edge, { clientX: 124, pointerId: 1 });

    expect(props.onTimingChange).not.toHaveBeenCalled();
    expect(props.onTimingChangeBatch).not.toHaveBeenCalled();
    expect(props.onDragStart).not.toHaveBeenCalled();
  });

  it("edits line text inline", () => {
    const props = setup();
    const block = screen.getAllByTestId("timeline-segment")[1];
    fireEvent.doubleClick(block.querySelector("span[title*='Doble-click']"));
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
    expect(screen.getByLabelText("Alejar")).toBeInTheDocument();
    expect(screen.getByLabelText("Acercar")).toBeInTheDocument();
    fireEvent.click(screen.getByText("Restaurar tiempos originales"));
    expect(props.onReset).toHaveBeenCalledTimes(1);
  });

  it("auto-follows the playhead once without issuing near-duplicate scrolls", () => {
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

describe("zonas dudosas sobre la forma de onda", () => {
  // La timeline —donde el operador corrige el timing— no recibía NINGUNA señal
  // de calidad, así que para encontrar el punto a corregir sólo podía clickear
  // y escuchar: 2,9 seeks medidos por cada corrección. El backend ya calcula
  // `unsafe_windows`; acá se pintan.
  it("pinta una banda por cada ventana dudosa", () => {
    setup({
      unsafeWindows: [
        { id: "w1", start: 8, end: 12, reasons: ["low_ctc_timing_confidence"] },
        { id: "w2", start: 39, end: 42, reasons: ["voiced_gap"] },
      ],
    });
    expect(screen.getAllByTestId("timeline-unsafe-window")).toHaveLength(2);
  });

  it("no pinta nada cuando no hay ventanas (comportamiento previo intacto)", () => {
    setup();
    expect(screen.queryAllByTestId("timeline-unsafe-window")).toHaveLength(0);
  });

  it("descarta ventanas con tiempos inválidos en vez de romper el render", () => {
    setup({
      unsafeWindows: [
        { id: "ok", start: 5, end: 9 },
        { id: "invertida", start: 20, end: 20 },
        { id: "no-numerica", start: "x", end: "y" },
      ],
    });
    expect(screen.getAllByTestId("timeline-unsafe-window")).toHaveLength(1);
  });

  it("clampea la banda contra la duración (sin scroll fantasma)", () => {
    // live_structural_disagreement genera ventanas que terminan después del
    // audio; sin clamp la banda estiraba el track.
    setup({ duration: 60, unsafeWindows: [{ id: "w", start: 55, end: 200 }] });
    const band = screen.getByTestId("timeline-unsafe-window");
    const width = parseFloat(band.style.width);
    expect(width).toBeGreaterThan(0);
    expect(width).toBeLessThan(60 * 48); // no puede exceder el track completo
  });
});
