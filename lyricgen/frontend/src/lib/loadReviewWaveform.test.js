import { describe, expect, it, vi } from "vitest";
import { loadReviewWaveform } from "./loadReviewWaveform";

const response = (body, ok = true) => ({ ok, json: async () => body });

describe("loadReviewWaveform", () => {
  it("devuelve un envelope válido", async () => {
    const request = vi.fn().mockResolvedValue(response({ duration: 3, peaks: [0.1, 0.8] }));
    await expect(loadReviewWaveform({ request, url: "/waveform", retryDelayMs: 0 }))
      .resolves.toEqual({ duration: 3, peaks: [0.1, 0.8] });
  });

  it("reintenta una falla transitoria de forma acotada", async () => {
    const request = vi.fn()
      .mockRejectedValueOnce(new Error("network"))
      .mockResolvedValueOnce(response({ duration: 1, peaks: [0.4] }));
    await expect(loadReviewWaveform({ request, url: "/waveform", retryDelayMs: 0 }))
      .resolves.toEqual({ duration: 1, peaks: [0.4] });
    expect(request).toHaveBeenCalledTimes(2);
  });

  it("falla cerrado ante payload inválido o error terminal", async () => {
    await expect(loadReviewWaveform({
      request: vi.fn().mockResolvedValue(response({ duration: 1 })),
      url: "/waveform",
      retryDelayMs: 0,
    })).resolves.toBeNull();
    const request = vi.fn().mockResolvedValue(response({}, false));
    await expect(loadReviewWaveform({ request, url: "/waveform", retryDelayMs: 0 }))
      .resolves.toBeNull();
    expect(request).toHaveBeenCalledTimes(2);
  });
});
