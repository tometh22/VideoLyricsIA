import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import ChangeRequestsSection from "./ChangeRequestsSection";

vi.mock("./useChangeRequests", () => ({
  default: () => ({
    crPendingCount: 0, crResolvedCount: 0, crPublicationMode: "snapshot",
    crLoading: false, changeRequests: [],
  }),
}));
vi.mock("./ChangeRequestsPanel", () => ({ default: () => null }));
vi.mock("./ChangeRequestRenderReview", () => ({ default: () => null }));
vi.mock("./ClientVisibility", () => ({ PublicationModeSwitch: () => null }));

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("ChangeRequestsSection delivery quality cohort", () => {
  it("shows the numerator, denominator, categories and follow-up period", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({
        delivery_cohort: {
          status: "ready", followup_days: 30,
          cohort_start: "2026-09-01T00:00:00Z",
          cohort_end: "2026-09-30T00:00:00Z",
          eligible_videos: 4,
          videos_with_lyric_or_timing_request: 1,
          lyric_or_timing_request_rate: 0.25,
          videos_by_category: { letra: 1, sincronizacion: 1 },
          videos_without_reconstructable_first_publish: 2,
        },
      }),
    }));

    render(<ChangeRequestsSection />);

    expect(await screen.findByText("25%")).toBeInTheDocument();
    expect(screen.getByText("1 de 4 con pedido")).toBeInTheDocument();
    expect(screen.getByText(/30 días desde su primera publicación/)).toBeInTheDocument();
    expect(screen.getByText(/sin fecha reconstruible/)).toBeInTheDocument();
    await waitFor(() => expect(global.fetch).toHaveBeenCalledWith(
      expect.stringContaining("/admin/quality/change-requests"),
      expect.any(Object),
    ));
  });

  it("does not display a percentage when publication history cannot be reconstructed", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true, json: () => Promise.resolve({
        delivery_cohort: { status: "unavailable" },
      }),
    }));
    render(<ChangeRequestsSection />);
    await waitFor(() => expect(global.fetch).toHaveBeenCalled());
    expect(screen.queryByTestId("change-request-quality-cohort")).not.toBeInTheDocument();
  });
});
