import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const fetchSseMock = vi.hoisted(() => vi.fn());

vi.mock("../lib/fetchSse", () => ({
  fetchSse: fetchSseMock,
  SseUnauthorizedError: class SseUnauthorizedError extends Error {},
}));

import useJobProgress from "./useJobProgress";

describe("useJobProgress terminal polling contract", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    fetchSseMock.mockRejectedValue(new Error("stream dropped"));
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it.each([
    "transcription_failed",
    "bg_preview_done",
    "bg_preview_failed",
  ])("stops fallback polling on %s", async (status) => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ status, progress: 100 }),
    });

    const { result, unmount } = renderHook(() => useJobProgress("job-1", {
      api: "http://test",
      token: "token",
    }));

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(result.current.status).toBe(status);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(global.fetch).toHaveBeenCalledTimes(1);
    unmount();
  });
});
