// Tests for the polling component added in PR #445 (audit P0 #75) that
// mounts when /videos/:id/edit-lyrics is opened on a job in status=
// editing. The component lives inline in App.jsx; we test its
// behavior by mirroring the polling loop in a small harness so we
// don't have to mount the whole App + router + i18n stack.

import { render, act, cleanup, screen } from "@testing-library/react";
import { describe, it, expect, vi, afterEach, beforeEach } from "vitest";
import { useEffect, useState } from "react";

afterEach(cleanup);

// MIRROR of App.jsx::EditingNotEditablePanel — keep in sync with the
// production component when its polling contract changes. This is the
// part we want to lock down:
//   - Polls /status/{jobId} every 5s while isRendering=true.
//   - Tick fires immediately on mount (no 5s wait).
//   - When polled status becomes editable, calls window.location.reload.
//   - Cleanup clears the interval on unmount + cancels in-flight ticks.
function PollHarness({ jobId, isRendering, mockFetch, onReload }) {
  const [polledStatus, setPolledStatus] = useState("editing");
  const [polledProgress, setPolledProgress] = useState(null);

  useEffect(() => {
    if (!isRendering) return undefined;
    let cancelled = false;
    const tick = async () => {
      try {
        const res = await mockFetch(`/status/${jobId}`);
        if (!res.ok || cancelled) return;
        const data = await res.json();
        if (cancelled) return;
        setPolledStatus(data.status);
        if (typeof data.progress === "number") setPolledProgress(data.progress);
        const editable = ["done", "pending_review", "rejected"].includes(data.status);
        if (editable) onReload();
      } catch {
        // silent
      }
    };
    const iv = setInterval(tick, 5000);
    tick();
    return () => { cancelled = true; clearInterval(iv); };
  }, [jobId, isRendering, mockFetch, onReload]);

  return (
    <div>
      <span data-testid="status">{polledStatus}</span>
      <span data-testid="progress">{polledProgress ?? "null"}</span>
    </div>
  );
}

describe("EditingNotEditablePanel polling", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("fires an immediate tick on mount (no 5s wait)", async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ status: "editing", progress: 25, current_step: "video" }),
    });
    const onReload = vi.fn();
    render(<PollHarness jobId="abc" isRendering={true} mockFetch={mockFetch} onReload={onReload} />);
    // Let the immediate tick + microtasks resolve.
    await act(async () => { await Promise.resolve(); });
    expect(mockFetch).toHaveBeenCalledTimes(1);
  });

  it("polls every 5s while isRendering=true", async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ status: "editing", progress: 30 }),
    });
    const onReload = vi.fn();
    render(<PollHarness jobId="abc" isRendering={true} mockFetch={mockFetch} onReload={onReload} />);
    await act(async () => { await Promise.resolve(); });
    expect(mockFetch).toHaveBeenCalledTimes(1);
    // Advance 5s.
    await act(async () => { vi.advanceTimersByTime(5000); await Promise.resolve(); });
    expect(mockFetch).toHaveBeenCalledTimes(2);
    // Another 5s.
    await act(async () => { vi.advanceTimersByTime(5000); await Promise.resolve(); });
    expect(mockFetch).toHaveBeenCalledTimes(3);
  });

  it("does NOT poll when isRendering=false (the non-editing fallback)", async () => {
    const mockFetch = vi.fn();
    const onReload = vi.fn();
    render(<PollHarness jobId="abc" isRendering={false} mockFetch={mockFetch} onReload={onReload} />);
    await act(async () => { await Promise.resolve(); });
    await act(async () => { vi.advanceTimersByTime(20000); await Promise.resolve(); });
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it("triggers reload when status transitions to done", async () => {
    let callCount = 0;
    const mockFetch = vi.fn().mockImplementation(() => {
      callCount += 1;
      const status = callCount < 2 ? "editing" : "done";
      return Promise.resolve({ ok: true, json: async () => ({ status, progress: 100 }) });
    });
    const onReload = vi.fn();
    render(<PollHarness jobId="abc" isRendering={true} mockFetch={mockFetch} onReload={onReload} />);
    // First tick: status=editing → no reload.
    await act(async () => { await Promise.resolve(); });
    expect(onReload).not.toHaveBeenCalled();
    // 5s later: status=done → reload.
    await act(async () => { vi.advanceTimersByTime(5000); await Promise.resolve(); });
    expect(onReload).toHaveBeenCalledTimes(1);
  });

  it("triggers reload on pending_review transition too", async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ status: "pending_review" }),
    });
    const onReload = vi.fn();
    render(<PollHarness jobId="abc" isRendering={true} mockFetch={mockFetch} onReload={onReload} />);
    await act(async () => { await Promise.resolve(); });
    expect(onReload).toHaveBeenCalled();
  });

  it("stops polling on unmount (cleanup clears interval)", async () => {
    const mockFetch = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ status: "editing", progress: 10 }),
    });
    const onReload = vi.fn();
    const { unmount } = render(
      <PollHarness jobId="abc" isRendering={true} mockFetch={mockFetch} onReload={onReload} />
    );
    await act(async () => { await Promise.resolve(); });
    expect(mockFetch).toHaveBeenCalledTimes(1);
    unmount();
    // Advance enough time for several would-be ticks.
    await act(async () => { vi.advanceTimersByTime(30000); await Promise.resolve(); });
    // No new fetches after unmount.
    expect(mockFetch).toHaveBeenCalledTimes(1);
  });

  it("swallows fetch errors silently (next tick reintenta)", async () => {
    let callCount = 0;
    const mockFetch = vi.fn().mockImplementation(() => {
      callCount += 1;
      if (callCount === 1) return Promise.reject(new Error("ECONNRESET"));
      return Promise.resolve({ ok: true, json: async () => ({ status: "editing", progress: 50 }) });
    });
    const onReload = vi.fn();
    render(<PollHarness jobId="abc" isRendering={true} mockFetch={mockFetch} onReload={onReload} />);
    await act(async () => { await Promise.resolve(); });
    // First tick rejected — no crash, no reload.
    expect(onReload).not.toHaveBeenCalled();
    // Next tick succeeds.
    await act(async () => { vi.advanceTimersByTime(5000); await Promise.resolve(); });
    expect(mockFetch).toHaveBeenCalledTimes(2);
  });

  it("ignores non-2xx responses without crashing", async () => {
    const mockFetch = vi.fn().mockResolvedValue({ ok: false, json: async () => ({}) });
    const onReload = vi.fn();
    render(<PollHarness jobId="abc" isRendering={true} mockFetch={mockFetch} onReload={onReload} />);
    await act(async () => { await Promise.resolve(); });
    expect(onReload).not.toHaveBeenCalled();
    // The polling continues normally; component stays mounted.
    await act(async () => { vi.advanceTimersByTime(5000); await Promise.resolve(); });
    expect(mockFetch).toHaveBeenCalledTimes(2);
  });
});
