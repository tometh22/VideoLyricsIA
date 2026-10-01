import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchJson } from "./adminApi";

afterEach(() => vi.unstubAllGlobals());

describe("admin API structured errors", () => {
  it("preserves conflict status, code and evidence instead of stringifying an object", async () => {
    const detail = { code: "proposal_changed", message: "Actualizá la propuesta", revision: 7 };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 409, json: async () => ({ detail }) }));
    await expect(fetchJson("/test")).rejects.toMatchObject({ status: 409, code: "proposal_changed", detail, message: "Actualizá la propuesta" });
  });
  it("preserves HTTP status when a proxy returns non-JSON", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 502, json: async () => { throw new Error("html"); } }));
    await expect(fetchJson("/test")).rejects.toMatchObject({ status: 502, message: "HTTP 502" });
  });
  it("does not invent a status for a lost network response", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
    await expect(fetchJson("/test")).rejects.toMatchObject({ message: "Failed to fetch" });
  });
});
