import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { AlertProvider } from "./AlertProvider";
import JobDetail from "./JobDetail";

vi.mock("../i18n", () => ({
  useI18n: () => ({ t: (key) => key }),
}));

vi.mock("../mediaUrl", () => ({
  getDownloadUrl: vi.fn(),
  useMediaUrl: vi.fn(() => null),
}));

describe("JobDetail active render progress", () => {
  it("shows the real pipeline process for a processing variant", async () => {
    const job = {
      job_id: "variant-123",
      filename: "Mi variante.wav",
      song_title: "Mi variante",
      artist: "Artista",
      status: "processing",
      current_step: "background",
      step_text_es: "Generando el fondo cinematográfico",
      progress: 27,
      eta_s: 180,
    };

    vi.spyOn(globalThis, "fetch").mockResolvedValue({
      ok: true,
      json: async () => job,
    });

    render(
      <MemoryRouter>
        <AlertProvider>
          <JobDetail job={job} onBack={vi.fn()} onJobUpdate={vi.fn()} />
        </AlertProvider>
      </MemoryRouter>,
    );

    expect(screen.getByText("Generando el fondo cinematográfico")).toBeTruthy();
    expect(screen.getByText(/~3 hero.eta_minutes/)).toBeTruthy();
    expect(screen.queryByText("detail.not_available")).toBeNull();

    await waitFor(() => expect(globalThis.fetch).toHaveBeenCalledWith(
      expect.stringContaining("/status/variant-123"),
      expect.any(Object),
    ));

    globalThis.fetch.mockRestore();
  });
});
