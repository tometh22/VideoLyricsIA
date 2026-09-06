import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import CompleteReviewerCandidate from "./CompleteReviewerCandidate";

afterEach(cleanup);
const baseline = [{ text: "Canto", start: 2, end: 4 }];
const candidate = { source: { segments_revision: 3 }, baseline, segments: baseline,
  changes: [], review_details: { localized_doubts: [{ line_index: 0, reason: "Final incierto" }] } };

it("muestra candidata sin cambios sin certificar ni ofrecer aprobación", async () => {
  const onSeek = vi.fn();
  render(<CompleteReviewerCandidate candidate={candidate} currentRevision={3} currentSegments={baseline} onSeek={onSeek} />);
  expect(screen.getByText(/Sin cambios respaldados; no certifica exactitud/)).toBeTruthy();
  await userEvent.click(screen.getByText(/Ver letra y timing/));
  await userEvent.click(screen.getByRole("button", { name: "Escuchar línea 1" }));
  expect(onSeek).toHaveBeenCalledWith(1);
  expect(onSeek.mock.calls[0]).toHaveLength(1); // No clip end or forced stop.
  expect(screen.queryByRole("button", { name: /aprobar|aplicar|usar candidata/i })).toBeNull();
});

it("permite escuchar dudas localizadas con el audio existente", async () => {
  const onSeek = vi.fn();
  render(<CompleteReviewerCandidate candidate={candidate} currentRevision={3} currentSegments={baseline} onSeek={onSeek} />);
  await userEvent.click(screen.getByText(/Dudas y límites/));
  await userEvent.click(screen.getByRole("button", { name: "Escuchar duda 1" }));
  expect(onSeek).toHaveBeenCalledWith(1);
});

it("muestra decisiones retenidas como dudas sin acciones de incorporación", async () => {
  const onSeek = vi.fn();
  render(<CompleteReviewerCandidate candidate={{ ...candidate,
    review_details: { held_decisions: [{ line_index: 0, reason: "phrase_association_unresolved" }] } }}
    currentRevision={3} currentSegments={baseline} onSeek={onSeek} />);
  await userEvent.click(screen.getByText(/Dudas y límites/));
  expect(screen.getByText(/No se pudo localizar esta frase/)).toBeInTheDocument();
  expect(screen.getByText(/no se incorporan como cambios/)).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Escuchar duda 1" }));
  expect(onSeek).toHaveBeenCalledWith(1);
  expect(screen.queryByRole("button", { name: /incorporar|aprobar|aplicar/i })).toBeNull();
});

it.each([[4, baseline], [3, [{ ...baseline[0], text: "Edición humana" }]]])(
  "oculta candidata desactualizada tras revisión o edición local", (revision, segments) => {
    const { container } = render(<CompleteReviewerCandidate candidate={candidate} currentRevision={revision} currentSegments={segments} />);
    expect(container.innerHTML).toBe("");
  });

it("permanece ausente cuando el servidor no habilitó la candidata", () => {
  const { container } = render(<CompleteReviewerCandidate currentRevision={3} currentSegments={baseline} />);
  expect(container.innerHTML).toBe("");
});

it("no dirige al revisor a adoptar otras propuestas nativas", () => {
  render(<CompleteReviewerCandidate candidate={{ ...candidate, changes: [{}],
    adoption_status: "existing_different_proposal_preserved" }}
    currentRevision={3} currentSegments={baseline} />);
  expect(screen.getByText(/Las propuestas actuales son distintas y se conservan/)).toBeTruthy();
  expect(screen.queryByText(/podés usar su acción de incorporación/)).toBeNull();
});

it("solo identifica la acción existente si corresponde a la candidata", () => {
  render(<CompleteReviewerCandidate candidate={{ ...candidate, changes: [{}],
    adoption_status: "matching_existing_proposal" }} currentRevision={3} currentSegments={baseline} />);
  expect(screen.getByText(/La propuesta asociada corresponde a esta candidata/)).toBeTruthy();
  expect(screen.queryByRole("button", { name: /incorporar|aprobar/i })).toBeNull();
});
