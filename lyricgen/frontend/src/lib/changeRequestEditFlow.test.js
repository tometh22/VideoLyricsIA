import { describe, expect, it, vi } from "vitest";

import {
  changeRequestAdminPath,
  parseChangeRequestEditContext,
  recoverChangeRequestEditContext,
} from "./changeRequestEditFlow.js";

describe("change-request editor deep-link", () => {
  it("accepts only a complete, bounded request/proposal pair", () => {
    expect(parseChangeRequestEditContext("?change_request_id=42&proposal_id=abc-123_def"))
      .toEqual({ changeRequestId: 42, proposalId: "abc-123_def" });
    expect(parseChangeRequestEditContext("?change_request_id=42")).toBeNull();
    expect(parseChangeRequestEditContext("?change_request_id=../../admin&proposal_id=x")).toBeNull();
    expect(parseChangeRequestEditContext("?change_request_id=42&proposal_id=https://evil.example"))
      .toBeNull();
  });

  it("builds a fixed internal return path with explicit render state", () => {
    const context = { changeRequestId: 42, proposalId: "abc" };
    expect(changeRequestAdminPath(context, "submitted"))
      .toBe("/admin?section=cambios&change_request_id=42&render_submitted=1");
    expect(changeRequestAdminPath(context, "completed"))
      .toBe("/admin?section=cambios&change_request_id=42&render_completed=1");
  });
});

describe("legacy request-only links", () => {
  const job = { job_id: "job-85", segments_revision: 4 };
  const proposal = { id: "proposal-85", job_id: job.job_id, change_request_id: 85,
    status: "applied", applied_revision: 3 };
  const recover = (value = proposal, status = 200, search = "?change_request_id=85") => {
    const request = vi.fn().mockResolvedValue({ ok: status === 200, status,
      json: async () => ({ proposal: value }) });
    return { request, result: recoverChangeRequestEditContext({ search, job, request }) };
  };

  it("recovers the saved proposal and tolerates subsequent editor revisions", async () => {
    const { request, result } = recover();
    await expect(result).resolves.toEqual({ changeRequestId: 85, proposalId: "proposal-85" });
    expect(request).toHaveBeenCalledWith("/admin/change-requests/85/proposals/current");
  });
  it("keeps explicit proposal links without a lookup", async () => {
    const { request, result } = recover(proposal, 200, "?change_request_id=85&proposal_id=chosen-85");
    await expect(result).resolves.toEqual({ changeRequestId: 85, proposalId: "chosen-85" });
    expect(request).not.toHaveBeenCalled();
  });
  it.each(["partially_applied"])("recovers %s proposals", async status => {
    await expect(recover({ ...proposal, status }).result).resolves.toEqual({ changeRequestId: 85, proposalId: "proposal-85" });
  });
  it.each(["pending", "stale", "rejected"])("keeps a manual review context for a %s proposal", async status => {
    await expect(recover({ ...proposal, status }).result).resolves.toEqual({ changeRequestId: 85, proposalId: null });
  });
  it("allows manual requests with no proposal", async () => {
    const request = vi.fn().mockResolvedValueOnce({ status: 404 })
      .mockResolvedValueOnce({ ok: true, json: async () => ({ job_id: job.job_id, resolved: false }) });
    await expect(recoverChangeRequestEditContext({ search: "?change_request_id=85", job, request }))
      .resolves.toEqual({ changeRequestId: 85, proposalId: null });
    expect(changeRequestAdminPath({ changeRequestId: 85, proposalId: null }, "submitted"))
      .toContain("render_submitted=1");
  });
  it.each([{ job_id: "other-job" }, { change_request_id: 86 }])("rejects a mismatched request/job (%j)", async mismatch => {
    await expect(recover({ ...proposal, ...mismatch }).result).rejects.toThrow("change_request_job_mismatch");
  });
  it.each([5, null, -1])("rejects an unavailable saved revision (%s)", async applied_revision => {
    await expect(recover({ ...proposal, applied_revision }).result).rejects.toThrow("change_request_revision_unavailable");
  });
  it("does not silently lose the request on a lookup failure", async () => {
    await expect(recover(null, 503).result).rejects.toThrow("change_request_context_unavailable");
  });
  it("does not replace an invalid explicit proposal", async () => {
    const { request, result } = recover(proposal, 200, "?change_request_id=85&proposal_id=bad%2Fid");
    await expect(result).rejects.toThrow("invalid_change_request_context");
    expect(request).not.toHaveBeenCalled();
  });
});
