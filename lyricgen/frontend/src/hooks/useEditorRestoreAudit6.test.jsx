import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useEditorDocument } from "./useEditorDocument";
import { useEditorAutosave } from "./useEditorAutosave";
afterEach(() => { cleanup(); vi.useRealTimers(); });
const reply = (body, status = 200) => ({ ok: status < 400, status, clone: () => ({ json: async () => body }) });
const rows = text => [{ segment_id: "a", start: 0, end: 2, text }];

describe("sixth audit: restore is ordered with queued saves", () => {
  it("does not let autosave snapshots queued before restore overwrite the restored version", async () => {
    let revision = 1;
    let segments = rows("Original");
    let releaseFirst;
    const firstGate = new Promise(resolve => { releaseFirst = resolve; });
    let patches = 0;
    const request = vi.fn(async (path, options = {}) => {
      if (path.endsWith("/lock/heartbeat")) return reply({ acquired: true });
      if (options.method === "PATCH") {
        patches += 1;
        const body = JSON.parse(options.body);
        if (patches === 1) await firstGate;
        if (body.base_revision !== revision) return reply({ detail: { server_revision: revision, server_segments: segments } }, 409);
        segments = body.segments;
        revision += 1;
        return reply({ revision });
      }
      if (path.endsWith("/restore")) {
        const body = JSON.parse(options.body);
        if (body.base_revision !== revision) return reply({ current_revision: revision }, 409);
        segments = rows("Versión recuperada");
        revision += 1;
        return reply({ job_id: "restore6", revision, segments });
      }
      return reply({ job_id: "restore6", revision, segments });
    });
    const { result } = renderHook(() => useEditorDocument({ jobId: "restore6", enabled: true, request }));
    await waitFor(() => expect(result.current.loading).toBe(false));
    let first, second, restored;
    act(() => {
      first = result.current.save(rows("Edición A"));
      second = result.current.save(rows("Edición B"));
    });
    await waitFor(() => expect(patches).toBe(1));
    act(() => { restored = result.current.restoreVersion("historical-version"); });
    // Let a concurrently issued restore reach the server before releasing the
    // old request. A serialized implementation instead waits for that request.
    await act(async () => { await Promise.resolve(); });
    await act(async () => { releaseFirst(); await Promise.all([first, second, restored]); });
    expect((await restored).ok).toBe(true);
    expect(segments[0].text).toBe("Versión recuperada");
    expect(result.current.document.segments[0].text).toBe("Versión recuperada");
    expect((await second).reason).toBe("superseded");
    await act(async () => { await result.current.save(rows("Edición posterior intencional")); });
    expect(segments[0].text).toBe("Edición posterior intencional");
  });
});

it("does not retry a save superseded by an authoritative restore", async () => {
  vi.useFakeTimers();
  const save = vi.fn().mockResolvedValue({ ok: false, reason: "superseded" });
  const reconcile = vi.fn();
  const onStatus = vi.fn();
  const { result } = renderHook(() => useEditorAutosave({ enabled: true, dirty: false,
    segments: rows("Old snapshot"), save, reconcile, onStatus }));
  await act(async () => { await result.current.flush(); });
  await act(async () => { await vi.advanceTimersByTimeAsync(30000); });
  expect(save).toHaveBeenCalledOnce();
  expect(reconcile).not.toHaveBeenCalled();
  expect(onStatus).not.toHaveBeenCalledWith("error", expect.anything(), expect.anything());
});
