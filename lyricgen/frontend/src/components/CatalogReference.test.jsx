import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import CatalogReference from "./CatalogReference";

describe("catalogue source provenance", () => {
  it("does not invent a source for existing songs", () => {
    const { container } = render(<CatalogReference />);
    expect(container.firstChild).toBeNull();
  });
  it("shows absent references without a link", () => {
    render(<CatalogReference reference={{ status: "absent" }} />);
    expect(screen.getByText(/No hay una letra asociada/)).toBeTruthy();
    expect(screen.queryByRole("link", { hidden: true })).toBeNull();
  });
  it("keeps a rejected candidate readable without claiming it was used", () => {
    render(<CatalogReference reference={{ status: "candidate", text: "Texto de prueba", source_url: "javascript:alert(1)", audio_validation: { used: false } }} />);
    expect(screen.getByText(/No se aplicó/)).toBeTruthy();
    expect(screen.getByText("Texto de prueba")).toBeTruthy();
    expect(screen.queryByRole("link", { hidden: true })).toBeNull();
  });
  it("labels applied references as still needing human review", () => {
    render(<CatalogReference reference={{ status: "candidate", source_url: "https://docs.google.com/spreadsheets/d/example/edit", row_numbers: [5, 6], audio_validation: { used: true } }} />);
    expect(screen.getByText(/requieren revisión humana/)).toBeTruthy();
    expect(screen.getByText(/filas 5, 6/)).toBeTruthy();
  });
});
