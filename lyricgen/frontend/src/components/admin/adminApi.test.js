import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchJson } from "./adminApi";

afterEach(() => vi.restoreAllMocks());

describe("fetchJson error details", () => {
  it("keeps structured QC blockers available to the caller and shows their message", async () => {
    const detail = {
      code: "delivery_qc_blocked",
      message: "Revisá los puntos pendientes del preflight antes de publicar.",
      delivery_qc: { blocked: true, reason: "manual_review_required" },
    };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: false,
      status: 409,
      json: async () => ({ detail }),
    }));

    await expect(fetchJson("/admin/deliveries/from-job/job-1")).rejects.toMatchObject({
      message: detail.message,
      detail,
      status: 409,
    });
  });
});
