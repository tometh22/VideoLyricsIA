import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchWithTimeout } from "./fetchWithTimeout";

function abortablePendingFetch(_url, opts) {
  return new Promise((_resolve, reject) => {
    opts.signal.addEventListener("abort", () => {
      const error = new Error("aborted");
      error.name = "AbortError";
      reject(error);
    }, { once: true });
  });
}

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("fetchWithTimeout", () => {
  it("turns its own deadline into a TimeoutError", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn(abortablePendingFetch));
    const pending = fetchWithTimeout("/slow", {}, 100);
    const assertion = expect(pending).rejects.toMatchObject({ name: "TimeoutError" });
    await vi.advanceTimersByTimeAsync(100);
    await assertion;
  });

  it("preserves an external cancellation as AbortError", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn(abortablePendingFetch));
    const controller = new AbortController();
    const pending = fetchWithTimeout("/cancel", { signal: controller.signal }, 10_000);
    const assertion = expect(pending).rejects.toMatchObject({ name: "AbortError" });
    controller.abort();
    await assertion;
  });
});
