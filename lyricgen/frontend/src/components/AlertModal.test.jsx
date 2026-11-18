import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import AlertModal from "./AlertModal";

describe("AlertModal accessibility", () => {
  it("announces its description and keeps keyboard focus inside the dialog", () => {
    const previous = document.createElement("button");
    document.body.appendChild(previous);
    previous.focus();
    render(
      <AlertModal
        open
        onClose={vi.fn()}
        title="Revisión pendiente"
        description="Firmá los controles restantes."
      />,
    );

    const dialog = screen.getByRole("alertdialog", { name: "Revisión pendiente" });
    expect(dialog).toHaveAttribute("aria-describedby", "alert-modal-description");
    const close = screen.getByRole("button", { name: "Cerrar" });
    expect(close).toHaveFocus();
    fireEvent.keyDown(window, { key: "Tab" });
    expect(close).toHaveFocus();
    fireEvent.keyDown(window, { key: "Tab", shiftKey: true });
    expect(close).toHaveFocus();
    previous.remove();
  });
});
