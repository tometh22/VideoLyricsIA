import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import AdminPanel from "./AdminPanel";

function response(body) {
  return Promise.resolve({
    ok: true,
    status: 200,
    json: () => Promise.resolve(body),
  });
}

describe("AdminPanel · navegación de cambios", () => {
  beforeEach(() => {
    global.fetch = vi.fn((url) => {
      const target = String(url);
      if (target.includes("/admin/change-requests")) {
        return response({
          items: [],
          pending_count: 11,
          resolved_count: 73,
          proposal_enabled: true,
          proposal_apply_enabled: true,
        });
      }
      if (target.includes("/admin/stats")) {
        return response({ jobs: { pending_review: 349, processing: 0, errors: 126 } });
      }
      if (target.includes("/admin/stuck-jobs")) return response({ count: 0, jobs: [] });
      if (target.includes("/admin/jobs")) return response({ jobs: [], total: 0 });
      if (target.endsWith("/health")) return response({ status: "ok" });
      return response({});
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
    window.history.replaceState({}, "", "/admin");
  });

  it("abre Cambios UMG como pestaña dedicada y de ancho completo", async () => {
    render(<AdminPanel onBack={() => {}} />);

    await waitFor(() =>
      expect(screen.getByRole("button", { name: /Cambios UMG/ })).toHaveTextContent("11"),
    );
    fireEvent.click(screen.getByRole("button", { name: /Cambios UMG/ }));

    await waitFor(() => {
      expect(
        screen.getByRole("heading", { name: "Cambios UMG" }),
      ).toBeInTheDocument();
      expect(screen.getByTestId("change-requests-fullscreen")).toBeInTheDocument();
      expect(screen.getByTestId("admin-shell")).toHaveClass("max-w-none");
      expect(screen.getByText("pendientes").parentElement).toHaveTextContent("11");
      expect(screen.getByText("resueltos").parentElement).toHaveTextContent("73");
    });
    expect(
      screen.getByText(/Atendé cada pedido de punta a punta/),
    ).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Pipeline en vivo" })).toBe(null);
  });

  it("abre Cambios UMG directamente desde el retorno del editor", async () => {
    window.history.replaceState(
      {}, "", "/admin?section=cambios&change_request_id=77&render_submitted=1",
    );
    render(<AdminPanel onBack={() => {}} />);

    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Cambios UMG" })).toBeInTheDocument();
      expect(screen.getByTestId("admin-shell")).toHaveClass("max-w-none");
    });
    expect(screen.queryByRole("heading", { name: "Pipeline en vivo" })).toBe(null);
  });
});
