import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchWithTimeout } from "./fetchWithTimeout";

describe("fetchWithTimeout", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("distinguishes its own timeout from caller cancellation", async () => {
    vi.useFakeTimers();
    global.fetch = vi.fn((_url, { signal }) => new Promise((_resolve, reject) => {
      signal.addEventListener("abort", () => reject(Object.assign(new Error("aborted"), { name: "AbortError" })), { once: true });
    }));

    const timedOut = expect(fetchWithTimeout("/slow", {}, 100))
      .rejects.toMatchObject({ name: "TimeoutError" });
    await vi.advanceTimersByTimeAsync(100);
    await timedOut;

    const controller = new AbortController();
    const cancelled = expect(fetchWithTimeout("/cancel", { signal: controller.signal }, 1000))
      .rejects.toMatchObject({ name: "AbortError" });
    controller.abort();
    await cancelled;
  });
});
