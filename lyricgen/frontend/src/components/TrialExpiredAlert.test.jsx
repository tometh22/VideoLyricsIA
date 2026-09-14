import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import AlertModal from "./AlertModal";

afterEach(cleanup);

describe("expired trial action feedback", () => {
  it("shows the policy message without crashing or exposing ledger metadata", () => {
    const close = vi.fn();
    render(<AlertModal open onClose={close} title="No se pudo eliminar el video"
      description={{ code: "trial_expired", message: "Finalizaron las 24 horas del trial.",
        trial: { grant_id: 928, state: "expired" } }} />);
    expect(screen.getByText("Finalizaron las 24 horas del trial.")).toBeInTheDocument();
    expect(screen.queryByText(/grant_id|\[object Object\]/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Cerrar", exact: true }));
    expect(close).toHaveBeenCalledOnce();
  });

  it("keeps existing rich descriptions working", () => {
    render(<AlertModal open title="Revisá el video" description={<strong>Mensaje existente</strong>} />);
    expect(screen.getByText("Mensaje existente")).toBeInTheDocument();
  });
});
