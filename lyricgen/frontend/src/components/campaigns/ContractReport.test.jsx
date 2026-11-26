import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import ContractReport from "./ContractReport";

const entry = reason => ({ at: "2026-11-18T00:00:00Z", actor: 1, detail: { reason } });
const report = {
  campaign_id: "c1", name: "Campaña", at: "2026-11-18T00:00:00Z",
  contract: { revision: 1, agreement: "Acuerdo" }, groups: [], videos: [], history: [entry("Primera página")],
  history_pagination: { limit: 50, offset: 0, has_more: true, next_offset: 50 },
};
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe("contract history", () => {
  it("loads the next audit page only on request and keeps earlier changes", async () => {
    const fetcher = vi.fn(async () => ({ ok: true, json: async () => ({ ...report, history: [entry("Segunda página")], history_pagination: { has_more: false, next_offset: null } }) }));
    vi.stubGlobal("fetch", fetcher);
    render(<ContractReport campaignId="c1" report={report} />);
    expect(fetcher).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Cargar más cambios" }));
    await screen.findByText(/Segunda página/);
    expect(screen.getByText(/Primera página/)).toBeInTheDocument();
    expect(fetcher.mock.calls[0][0]).toBe("/batch/campaigns/c1/creative/report?history_limit=50&history_offset=50");
    expect(screen.queryByRole("button", { name: "Cargar más cambios" })).toBeNull();
  });

  it("fetches the complete report before printing a paginated audit", async () => {
    const fetcher = vi.fn(async () => ({ ok: true, json: async () => ({ ...report, history: [entry("Auditoría completa")], history_pagination: undefined }) }));
    vi.stubGlobal("fetch", fetcher);
    const print = vi.spyOn(window, "print").mockImplementation(() => {
      expect(screen.getByText(/Auditoría completa/)).toBeInTheDocument();
    });
    render(<ContractReport campaignId="c1" report={report} />);
    fireEvent.click(screen.getByRole("button", { name: "Imprimir / guardar PDF" }));
    await waitFor(() => expect(print).toHaveBeenCalledTimes(1));
    expect(fetcher.mock.calls[0][0]).toBe("/batch/campaigns/c1/creative/report");
  });
});
