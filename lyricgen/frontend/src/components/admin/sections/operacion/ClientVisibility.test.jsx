import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ClientVisibilityControl, PublicationModeSwitch, visibilityMessage } from "./ClientVisibility";

describe("visibilityMessage", () => {
  it("says what the CLIENT sees in each state", () => {
    expect(visibilityMessage({ client_visibility: "auto", hidden_from_client: false })).toMatch(/Visible\. Si el video se edita/);
    expect(visibilityMessage({ client_visibility: "auto", hidden_from_client: true })).toMatch(/Oculto por ahora.*Aparece solo/);
    expect(visibilityMessage({ client_visibility: "visible", hidden_from_client: false })).toMatch(/Siempre visible/);
    expect(visibilityMessage({ client_visibility: "hidden", hidden_from_client: true })).toMatch(/hasta que lo muestres/);
    expect(visibilityMessage({})).toMatch(/Visible/);
    expect(visibilityMessage({ client_visibility: "auto", snapshot_pinned: true })).toMatch(/sigue viendo la versión anterior/);
  });
});

describe("ClientVisibilityControl", () => {
  it("marks the current mode and only fires for a different one", () => {
    const onChange = vi.fn();
    render(<ClientVisibilityControl publication={{ client_visibility: "auto" }} deliveryId={9} onChange={onChange} />);
    expect(screen.getByRole("button", { name: "Automático" })).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getByRole("button", { name: "Automático" }));
    expect(onChange).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Oculto" }));
    expect(onChange).toHaveBeenCalledWith(9, "hidden");
    fireEvent.click(screen.getByRole("button", { name: "Siempre visible" }));
    expect(onChange).toHaveBeenLastCalledWith(9, "visible");
  });

  it("disables the buttons while saving and warns that opened links keep working when hidden", () => {
    const { rerender } = render(<ClientVisibilityControl publication={{ client_visibility: "hidden" }} deliveryId={9} busy onChange={() => {}} />);
    expect(screen.getByRole("button", { name: "Oculto" })).toBeDisabled();
    expect(screen.getByText(/siguen funcionando hasta que venzan/)).toBeInTheDocument();
    rerender(<ClientVisibilityControl publication={{ client_visibility: "auto" }} deliveryId={9} onChange={() => {}} />);
    expect(screen.queryByText(/siguen funcionando/)).not.toBeInTheDocument();
  });

  it("renders nothing without a delivery", () => {
    const { container } = render(<ClientVisibilityControl publication={{}} deliveryId={null} />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe("PublicationModeSwitch", () => {
  it("toggles between copying files and publishing without copies, after confirming", () => {
    const onChange = vi.fn();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    const { rerender } = render(<PublicationModeSwitch mode="snapshot" onChange={onChange} />);
    const toggle = screen.getByRole("switch", { name: /Publicar sin copiar archivos/ });
    expect(toggle).not.toBeChecked();
    expect(screen.getByText(/guarda una copia/)).toBeInTheDocument();
    fireEvent.click(toggle);
    expect(onChange).toHaveBeenCalledWith("pointer");
    rerender(<PublicationModeSwitch mode="pointer" onChange={onChange} />);
    expect(screen.getByRole("switch")).toBeChecked();
    expect(screen.getByText(/se oculta solo mientras tiene cambios sin publicar/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("switch"));
    expect(onChange).toHaveBeenLastCalledWith("snapshot");
    confirm.mockReturnValue(false);
    fireEvent.click(screen.getByRole("switch"));
    expect(onChange).toHaveBeenCalledTimes(2);
    confirm.mockRestore();
  });

  it("is disabled for admins who are not super admin", () => {
    render(<PublicationModeSwitch mode="snapshot" canChange={false} onChange={() => {}} />);
    expect(screen.getByRole("switch")).toBeDisabled();
  });
});
