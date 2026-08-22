import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import GuidedTimingReview from "./GuidedTimingReview";

vi.mock("../i18n", () => ({
  useI18n: () => ({ t: () => undefined }),
}));

const WINDOWS = [
  { id: "chorus", start: 10, end: 14, reasons: ["low_ctc_timing_confidence"] },
  { id: "outro", start: 20, end: 25, reasons: ["event_count"] },
];

const SEGMENTS = [
  { _id: "line-1", start: 10.2, end: 12.4, text: "Primera frase" },
  { _id: "line-2", start: 20.3, end: 23.2, text: "Segunda frase" },
];

function props(overrides = {}) {
  return {
    windows: WINDOWS,
    segments: SEGMENTS,
    waveform: { duration: 30, peaks: [0.1, 0.4, 0.9, 0.3, 0.7, 0.2] },
    duration: 30,
    currentTime: 10.5,
    confirmedIds: new Set(),
    onConfirm: vi.fn(),
    onPlayWindow: vi.fn(),
    onSeek: vi.fn(),
    onMove: vi.fn(),
    onOpenAdvanced: vi.fn(),
    ...overrides,
  };
}

afterEach(cleanup);

describe("GuidedTimingReview", () => {
  it("mantiene el paso a paso y oculta ajustes hasta que hacen falta", async () => {
    render(<GuidedTimingReview {...props()} />);

    expect(screen.getByTestId("guided-stepper")).toHaveTextContent("1. Escuchá");
    expect(screen.getByTestId("guided-stepper")).toHaveTextContent("2. Compará/Ajustá");
    expect(screen.getByTestId("guided-stepper")).toHaveTextContent("3. Confirmá");
    expect(screen.getByRole("button", { name: /Escuchar fragmento/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /mover 0,1 segundos antes/i })).toBeNull();
    expect(screen.getByTestId("guided-waveform-coachmark")).toHaveTextContent("Escuchá el fragmento");

    await userEvent.click(screen.getByRole("button", { name: /Escuchar fragmento/ }));
    expect(screen.getByText("¿La frase aparece cuando empieza la voz?")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Sí, está bien" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "No, ajustar" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /mover 0,1 segundos antes/i })).toBeNull();

    await userEvent.click(screen.getByRole("button", { name: "No, ajustar" }));
    expect(screen.getByRole("button", { name: /mover 0,1 segundos antes/i })).toBeInTheDocument();
    expect(screen.getByTestId("guided-phrase-coachmark")).toBeInTheDocument();
  });

  it("permite confirmar sin mover la frase cuando ya coincide", async () => {
    const onMove = vi.fn();
    const onConfirm = vi.fn();
    render(<GuidedTimingReview {...props({ onMove, onConfirm })} />);

    await userEvent.click(screen.getByRole("button", { name: /Escuchar fragmento/ }));
    await userEvent.click(screen.getByRole("button", { name: "Sí, está bien" }));
    expect(screen.getByTestId("guided-confirm-coachmark")).toHaveTextContent("No muevas la frase");
    expect(screen.getByRole("button", { name: /confirmar y seguir/i })).toBeEnabled();
    expect(onMove).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: /confirmar y seguir/i }));
    expect(onConfirm).toHaveBeenCalledWith(WINDOWS[0]);
  });

  it("permite repetir el tutorial desde Cómo funciona", async () => {
    render(<GuidedTimingReview {...props()} />);
    await userEvent.click(screen.getByRole("button", { name: /cómo funciona/i }));
    expect(screen.getByRole("dialog", { name: /cómo funciona/i })).toHaveTextContent("tres pasos");
    expect(screen.getByRole("button", { name: /Escuchar fragmento/ })).toBeInTheDocument();
  });

  it("presenta una sola decisión con lenguaje humano y audio visible", () => {
    render(<GuidedTimingReview {...props()} />);

    expect(screen.getByTestId("guided-timing-review")).toHaveTextContent("Encontramos 2 partes");
    expect(screen.getByText("Parte 1 de 2")).toBeInTheDocument();
    expect(screen.getByText("El inicio o final puede estar corrido")).toBeInTheDocument();
    expect(screen.getByText("Audio de la canción")).toBeInTheDocument();
    expect(screen.getByText("Los picos muestran dónde hay voz o sonido")).toBeInTheDocument();
    expect(screen.getAllByText("Primera frase")).toHaveLength(1);
    expect(screen.queryByText(/low_ctc/i)).toBeNull();
    expect(screen.getByTestId("guided-waveform").querySelector("canvas")).toBeInTheDocument();
  });

  it("permite buscar en el audio con teclado", () => {
    const onSeek = vi.fn();
    render(<GuidedTimingReview {...props({ onSeek, currentTime: 10.5 })} />);
    const surface = screen.getByRole("group", { name: /Buscar dentro de este tramo/i });
    fireEvent.keyDown(surface, { key: "ArrowRight" });
    expect(onSeek).toHaveBeenCalledWith(10.6);
    fireEvent.keyDown(surface, { key: "Home" });
    expect(onSeek).toHaveBeenLastCalledWith(8.5);
  });

  it("reproduce el tramo, ajusta de a 100 ms y confirma antes de avanzar", async () => {
    const onPlayWindow = vi.fn();
    const onStopPlayback = vi.fn();
    const onMove = vi.fn();
    const onConfirm = vi.fn();
    render(<GuidedTimingReview {...props({ onPlayWindow, onStopPlayback, onMove, onConfirm })} />);

    await userEvent.click(screen.getByRole("button", { name: /Escuchar fragmento/ }));
    expect(onPlayWindow).toHaveBeenCalledWith(WINDOWS[0]);

    await userEvent.click(screen.getByRole("button", { name: "No, ajustar" }));
    await userEvent.click(screen.getByRole("button", { name: /mover 0,1 segundos antes/i }));
    expect(onMove).toHaveBeenCalledWith("line-1", 10.1, 12.3, { operation: "guided_nudge" });

    await userEvent.click(screen.getByRole("button", { name: /confirmar y seguir/i }));
    expect(onStopPlayback).toHaveBeenCalled();
    expect(onConfirm).toHaveBeenCalledWith(WINDOWS[0]);
    expect(screen.getByText("Parte 2 de 2")).toBeInTheDocument();
    expect(screen.getByText("Puede faltar o sobrar una frase")).toBeInTheDocument();
  });

  it("cancela un gesto interrumpido sin guardar el movimiento", async () => {
    const onMove = vi.fn();
    render(<GuidedTimingReview {...props({ onMove })} />);
    await userEvent.click(screen.getByRole("button", { name: /Escuchar fragmento/ }));
    await userEvent.click(screen.getByRole("button", { name: "No, ajustar" }));
    const phrase = screen.getByTestId("guided-segment-line-1");
    fireEvent.pointerDown(phrase, { pointerId: 8, clientX: 100, button: 0 });
    fireEvent.pointerMove(phrase, { pointerId: 8, clientX: 180 });
    fireEvent.pointerCancel(phrase, { pointerId: 8, clientX: 180 });
    expect(onMove).not.toHaveBeenCalled();
  });

  it("aborta el drag si una reconciliación cambió la frase durante el gesto", async () => {
    const onMove = vi.fn();
    const initial = props({ onMove });
    const { rerender } = render(<GuidedTimingReview {...initial} />);
    await userEvent.click(screen.getByRole("button", { name: /Escuchar fragmento/ }));
    await userEvent.click(screen.getByRole("button", { name: "No, ajustar" }));
    const phrase = screen.getByTestId("guided-segment-line-1");
    fireEvent.pointerDown(phrase, { pointerId: 9, clientX: 100, button: 0 });
    rerender(<GuidedTimingReview {...initial} segments={[
      { ...SEGMENTS[0], start: 10.4, end: 12.6 },
      SEGMENTS[1],
    ]} />);
    const updatedPhrase = screen.getByTestId("guided-segment-line-1");
    fireEvent.pointerMove(updatedPhrase, { pointerId: 9, clientX: 170 });
    fireEvent.pointerUp(updatedPhrase, { pointerId: 9, clientX: 170 });
    expect(onMove).not.toHaveBeenCalled();
  });

  it("bloquea nudges cuando la frase ya tiene un solapamiento destructivo", async () => {
    const onMove = vi.fn();
    render(<GuidedTimingReview {...props({
      windows: [{ id: "overlap", start: 9, end: 18, reasons: ["overlap"] }],
      segments: [
        { _id: "before", start: 9, end: 11, text: "Anterior" },
        { _id: "line-1", start: 10.5, end: 14.5, text: "Solapada" },
        { _id: "after", start: 14, end: 17, text: "Siguiente" },
      ],
      onMove,
    })} />);

    await userEvent.click(screen.getByRole("button", { name: /Escuchar fragmento/ }));
    await userEvent.click(screen.getByRole("button", { name: "No, ajustar" }));
    expect(screen.getByText(/ya se superpone con otra/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /mover 0,1 segundos antes/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /mover 0,1 segundos después/i })).toBeDisabled();
    expect(onMove).not.toHaveBeenCalled();
  });

  it("no reproduce una ventana que quedó fuera de la duración real", async () => {
    const onPlayWindow = vi.fn();
    const onConfirm = vi.fn();
    render(<GuidedTimingReview {...props({
      windows: [{ id: "outside", start: 40, end: 45, reasons: ["timing"] }],
      segments: [],
      duration: 30,
      waveform: { duration: 30, peaks: [0.2, 0.5] },
      onPlayWindow,
      onConfirm,
    })} />);

    expect(screen.getByText(/fuera de la duración del audio/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /fuera de la duración del audio/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /confirmar y seguir/i })).toBeDisabled();
    expect(onPlayWindow).not.toHaveBeenCalled();
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("deshabilita escuchar sin audio y ofrece reintento", async () => {
    const onRetryAudio = vi.fn();
    render(<GuidedTimingReview {...props({ audioAvailable: false, audioLoading: false, onRetryAudio })} />);
    expect(screen.getByRole("button", { name: /audio no disponible/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /confirmar y seguir/i })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: /reintentar audio/i }));
    expect(onRetryAudio).toHaveBeenCalledTimes(1);
  });

  it("detiene el loop si quality reemplaza la ventana activa", () => {
    const onStopPlayback = vi.fn();
    const initial = props({ onStopPlayback, playingWindowId: "chorus", isPlaying: true });
    const { rerender } = render(<GuidedTimingReview {...initial} />);
    onStopPlayback.mockClear();
    rerender(<GuidedTimingReview {...initial} windows={[
      { id: "terminal", start: 11, end: 15, reasons: ["timing"] },
    ]} />);
    expect(onStopPlayback).toHaveBeenCalled();
  });

  it("no permite mover el final hacia adelante sin una duración conocida", async () => {
    render(<GuidedTimingReview {...props({
      windows: [{ id: "unknown-duration", start: 10, end: 14, reasons: ["timing"] }],
      segments: [{ _id: "last", start: 10.2, end: 12.4, text: "Última frase" }],
      waveform: null,
      duration: 0,
    })} />);
    await userEvent.click(screen.getByRole("button", { name: /Escuchar fragmento/ }));
    await userEvent.click(screen.getByRole("button", { name: "No, ajustar" }));
    expect(screen.getByRole("button", { name: /mover 0,1 segundos antes/i })).toBeEnabled();
    expect(screen.getByRole("button", { name: /mover 0,1 segundos después/i })).toBeDisabled();
  });

  it("permite arrastrar una frase sin cruzar a sus vecinas", async () => {
    const onMove = vi.fn();
    render(<GuidedTimingReview {...props({
      windows: [{ id: "wide", start: 5, end: 25, reasons: ["timing"] }],
      onMove,
    })} />);
    await userEvent.click(screen.getByRole("button", { name: /Escuchar fragmento/ }));
    await userEvent.click(screen.getByRole("button", { name: "No, ajustar" }));
    const phrase = screen.getByTestId("guided-segment-line-1");
    fireEvent.pointerDown(phrase, { pointerId: 1, clientX: 100, button: 0 });
    fireEvent.pointerMove(phrase, { pointerId: 1, clientX: 150 });
    fireEvent.pointerUp(phrase, { pointerId: 1, clientX: 150 });
    expect(onMove).toHaveBeenCalled();
    const [, start, end, interaction] = onMove.mock.calls[0];
    expect(start).toBeGreaterThan(10.2);
    expect(end).toBeLessThanOrEqual(20.25);
    expect(interaction).toEqual({ operation: "guided_drag" });
  });

  it("respeta una frase vecina aunque quede fuera de la ventana visible", async () => {
    const onMove = vi.fn();
    render(<GuidedTimingReview {...props({
      windows: [{ id: "focused", start: 9, end: 13, reasons: ["timing"] }],
      segments: [
        { _id: "line-1", start: 10.2, end: 12.4, text: "Primera frase" },
        { _id: "line-2", start: 14.2, end: 16, text: "Vecina fuera de vista" },
      ],
      onMove,
    })} />);

    await userEvent.click(screen.getByRole("button", { name: /Escuchar fragmento/ }));
    await userEvent.click(screen.getByRole("button", { name: "No, ajustar" }));
    expect(screen.queryByTestId("guided-segment-line-2")).toBeNull();
    const phrase = screen.getByTestId("guided-segment-line-1");
    fireEvent.pointerDown(phrase, { pointerId: 2, clientX: 100, button: 0 });
    fireEvent.pointerMove(phrase, { pointerId: 2, clientX: 600 });
    fireEvent.pointerUp(phrase, { pointerId: 2, clientX: 600 });

    expect(onMove).toHaveBeenCalled();
    expect(onMove.mock.calls[0][2]).toBeCloseTo(14.15, 5);
  });

  it("distingue carga, ausencia visual, resultado limpio y revisión completa", () => {
    const { rerender } = render(<GuidedTimingReview {...props({ waveform: null, waveformLoading: true })} />);
    expect(screen.getByRole("status", { name: "" })).toHaveTextContent(/Preparando guía de audio/i);

    rerender(<GuidedTimingReview {...props({ waveform: null, waveformLoading: false })} />);
    expect(screen.getByText(/guía visual no está disponible/i)).toBeInTheDocument();

    rerender(<GuidedTimingReview {...props({ windows: [] })} />);
    expect(screen.getByTestId("guided-timing-empty")).toHaveTextContent("No encontramos partes dudosas");

    rerender(<GuidedTimingReview {...props({ confirmedIds: new Set(["chorus", "outro"]) })} />);
    expect(screen.getByTestId("guided-timing-complete")).toHaveTextContent("Sincronización revisada");
  });
});
