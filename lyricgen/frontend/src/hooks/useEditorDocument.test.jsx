import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useEditorDocument } from "./useEditorDocument";

function reply(body, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    clone: () => ({ json: async () => body }),
  };
}

afterEach(() => vi.restoreAllMocks());

describe("useEditorDocument save ordering", () => {
  it("serializes PATCH requests and gives the second save the confirmed revision", async () => {
    let releaseFirst;
    const firstResponse = new Promise((resolve) => { releaseFirst = resolve; });
    const patchBodies = [];
    const request = vi.fn(async (path, options = {}) => {
      if (path === "/editor/job-1" && !options.method) {
        return reply({ job_id: "job-1", revision: 0, segments: [], lock: { active: false } });
      }
      if (path.endsWith("/lock/heartbeat")) return reply({ acquired: true });
      if (path.endsWith("/lock") && options.method === "DELETE") return reply({ released: true });
      if (path === "/editor/job-1" && options.method === "PATCH") {
        const body = JSON.parse(options.body);
        patchBodies.push(body);
        if (patchBodies.length === 1) return firstResponse;
        return reply({ revision: 2, version_id: "v2", saved_at: "2026-08-06T10:00:02Z", applied: true });
      }
      return reply({}, 404);
    });
    const { result } = renderHook(() => useEditorDocument({
      jobId: "job-1", enabled: true, request,
    }));
    await waitFor(() => expect(result.current.loading).toBe(false));

    let first;
    let second;
    act(() => {
      first = result.current.save([{ start: 0, end: 1, text: "draft" }], "draft");
      second = result.current.save([{ start: 0, end: 1, text: "manual" }], "manual");
    });
    await waitFor(() => expect(patchBodies).toHaveLength(1));
    expect(patchBodies[0].base_revision).toBe(0);

    releaseFirst(reply({ revision: 1, version_id: null, saved_at: "2026-08-06T10:00:01Z", applied: true }));
    await act(async () => { await first; await second; });
    expect(patchBodies).toHaveLength(2);
    expect(patchBodies[1].base_revision).toBe(1);
  });

  it("does not start a lock heartbeat when the durable document fails to load", async () => {
    const request = vi.fn(async () => reply({ detail: "Job not found." }, 404));
    const { result } = renderHook(() => useEditorDocument({
      jobId: "historical-job", enabled: true, request,
    }));

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.errorStatus).toBe(404);
    expect(request).toHaveBeenCalledTimes(1);
    expect(request).toHaveBeenCalledWith("/editor/historical-job");
  });
});
