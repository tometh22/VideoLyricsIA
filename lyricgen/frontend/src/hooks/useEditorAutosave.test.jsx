import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useEditorAutosave } from "./useEditorAutosave";

afterEach(() => vi.useRealTimers());

describe("useEditorAutosave", () => {
  it("writes a draft at 800ms and a checkpoint after 5s", async () => {
    vi.useFakeTimers();
    const save = vi.fn().mockResolvedValue({ ok: true, revision: 2 });
    renderHook(() => useEditorAutosave({
      enabled: true,
      segments: [{ start: 0, end: 1, text: "line" }],
      dirty: true,
      blocked: false,
      save,
      onStatus: vi.fn(),
    }));
    await act(async () => { await vi.advanceTimersByTimeAsync(800); });
    expect(save).toHaveBeenCalledWith(expect.any(Array), "draft");
    await act(async () => { await vi.advanceTimersByTimeAsync(4_200); });
    expect(save).toHaveBeenCalledWith(expect.any(Array), "autosave");
  });

  it("reconciles before retrying and stops on a reconnect conflict", async () => {
    vi.useFakeTimers();
    const save = vi.fn().mockResolvedValueOnce({ ok: false, reason: "offline" });
    const reconcile = vi.fn().mockResolvedValue({ ok: false, reason: "conflict" });
    const onStatus = vi.fn();
    renderHook(() => useEditorAutosave({
      enabled: true,
      segments: [{ start: 0, end: 1, text: "local" }],
      dirty: true,
      blocked: false,
      save,
      reconcile,
      onStatus,
    }));
    await act(async () => { await vi.advanceTimersByTimeAsync(800); });
    await act(async () => { window.dispatchEvent(new Event("online")); await Promise.resolve(); });
    expect(reconcile).toHaveBeenCalledTimes(1);
    expect(save).toHaveBeenCalledTimes(1);
    expect(onStatus).toHaveBeenCalledWith("conflict", "conflict", expect.any(Object));
  });

  it("flushes the exact approval snapshot instead of a stale render snapshot", async () => {
    const save = vi.fn().mockResolvedValue({ ok: true, revision: 3, versionId: "v3" });
    const current = [{ start: 0, end: 2, text: "current" }];
    const approved = [{ start: 0, end: 1.95, text: "current" }];
    const { result } = renderHook(() => useEditorAutosave({
      enabled: true,
      segments: current,
      dirty: false,
      blocked: false,
      save,
      onStatus: vi.fn(),
    }));

    await act(async () => { await result.current.flush("manual", approved); });
    expect(save).toHaveBeenCalledWith(approved, "manual");
  });
});
