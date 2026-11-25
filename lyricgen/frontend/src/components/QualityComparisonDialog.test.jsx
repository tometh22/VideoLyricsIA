import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import QualityProposalPanel from "./QualityProposalPanel";
import { buildQualityComparison } from "../lib/qualityComparison";

vi.mock("./NativeLyricPreview", async () => {
  const { useEffect } = await import("react");
  return { default: function Preview({ segments, onStateChange, currentTime }) {
    useEffect(() => { onStateChange("ready"); }, []);
    return <output data-testid="subtitles">{segments[0].end} / {currentTime}</output>;
  } };
});

const current = { start: 12, end: 16, text: "La voz" };
const next = { start: 17, end: 18, text: "Siguiente" };
const windowProposal = {
  id: "window-1", start: 12, end: 17, current_segments: [current],
  proposed_segments: [{ ...current, end: 16.6 }],
};

afterEach(() => {
  cleanup();
  Reflect.deleteProperty(HTMLDialogElement.prototype, "showModal");
  Reflect.deleteProperty(HTMLDialogElement.prototype, "close");
  vi.restoreAllMocks();
});

it("encuadra un final tardío y conserva todos los carteles vecinos en ambos renders", () => {
  const long = { ...current, start: 1, end: 30 };
  const comparison = buildQualityComparison([long, { ...next, start: 32, end: 33 }], {
    start: 1, end: 32, current_segments: [long], proposed_segments: [{ ...long, end: 31 }],
  }, 34);
  expect(comparison).toMatchObject({ start: 27.5, end: 33.5 });
  expect(comparison.current[0]).toBe(long);
  expect(comparison.current[1]).toBe(comparison.proposed[1]);
  expect(comparison.proposed[0]).toMatchObject({ end: 31 });
});

it("omite una comparación desactualizada o con cambios que superan diez segundos", () => {
  expect(buildQualityComparison([{ ...current, text: "Editada" }], windowProposal, 30)).toBeNull();
  const long = { ...current, end: 25 };
  expect(buildQualityComparison([long], {
    start: 12, end: 25, current_segments: [long], proposed_segments: [{ ...long, text: "Otra letra" }],
  }, 30)).toBeNull();
});

it("compara una frase propuesta dentro de un hueco sin perder el contexto", () => {
  const addition = { ...current, start: 16.2, end: 16.8 };
  const comparison = buildQualityComparison([current, next], {
    start: 16, end: 17, current_segments: [], proposed_segments: [addition],
  }, 30);
  expect(comparison.proposed).toEqual([current, addition, next]);
});

function openComparison(sourceOverrides = {}) {
  vi.spyOn(Math, "random").mockReturnValue(0.1);
  HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  HTMLDialogElement.prototype.close = function () { this.open = false; };
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(function () { fireEvent.pause(this); });
  vi.spyOn(HTMLMediaElement.prototype, "play").mockImplementation(function () {
    fireEvent.playing(this);
    return Promise.resolve();
  });
  const proposal = { id: "pilot-1", base_revision: 4, windows: [windowProposal] };
  const comparisonSource = {
    segments: [current, next], duration: 30, jobId: "job-1", audioUrl: "/master.wav", settings: {},
    request: vi.fn(), onOpen: vi.fn(), onDecision: vi.fn().mockResolvedValue(true), ...sourceOverrides,
  };
  const props = { proposal, currentRevision: 4, comparisonSource, onApplySelected: vi.fn() };
  const { container, rerender } = render(<QualityProposalPanel {...props} />);
  fireEvent.click(screen.getByText(/Ver propuestas/));
  fireEvent.click(screen.getByRole("button", { name: "Comparar A/B" }));
  const dialog = within(screen.getByRole("dialog"));
  const audio = container.querySelector("audio");
  Object.defineProperty(audio, "duration", { value: 30 });
  fireEvent.loadedMetadata(audio);
  const finish = (variant) => {
    fireEvent.click(dialog.getByRole("button", { name: `Reproducir ${variant}` }));
    expect(audio.currentTime).toBeCloseTo(13.3);
    audio.currentTime = 19.3;
    fireEvent.ended(audio);
  };
  return { dialog, finish, props, rerender };
}

it("exige ambos pases, guarda el voto con su revisión y pide una decisión separada para aplicar", async () => {
  const { dialog, finish, props } = openComparison();
  expect(props.comparisonSource.onOpen).toHaveBeenCalledOnce();
  expect(dialog.getByRole("button", { name: "Prefiero B" })).toBeDisabled();
  expect(dialog.queryByText(/borrador actual/)).toBeNull();
  expect(dialog.getByTestId("subtitles")).toHaveTextContent(/^16 \/ /);
  finish("A");
  expect(dialog.getByRole("button", { name: "Prefiero B" })).toBeDisabled();
  fireEvent.click(dialog.getByRole("button", { name: "Versión B" }));
  expect(dialog.getByTestId("subtitles")).toHaveTextContent(/^16\.6 \/ /);
  finish("B");
  fireEvent.click(dialog.getByRole("button", { name: "Prefiero B" }));
  expect(await dialog.findByText(/A es el borrador actual. B es la propuesta/)).toBeVisible();
  expect(props.comparisonSource.onDecision).toHaveBeenCalledWith(expect.objectContaining({
    choice: "proposed", proposal_id: "pilot-1", window_id: "window-1",
    revision: 4, variant_a: "current", first_heard: "current", position_ms: 13300, duration_ms: 6000,
  }));
  expect(props.onApplySelected).not.toHaveBeenCalled();
  fireEvent.click(dialog.getByRole("button", { name: "Aplicar al borrador" }));
  expect(props.onApplySelected).toHaveBeenCalledWith(["window-1"], props.proposal);
});

it("reintenta el mismo voto tras un fallo y cierra al cambiar la revisión", async () => {
  const onDecision = vi.fn().mockResolvedValueOnce(false).mockResolvedValueOnce(true);
  const { dialog, finish, props, rerender } = openComparison({ onDecision });
  finish("A");
  fireEvent.click(dialog.getByRole("button", { name: "Versión B" }));
  finish("B");
  fireEvent.click(dialog.getByRole("button", { name: "Ambas necesitan edición" }));
  expect(await dialog.findByRole("alert")).toBeVisible();
  expect(dialog.queryByText(/borrador actual/)).toBeNull();
  fireEvent.click(dialog.getByRole("button", { name: "Reintentar guardado" }));
  await dialog.findByText(/Decisión guardada/);
  expect(onDecision.mock.calls[0]).toEqual(onDecision.mock.calls[1]);
  expect(dialog.queryByRole("button", { name: "Aplicar al borrador" })).toBeNull();
  act(() => rerender(<QualityProposalPanel {...props} currentRevision={5} />));
  expect(screen.queryByRole("dialog")).toBeNull();
});

it("permite corregir de inmediato sin exigir reproducción, voto ni aplicación", () => {
  const onEdit = vi.fn();
  const { dialog, props } = openComparison({ onEdit });
  fireEvent.click(dialog.getByRole("button", { name: "Corregir en el editor" }));
  expect(onEdit).toHaveBeenCalledWith(windowProposal);
  expect(props.comparisonSource.onDecision).not.toHaveBeenCalled();
  expect(props.onApplySelected).not.toHaveBeenCalled();
  expect(screen.queryByRole("dialog")).toBeNull();
});

it("prioriza tres comparaciones y deja los cambios pequeños en la revisión manual", () => {
  const segments = Array.from({ length: 5 }, (_, i) => ({ text: `Frase ${i}`, start: 5 * i, end: 5 * i + 2 }));
  const windows = segments.map((line, i) => ({
    id: `w${i}`, start: line.start, end: line.end + 1, suggestion_type: "timing",
    current_segments: [line], proposed_segments: [{ ...line, end: line.end + (i === 4 ? 0.15 : 0.5) }],
  }));
  render(<QualityProposalPanel currentRevision={4}
    proposal={{ id: "ranked", base_revision: 4, windows }}
    comparisonSource={{ segments, duration: 30 }} />);
  fireEvent.click(screen.getByText(/Ver propuestas/));
  const more = screen.getByText("Ver más sugerencias (2)");
  expect(more.parentElement).not.toHaveAttribute("open");
  expect(within(more.parentElement).getAllByText("Comparar A/B")).toHaveLength(1);
  fireEvent.click(more);
  expect(screen.getAllByRole("button", { name: "Comparar A/B" })).toHaveLength(4);
  expect(within(screen.getByTestId("quality-proposal-window-w4")).queryByRole("button", { name: "Comparar A/B" })).toBeNull();
});
