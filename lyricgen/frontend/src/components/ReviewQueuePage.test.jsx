import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import ReviewQueuePage from "./ReviewQueuePage";

function response(body) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function row(ordinal, overrides = {}) {
  return {
    item_id: `item-${ordinal}`,
    job_id: `job000000${String(ordinal).padStart(3, "0")}`,
    priority: String(ordinal),
    artist: "Divididos",
    title: `Canción ${ordinal}`,
    version: "studio",
    duration_seconds: 180,
    state: "ready",
    active_minutes: 0,
    review_group: "standard",
    manual_reasons: [],
    reference: { manual_full_review_required: false },
    ...overrides,
  };
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  localStorage.clear();
});

describe("ReviewQueuePage", () => {
  it("carga todas las páginas y conserva el grupo manual al final", async () => {
    const fetchMock = vi.fn(async (input) => {
      const url = String(input);
      if (url === "/batch/campaigns") {
        return response({ items: [{ id: "campaign-1", name: "Agosto", status: "active" }] });
      }
      if (url.includes("page=2")) {
        return response({ items: [row(101)], page: 2, pages: 3 });
      }
      if (url.includes("page=3")) {
        return response({
          items: [row(254, {
            title: "¿Qué Ves?",
            review_group: "manual",
            manual_reasons: ["missing_reference"],
            reference: { manual_full_review_required: true },
          })],
          page: 3,
          pages: 3,
        });
      }
      return response({
        items: [row(1)],
        page: 1,
        pages: 3,
        counters: { ready: 300, approved_today: 0 },
        review_minutes_today: { average: null },
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<MemoryRouter><ReviewQueuePage /></MemoryRouter>);

    await waitFor(() => expect(screen.getByText("¿Qué Ves?")).toBeInTheDocument());
    expect(screen.getByText("Canción 101")).toBeInTheDocument();
    expect(screen.getByText("Revisión manual · 1 canciones")).toBeInTheDocument();
    expect(screen.getByText("Sin referencia")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("page=2"), expect.any(Object),
    );
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("page=3"), expect.any(Object),
    );
  });
});
