import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { CampaignReviewerRow, CampaignReviewerSummary } from "./CampaignReviewerStatus";

describe("campaign reviewer status", () => {
  it.each([
    ["missing_independent_audio", "Faltan escuchas de una de las familias de modelos."],
    ["empty_transcription", "El documento no tiene texto transcripto."],
    ["audio_unavailable", "El audio no está disponible para revisar."],
    ["source_changed", "La candidata quedó desactualizada porque cambió el audio o el documento."],
    ["tool_failure", "Una herramienta no pudo completar el análisis."],
  ])("explains %s without exposing internal codes", (blocker, message) => {
    render(<CampaignReviewerRow status={{ status: "blocked", blocker }} />);
    expect(screen.getByText(message)).toBeInTheDocument();
    expect(screen.queryByText(blocker)).not.toBeInTheDocument();
  });
  it("does not guess the cause or expose an unknown raw exception", () => {
    render(<CampaignReviewerRow status={{ status: "blocked", blocker: "exception:/private/audio.wav" }} />);
    expect(screen.getByText(/motivo todavía no está clasificado/)).toBeInTheDocument();
    expect(screen.queryByText(/private/)).not.toBeInTheDocument();
  });
  it("is hidden unless the backend explicitly enables the campaign", () => {
    const { container, rerender } = render(<CampaignReviewerSummary status={{ total: 300 }} />);
    expect(container).toBeEmptyDOMElement();
    rerender(<CampaignReviewerSummary status={{ enabled: false, total: 300 }} />);
    expect(container).toBeEmptyDOMElement();
  });
  it("shows whole-roster counts and separates stale from completed", () => {
    render(<CampaignReviewerSummary status={{ enabled: true, total: 300,
      counters: { complete: 290, partial: 3, pending: 2, blocked: 4, stale: 1 },
      candidate_count: 289, changed_song_count: 42, unchanged_song_count: 247 }} />);
    expect(screen.getByText("Revisión asistida · 290/300 completas")).toBeInTheDocument();
    for (const label of ["Completa", "Parcial", "Pendiente", "Bloqueada", "Desactualizada"])
      expect(screen.getByText(label)).toBeInTheDocument();
    expect(screen.getByText(/Candidatas disponibles: 289/)).toBeInTheDocument();
    expect(screen.getByText(/No equivale a aprobación humana/)).toBeInTheDocument();
  });
  it("opens a complete published candidate in the existing editor, including no-change candidates", () => {
    const open = vi.fn();
    render(<CampaignReviewerRow status={{ status: "complete", candidate_available: true, changes_count: 0, doubts_count: 7 }} jobId="abc123" onOpen={open} />);
    fireEvent.click(screen.getByRole("button", { name: /Ver candidata/ }));
    expect(open).toHaveBeenCalledWith("/review/abc123");
    expect(screen.getByText("0 cambios · 7 dudas")).toBeInTheDocument();
  });
  it.each(["partial", "pending", "blocked", "stale", "unknown"])("does not offer candidate access for %s even with an inconsistent availability flag", (status) => {
    render(<CampaignReviewerRow status={{ status, candidate_available: true }} jobId="abc" onOpen={vi.fn()} />);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });
  it("does not invent a candidate or zero counts from missing information", () => {
    render(<CampaignReviewerRow status={{ status: "complete", candidate_available: false }} jobId="abc" onOpen={vi.fn()} />);
    expect(screen.getByText("Candidata aún no disponible en el editor.")).toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });
});
