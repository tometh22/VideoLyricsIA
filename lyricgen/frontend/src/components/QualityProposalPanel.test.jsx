import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import QualityProposalPanel from "./QualityProposalPanel";

const PROPOSAL = {
  id: "proposal-v6-1",
  base_revision: 12,
  expires_at: "2099-08-20T12:00:00.000Z",
  status: "pending",
  windows: [
    {
      id: "outro-a",
      start: 60.85,
      end: 67.04,
      current_segments: [{ start: 60.84, end: 70.83, text: "Real, real, real." }],
      proposed_segments: [
        { start: 60.85, end: 63.77, text: "Real, uoh uoh" },
        { start: 63.77, end: 67.04, text: "Real, uoh uoh" },
      ],
      reasons: ["acoustic_cardinality_disagreement", "vocalization"],
    },
    {
      id: "outro-b",
      start: 75.65,
      end: 83.27,
      current_segments: ["Real"],
      proposed_segments: ["¡no!", "¡noooo!"],
      reasons: [{ code: "text_audio_mismatch" }],
    },
  ],
};

afterEach(cleanup);

describe("QualityProposalPanel", () => {
  it("compara antes/después y nunca selecciona ni aplica automáticamente", () => {
    const onApplySelected = vi.fn();
    const onDismiss = vi.fn();
    const onSeek = vi.fn();

    render(
      <QualityProposalPanel
        proposal={PROPOSAL}
        currentRevision={12}
        onApplySelected={onApplySelected}
        onDismiss={onDismiss}
        onSeek={onSeek}
      />,
    );

    expect(screen.getByText(/Nada se aplica automáticamente/i)).toBeInTheDocument();
    expect(screen.getAllByRole("checkbox")).toHaveLength(2);
    expect(screen.getAllByRole("checkbox").every((checkbox) => !checkbox.checked)).toBe(true);
    expect(screen.getByRole("button", { name: "Aplicar seleccionadas (0)" })).toBeDisabled();
    expect(onApplySelected).not.toHaveBeenCalled();
    expect(onDismiss).not.toHaveBeenCalled();
    expect(onSeek).not.toHaveBeenCalled();

    const firstWindow = screen.getByTestId("quality-proposal-window-outro-a");
    expect(within(firstWindow).getByRole("list", { name: "Antes" })).toHaveTextContent("Real, real, real.");
    expect(within(firstWindow).getByRole("list", { name: "Propuesta" })).toHaveTextContent("Real, uoh uoh");
    expect(within(firstWindow).getByText(/La cantidad de frases no coincide con el audio/i)).toBeInTheDocument();
  });

  it("permite escuchar y aplicar solamente las ventanas elegidas", async () => {
    const user = userEvent.setup();
    const onApplySelected = vi.fn();
    const onSeek = vi.fn();

    render(
      <QualityProposalPanel
        proposal={PROPOSAL}
        currentRevision={12}
        onApplySelected={onApplySelected}
        onDismiss={vi.fn()}
        onSeek={onSeek}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Escuchar zona 2 desde 1:15.7" }));
    expect(onSeek).toHaveBeenCalledWith(75.65, PROPOSAL.windows[1]);

    await user.click(screen.getByRole("checkbox", { name: "Seleccionar zona 2, 1:15.7 a 1:23.3" }));
    expect(screen.getByText("1 corrección seleccionada.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Aplicar seleccionadas (1)" }));

    expect(onApplySelected).toHaveBeenCalledTimes(1);
    expect(onApplySelected).toHaveBeenCalledWith(["outro-b"], PROPOSAL);
  });

  it("descarta únicamente por una acción explícita", async () => {
    const user = userEvent.setup();
    const onDismiss = vi.fn();
    render(
      <QualityProposalPanel
        proposal={PROPOSAL}
        currentRevision={12}
        onApplySelected={vi.fn()}
        onDismiss={onDismiss}
      />,
    );

    expect(onDismiss).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Descartar propuesta" }));
    expect(onDismiss).toHaveBeenCalledTimes(1);
    expect(onDismiss).toHaveBeenCalledWith(PROPOSAL);
  });

  it("marca como obsoleta una propuesta de otra revisión y bloquea mutaciones", () => {
    const onApplySelected = vi.fn();
    const onDismiss = vi.fn();
    const { container } = render(
      <QualityProposalPanel
        proposal={PROPOSAL}
        currentRevision={13}
        onApplySelected={onApplySelected}
        onDismiss={onDismiss}
      />,
    );

    expect(screen.getByRole("alert")).toHaveTextContent(/versión anterior/i);
    expect(screen.getByTestId("quality-proposal-panel")).toHaveAttribute("data-proposal-state", "stale");
    expect(screen.getAllByRole("checkbox").every((checkbox) => checkbox.disabled)).toBe(true);
    expect(screen.getByRole("button", { name: /Aplicar seleccionadas/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Descartar propuesta" })).toBeDisabled();
    expect(container.querySelector("fieldset")).toBeDisabled();
    expect(onApplySelected).not.toHaveBeenCalled();
    expect(onDismiss).not.toHaveBeenCalled();
  });

  it("detecta vencimiento por fecha y conserva la comparación en modo lectura", () => {
    const expiredProposal = {
      ...PROPOSAL,
      id: "expired-proposal",
      expires_at: "2000-01-01T00:00:00.000Z",
    };
    render(
      <QualityProposalPanel
        proposal={expiredProposal}
        currentRevision={12}
        onApplySelected={vi.fn()}
        onDismiss={vi.fn()}
      />,
    );

    expect(screen.getByRole("alert")).toHaveTextContent(/propuesta venció/i);
    expect(screen.getByTestId("quality-proposal-panel")).toHaveAttribute("data-proposal-state", "expired");
    expect(screen.getByText("Real, real, real.")).toBeInTheDocument();
    expect(screen.getAllByRole("checkbox").every((checkbox) => checkbox.disabled)).toBe(true);
    expect(screen.getByRole("button", { name: /Aplicar seleccionadas/i })).toBeDisabled();
  });

  it("maneja una propuesta cerrada o sin ventanas sin ofrecer aplicación", () => {
    const { rerender } = render(
      <QualityProposalPanel
        proposal={{ ...PROPOSAL, status: "applied" }}
        currentRevision={13}
        onApplySelected={vi.fn()}
        onDismiss={vi.fn()}
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent(/ya fue aplicada/i);
    expect(screen.getByTestId("quality-proposal-panel")).toHaveAttribute("data-proposal-state", "applied");
    expect(screen.getByRole("button", { name: /Aplicar seleccionadas/i })).toBeDisabled();

    rerender(
      <QualityProposalPanel
        proposal={{ ...PROPOSAL, id: "empty", windows: [] }}
        currentRevision={12}
        onApplySelected={vi.fn()}
        onDismiss={vi.fn()}
      />,
    );
    expect(screen.getByText(/No hay ventanas sugeridas/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Aplicar seleccionadas/i })).toBeDisabled();
  });
});
