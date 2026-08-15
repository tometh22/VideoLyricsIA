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

  it("rebases an independent stale PATCH and retries it against the new revision", async () => {
    const patchBodies = [];
    const request = vi.fn(async (path, options = {}) => {
      if (path === "/editor/job-2" && !options.method) {
        return reply({
          job_id: "job-2", revision: 1,
          segments: [{ segment_id: "base", start: 0, end: 1, text: "base" }],
          original_segments: [{ segment_id: "base", start: 0, end: 1, text: "base" }],
          lock: { active: false },
        });
      }
      if (path.endsWith("/lock/heartbeat")) return reply({ acquired: true });
      if (path.endsWith("/lock") && options.method === "DELETE") return reply({ released: true });
      if (path === "/editor/job-2" && options.method === "PATCH") {
        patchBodies.push(JSON.parse(options.body));
        if (patchBodies.length === 1) {
          return reply({
            detail: "editor_revision_conflict",
            server_revision: 2,
            server_segments: [
              { segment_id: "base", start: 0, end: 1, text: "base" },
              { segment_id: "remote", start: 2, end: 3, text: "remote" },
            ],
          }, 409);
        }
        return reply({ revision: 3, version_id: "v3", saved_at: "2026-08-06T10:00:03Z" });
      }
      return reply({}, 404);
    });
    const { result } = renderHook(() => useEditorDocument({
      jobId: "job-2", enabled: true, request,
    }));
    await waitFor(() => expect(result.current.loading).toBe(false));

    let rebased;
    await act(async () => {
      rebased = await result.current.save([{ segment_id: "base", start: 0, end: 1, text: "base" }], "manual");
    });
    expect(rebased).toMatchObject({ ok: false, reason: "merged", serverRevision: 2 });
    expect(result.current.conflict).toBeUndefined();

    await act(async () => {
      await result.current.save(rebased.mergedSegments, "manual");
    });
    expect(patchBodies[1].base_revision).toBe(2);
    expect(patchBodies[1].segments).toEqual(expect.arrayContaining([
      expect.objectContaining({ text: "remote" }),
    ]));
  });

  it("preserves a same-line 409 locally without returning a retryable merge", async () => {
    const request = vi.fn(async (path, options = {}) => {
      if (path === "/editor/job-conflict" && !options.method) {
        return reply({
          job_id: "job-conflict", revision: 1,
          segments: [{ segment_id: "line", start: 0, end: 1, text: "base" }],
          lock: { active: false },
        });
      }
      if (path.endsWith("/lock/heartbeat")) return reply({ acquired: true });
      if (path.endsWith("/lock") && options.method === "DELETE") return reply({ released: true });
      if (path === "/editor/job-conflict" && options.method === "PATCH") {
        return reply({
          detail: "editor_revision_conflict",
          server_revision: 2,
          server_segments: [{ segment_id: "line", start: 0, end: 1, text: "remote" }],
        }, 409);
      }
      return reply({}, 404);
    });
    const { result } = renderHook(() => useEditorDocument({
      jobId: "job-conflict", enabled: true, request,
    }));
    await waitFor(() => expect(result.current.loading).toBe(false));

    let saved;
    await act(async () => {
      saved = await result.current.save([
        { segment_id: "line", start: 0, end: 1, text: "local" },
      ], "manual");
    });
    expect(saved).toMatchObject({ ok: false, reason: "conflict", serverRevision: 2 });
    expect(saved.mergedSegments).toBeUndefined();
    expect(request.mock.calls.filter(([path, options]) => path === "/editor/job-conflict" && options?.method === "PATCH")).toHaveLength(1);
  });

  it("ignora una lectura atrasada y no retrocede la revisión CAS", async () => {
    let loadCount = 0;
    const patchBodies = [];
    const request = vi.fn(async (path, options = {}) => {
      if (path === "/editor/job-3" && !options.method) {
        loadCount += 1;
        return reply({
          job_id: "job-3",
          revision: loadCount === 1 ? 1 : 1,
          segments: [{ start: 0, end: 1, text: loadCount === 1 ? "base" : "replica atrasada" }],
          original_segments: [{ start: 0, end: 1, text: "base" }],
          lock: { active: false },
        });
      }
      if (path.endsWith("/lock/heartbeat")) return reply({ acquired: true });
      if (path.endsWith("/lock") && options.method === "DELETE") return reply({ released: true });
      if (path === "/editor/job-3" && options.method === "PATCH") {
        patchBodies.push(JSON.parse(options.body));
        return reply({ revision: 2, version_id: "v2", saved_at: "2026-08-06T10:00:02Z" });
      }
      return reply({}, 404);
    });
    const { result } = renderHook(() => useEditorDocument({
      jobId: "job-3", enabled: true, request,
    }));
    await waitFor(() => expect(result.current.loading).toBe(false));

    await act(async () => {
      await result.current.save([{ start: 0, end: 1, text: "local" }], "manual");
    });
    expect(result.current.revisionRef.current).toBe(2);

    await act(async () => {
      const reconciliation = await result.current.reconcile([
        { start: 0, end: 1, text: "local" },
      ]);
      expect(reconciliation).toMatchObject({ staleRead: true });
    });
    expect(result.current.revisionRef.current).toBe(2);

    await act(async () => {
      await result.current.save([{ start: 0, end: 1, text: "new local" }], "manual");
    });
    expect(patchBodies.at(-1).base_revision).toBe(2);
  });
});
