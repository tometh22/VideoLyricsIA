import { describe, expect, it, vi } from "vitest";
import {
  isTransientAudioFailure,
  loadEditorAudio,
  retryAfterMs,
} from "./editorAudioRecovery";

function response(status, body = {}, retryAfter = null) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: vi.fn((name) => name === "Retry-After" ? retryAfter : null) },
    json: async () => body,
  };
}

describe("editor source audio recovery", () => {
  it("honours Retry-After after pool backpressure and recovers without declaring audio missing", async () => {
    const request = vi.fn()
      .mockResolvedValueOnce(response(503, {}, "3"))
      .mockResolvedValueOnce(response(503, {}, "3"))
      .mockResolvedValueOnce(response(200, { url: "https://r2.example/song.mp3" }));
    const wait = vi.fn().mockResolvedValue(undefined);
    const onRetry = vi.fn();

    await expect(loadEditorAudio({ request, wait, onRetry })).resolves.toEqual({
      ok: true,
      url: "https://r2.example/song.mp3",
    });
    expect(wait).toHaveBeenCalledWith(3_000);
    expect(wait).toHaveBeenCalledTimes(2);
    expect(onRetry).toHaveBeenNthCalledWith(1, { attempt: 1, delayMs: 3_000 });
    expect(request).toHaveBeenCalledTimes(3);
  });

  it("only marks audio missing when the endpoint explicitly returns 404", async () => {
    const request = vi.fn().mockResolvedValue(response(404));
    const wait = vi.fn();
    await expect(loadEditorAudio({ request, wait })).resolves.toEqual({ ok: false, reason: "missing" });
    expect(wait).not.toHaveBeenCalled();
  });

  it("keeps 5xx/network failures retryable instead of converting them to a missing file", async () => {
    const request = vi.fn()
      .mockRejectedValueOnce(new Error("network"))
      .mockResolvedValueOnce(response(502))
      .mockResolvedValueOnce(response(503, {}, "2"))
      .mockResolvedValueOnce(response(503, {}, "2"));
    const wait = vi.fn().mockResolvedValue(undefined);
    await expect(loadEditorAudio({ request, wait })).resolves.toEqual({ ok: false, reason: "temporary" });
    expect(wait).toHaveBeenNthCalledWith(1, 1_000);
    expect(wait).toHaveBeenNthCalledWith(2, 2_000);
    expect(wait).toHaveBeenNthCalledWith(3, 2_000);
  });

  it("classifies only backpressure/server failures as transient HTTP statuses", () => {
    expect(isTransientAudioFailure(response(503))).toBe(true);
    expect(isTransientAudioFailure(response(500))).toBe(true);
    expect(isTransientAudioFailure(response(404))).toBe(false);
    expect(retryAfterMs(response(503, {}, "999"), 0)).toBe(60_000);
  });
});
