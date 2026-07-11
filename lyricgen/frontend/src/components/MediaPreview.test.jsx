import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import MediaPreview from "./MediaPreview";

afterEach(cleanup);

describe("MediaPreview", () => {
  it("renders the shared AI fallback while media is unavailable", () => {
    render(<MediaPreview status="processing" />);
    expect(screen.getByText("Generando preview con IA")).toBeTruthy();
    expect(screen.queryByRole("img")).toBeNull();
  });

  it("shows media when a source exists and falls back after an image error", () => {
    render(<MediaPreview src="/broken-thumbnail.jpg" alt="Video preview" />);
    const image = screen.getByRole("img", { name: "Video preview" });
    expect(image.getAttribute("src")).toBe("/broken-thumbnail.jpg");
    fireEvent.error(image);
    expect(screen.queryByRole("img", { name: "Video preview" })).toBeNull();
    expect(screen.getByText("Miniatura no disponible")).toBeTruthy();
  });

  it("recovers when the source changes after a failed image", () => {
    const view = render(<MediaPreview src="/first.jpg" alt="Preview" />);
    fireEvent.error(screen.getByRole("img", { name: "Preview" }));
    view.rerender(<MediaPreview src="/second.jpg" alt="Preview" />);
    expect(screen.getByRole("img", { name: "Preview" }).getAttribute("src")).toBe("/second.jpg");
  });
});
