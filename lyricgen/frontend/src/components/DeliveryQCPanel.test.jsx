import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import DeliveryQCPanel from "./DeliveryQCPanel";

afterEach(() => vi.restoreAllMocks());

const job = {
  job_id: "abc123", segments_revision: 2,
  delivery_qc: {
    status: "COMPLETE", mode: "observe", decision: "REVIEW",
    summary: { fail_count: 0, warn_count: 1, open_count: 1 },
    issues: [{
      issue_id: "issue-1", severity: "WARN", status: "OPEN",
      summary: "Texto visible distinto", description: "Revisar cuadro",
      seconds: [12.5], timecodes: ["00:00:12:15"],
    }],
    repairs: { actions: [], candidate_segments: [] },
    approval: { blocked: false },
  },
};

describe("DeliveryQCPanel", () => {
  it("seeks to findings and persists a reviewer decision", async () => {
    const onSeek = vi.fn();
    const onJobUpdate = vi.fn();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ delivery_qc: { ...job.delivery_qc, summary: { open_count: 0 } } }),
    }));
    render(<DeliveryQCPanel job={job} onSeek={onSeek} onJobUpdate={onJobUpdate} onOpenEditor={vi.fn()} />);
    fireEvent.click(screen.getByText("00:00:12:15"));
    expect(onSeek).toHaveBeenCalledWith(12.5);
    fireEvent.click(screen.getByText("Revisado"));
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining("/delivery-qc/issues/issue-1/decision"),
      expect.objectContaining({ method: "POST" }),
    ));
    expect(onJobUpdate).toHaveBeenCalled();
  });
});
