import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useEditorAutosave } from "./useEditorAutosave";

afterEach(() => vi.useRealTimers());

describe("useEditorAutosave", () => {
  it.each(["save", "reconcile"])("preserves text and timing edited during an in-flight %s merge", async (stage) => {
    const initial = [
      { id: "a", start: 0, end: 1, text: "Edición de A" },
      { id: "b", start: 2, end: 3, text: "Segunda línea" },
    ];
    const latest = [{ ...initial[0], text: "A queda local", end: 1.8, operator_locked: true }, initial[1]];
    const remoteMerge = [initial[0], { ...initial[1], text: "Edición de B" }];
    let resolve;
    const deferred = new Promise((done) => { resolve = done; });
    const save = vi.fn().mockResolvedValue({ ok: true });
    const reconcile = vi.fn().mockReturnValue(deferred);
    if (stage === "save") save.mockReturnValueOnce(deferred);
    const onMerged = vi.fn();
    const { result, rerender } = renderHook(({ segments }) => useEditorAutosave({
      enabled: true, dirty: false, segments, save, reconcile, onMerged,
    }), { initialProps: { segments: initial } });
    let pending;
    act(() => { pending = stage === "save" ? result.current.flush("autosave") : result.current.forceRecover(); });
    rerender({ segments: latest });
    await act(async () => {
      resolve({ ok: stage === "reconcile", reason: "merged", mergedSegments: remoteMerge });
      await pending;
    });
    const persisted = save.mock.calls.at(-1)[0];
    expect(persisted[0]).toMatchObject({ text: "A queda local", end: 1.8, operator_locked: true });
    expect(persisted[1].text).toBe("Edición de B");
    expect(onMerged.mock.calls.at(-1)[0]).toEqual(persisted);
  });

  it("does not undo an explicit approval override when its save conflicts", async () => {
    const current = [{ id: "a", start: 0, end: 2, text: "current" }];
    const approved = [{ ...current[0], end: 1.95 }];
    const save = vi.fn()
      .mockResolvedValueOnce({ ok: false, reason: "merged", mergedSegments: approved })
      .mockResolvedValueOnce({ ok: true });
    const { result } = renderHook(() => useEditorAutosave({
      enabled: true, dirty: false, segments: current, save, onMerged: vi.fn(),
    }));
    await act(async () => { await result.current.flush("manual", approved); });
    expect(save.mock.calls[1][0]).toEqual(approved);
  });

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

  it("retries a reconnect conflict without emitting a collaboration status", async () => {
    vi.useFakeTimers();
    const save = vi.fn()
      .mockResolvedValueOnce({ ok: false, reason: "offline" })
      .mockResolvedValue({ ok: true, revision: 2 });
    const reconcile = vi.fn().mockResolvedValue({
      ok: true,
      mergedSegments: [
        { start: 0, end: 1, text: "local" },
        { start: 1, end: 2, text: "remote" },
      ],
    });
    const onStatus = vi.fn();
    const onMerged = vi.fn();
    renderHook(() => useEditorAutosave({
      enabled: true,
      segments: [{ start: 0, end: 1, text: "local" }],
      dirty: true,
      blocked: false,
      save,
      reconcile,
      onStatus,
      onMerged,
    }));
    await act(async () => { await vi.advanceTimersByTimeAsync(800); });
    await act(async () => { window.dispatchEvent(new Event("online")); await Promise.resolve(); });
    expect(reconcile).toHaveBeenCalledTimes(1);
    expect(save).toHaveBeenCalledTimes(2);
    expect(onMerged).toHaveBeenCalledWith(expect.arrayContaining([
      expect.objectContaining({ text: "remote" }),
    ]), expect.objectContaining({ mergedSegments: expect.any(Array) }));
    expect(onStatus).not.toHaveBeenCalledWith("conflict", "conflict", expect.any(Object));
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

  it("applies a silent three-way merge to the visible editor before retrying", async () => {
    const merged = [
      { start: 0, end: 1, text: "local" },
      { start: 1, end: 2, text: "remote-only" },
    ];
    const save = vi.fn()
      .mockResolvedValueOnce({ ok: false, reason: "merged", mergedSegments: merged })
      .mockResolvedValueOnce({ ok: true, revision: 4 });
    const onMerged = vi.fn();
    const { result } = renderHook(() => useEditorAutosave({
      enabled: true,
      segments: [{ start: 0, end: 1, text: "local" }],
      dirty: false,
      blocked: false,
      save,
      onStatus: vi.fn(),
      onMerged,
    }));

    await act(async () => { await result.current.flush("manual"); });
    expect(onMerged).toHaveBeenCalledWith(merged, expect.objectContaining({ reason: "merged" }));
    expect(save).toHaveBeenCalledTimes(2);
    expect(save.mock.calls[1][0]).toEqual(merged);
  });

  it("retries a same-line rebase without publishing a conflict status", async () => {
    const merged = [{ start: 0, end: 1, text: "local" }];
    const save = vi.fn()
      .mockResolvedValueOnce({ ok: false, reason: "merged", mergedSegments: merged, hadLineConflicts: true })
      .mockResolvedValueOnce({ ok: true, revision: 2 });
    const onStatus = vi.fn();
    const onMerged = vi.fn();
    const { result } = renderHook(() => useEditorAutosave({
      enabled: true,
      segments: [{ start: 0, end: 1, text: "local" }],
      dirty: false,
      blocked: false,
      save,
      onStatus,
      onMerged,
    }));

    await act(async () => { await result.current.flush("manual"); });
    expect(save).toHaveBeenCalledTimes(2);
    expect(onMerged).toHaveBeenCalledWith(merged, expect.objectContaining({ hadLineConflicts: true }));
    expect(onStatus).not.toHaveBeenCalledWith("conflict", "conflict", expect.any(Object));
  });

  it("treats an unrecoverable reconciliation response as a retryable server failure", async () => {
    vi.useFakeTimers();
    const save = vi.fn().mockResolvedValue({ ok: false, reason: "offline" });
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
    expect(save).toHaveBeenCalledTimes(1);
    await act(async () => { window.dispatchEvent(new Event("online")); await Promise.resolve(); });
    expect(reconcile).toHaveBeenCalledTimes(1);
    expect(save).toHaveBeenCalledTimes(1);
    expect(onStatus).toHaveBeenCalledWith("error", "server", expect.any(Object));
  });

  it("does not restart the debounce when a save callback changes identity", async () => {
    vi.useFakeTimers();
    const firstSave = vi.fn().mockResolvedValue({ ok: true, revision: 2 });
    const secondSave = vi.fn().mockResolvedValue({ ok: true, revision: 3 });
    const segments = [{ start: 0, end: 1, text: "line" }];
    const { rerender } = renderHook(({ save }) => useEditorAutosave({
      enabled: true,
      segments,
      dirty: true,
      blocked: false,
      save,
      onStatus: vi.fn(),
    }), { initialProps: { save: firstSave } });

    await act(async () => { await vi.advanceTimersByTimeAsync(800); });
    expect(firstSave).toHaveBeenCalledTimes(1);

    // useEditorDocument legitimately replaces this callback after a durable
    // response. That is not a lyric edit and must not arm a fresh draft.
    rerender({ save: secondSave });
    await act(async () => { await vi.advanceTimersByTimeAsync(4_200); });
    expect(secondSave).toHaveBeenCalledTimes(1); // original 5 s checkpoint
    await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
    expect(firstSave).toHaveBeenCalledTimes(1);
    expect(secondSave).toHaveBeenCalledTimes(1);
  });

  it("does not retry a failed request after the editor unmounts", async () => {
    vi.useFakeTimers();
    let settle;
    const save = vi.fn().mockImplementation(() => new Promise((resolve) => { settle = resolve; }));
    const onStatus = vi.fn();
    const { unmount } = renderHook(() => useEditorAutosave({
      enabled: true,
      segments: [{ start: 0, end: 1, text: "line" }],
      dirty: true,
      blocked: false,
      save,
      onStatus,
    }));

    await act(async () => { await vi.advanceTimersByTimeAsync(800); });
    expect(save).toHaveBeenCalledTimes(1);
    unmount();
    await act(async () => { settle({ ok: false, reason: "server" }); await Promise.resolve(); });
    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });

    expect(save).toHaveBeenCalledTimes(1);
    expect(onStatus).not.toHaveBeenCalledWith("error", "server", expect.any(Object));
  });
});
